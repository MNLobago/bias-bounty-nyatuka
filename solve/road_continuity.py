"""Bounded, label-free Overture road-tag continuity diagnostic."""

from collections import defaultdict

import numpy as np
import pyarrow.parquet as pq
from sklearn.linear_model import LogisticRegression


CLASSES = ("motorway", "trunk", "primary", "secondary")
MAX_SOURCE_ROWS = 120_000
MAX_BLOCKS_PER_LABEL = 180


def _region_blocks(path, region, route_group, county_lookup):
    """Two adjacent same-class segments form a masked corridor block."""
    pf = pq.ParquetFile(path)
    rows = []
    seen = 0
    for batch in pf.iter_batches(batch_size=30_000,
                                 columns=["class", "routes", "connectors", "bbox"]):
        columns = [batch.column(i).to_pylist() for i in range(4)]
        for cls, routes, connectors, bbox in zip(*columns):
            if bbox is None:
                continue
            x0, y0, x1, y1 = (bbox[k] for k in ("xmin", "ymin", "xmax", "ymax"))
            if cls not in CLASSES or not connectors or x0 is None or y0 is None:
                continue
            endpoints = [c["connector_id"] for c in connectors
                         if c and c.get("connector_id") and (c.get("at") == 0 or c.get("at") == 1)]
            if len(endpoints) < 2:
                continue
            tag = int(any(route_group(r.get("network")) < 3 for r in (routes or []) if r))
            rows.append((cls, tag, endpoints[0], endpoints[-1],
                         (x0 + x1) / 2, (y0 + y1) / 2))
        seen += batch.num_rows
        if seen >= MAX_SOURCE_ROWS:
            break

    counties = county_lookup(region, np.array([r[4] for r in rows]),
                             np.array([r[5] for r in rows]))
    at = defaultdict(list)
    for i, (_, _, a, b, _, _) in enumerate(rows):
        at[a].append(i)
        at[b].append(i)
    neighbors = [set() for _ in rows]
    for incident in at.values():
        if len(incident) == 2:
            a, b = incident
            if a != b and rows[a][0] == rows[b][0]:
                neighbors[a].add(b)
                neighbors[b].add(a)
    rng = np.random.default_rng([9826, len(rows)])
    pairs = []
    for i, row in enumerate(rows):
        for j in neighbors[i]:
            if i >= j or row[1] != rows[j][1]:
                continue
            outer = (neighbors[i] | neighbors[j]) - {i, j}
            if len(outer) < 2:
                continue
            pairs.append((i, j))
    rng.shuffle(pairs)
    chosen, used, counts = [], set(), {0: 0, 1: 0}
    for i, j in pairs:
        y = rows[i][1]
        if i in used or j in used or counts[y] >= MAX_BLOCKS_PER_LABEL:
            continue
        chosen.append((i, j))
        used.update((i, j))
        counts[y] += 1
    out = []
    for block_id, (i, j) in enumerate(chosen):
        outside = (neighbors[i] | neighbors[j]) - {i, j}
        tagged = sum(rows[k][1] for k in outside)
        degree = len(outside)
        out.append({"region": region, "block": f"{region}:{block_id}",
                    "class": rows[i][0], "county": str(counties[i]), "y": rows[i][1],
                    "tagged": tagged, "degree": degree})
    return out, {"source_rows": seen, "eligible_segments": len(rows),
                 "candidate_pairs": len(pairs), "blocks": len(chosen),
                 "tagged_blocks": counts[1], "untagged_blocks": counts[0]}


def run(paths, regions, route_group, county_lookup):
    blocks, audit = [], {}
    for region in regions:
        b, audit[region] = _region_blocks(paths[region]["roads"], region, route_group, county_lookup)
        blocks.extend(b)
    if not blocks or any(a["tagged_blocks"] < 20 or a["untagged_blocks"] < 20 for a in audit.values()):
        return {"passes": False, "reason": "too few eligible corridor blocks", "audit": audit}

    y = np.array([b["y"] for b in blocks], dtype=int)
    region = np.array([b["region"] for b in blocks])
    cls = np.array([CLASSES.index(b["class"]) for b in blocks])
    class_onehot = np.eye(len(CLASSES))[cls]
    tagged = np.array([b["tagged"] for b in blocks], dtype=float)
    degree = np.array([b["degree"] for b in blocks], dtype=float)
    graph_x = np.column_stack((class_onehot, tagged / np.maximum(degree, 1), degree))
    shuffle_x = graph_x.copy()
    rng = np.random.default_rng(9827)
    # Permuting adjacency summaries within region, county and class destroys
    # topology while leaving each segment's own tag, class and length untouched.
    for key in set((b["region"], b["county"], b["class"]) for b in blocks):
        ix = np.array([i for i, b in enumerate(blocks)
                       if (b["region"], b["county"], b["class"]) == key])
        if len(ix) > 1:
            shuffle_x[ix, -2:] = shuffle_x[rng.permutation(ix), -2:]

    results = {}
    loss_graph = np.zeros(len(y))
    loss_shuffle = np.zeros(len(y))
    loss_class = np.zeros(len(y))
    loss_zero = y.astype(float)  # squared loss of the current missing-tag = 0 control
    for hold in regions:
        train, test = region != hold, region == hold
        def predict(x):
            model = LogisticRegression(C=1.0, max_iter=300, random_state=9828)
            model.fit(x[train], y[train])
            return model.predict_proba(x[test])[:, 1]
        pg = predict(graph_x)
        ps = predict(shuffle_x)
        pc = predict(class_onehot)
        loss_graph[test] = (pg - y[test]) ** 2
        loss_shuffle[test] = (ps - y[test]) ** 2
        loss_class[test] = (pc - y[test]) ** 2
        results[hold] = {"blocks": int(test.sum()),
                         "brier_absent_control": float(loss_zero[test].mean()),
                         "brier_graph": float(loss_graph[test].mean()),
                         "brier_shuffled": float(loss_shuffle[test].mean()),
                         "brier_class_only": float(loss_class[test].mean())}

    gain = loss_zero - loss_graph
    placebo_increment = loss_class - loss_shuffle
    boot = np.empty((2000, 2))
    for z in range(len(boot)):
        take = np.concatenate([rng.choice(np.flatnonzero(region == r),
                                          size=int((region == r).sum()), replace=True) for r in regions])
        boot[z] = [gain[take].mean(), placebo_increment[take].mean()]
    ci = np.quantile(boot, [0.025, 0.975], axis=0)
    main_pass = all(results[r]["brier_graph"] < results[r]["brier_absent_control"] for r in regions)
    placebo_pass = ci[0, 1] > 0
    passed = bool(main_pass and ci[0, 0] > 0 and not placebo_pass)
    return {"audit": audit, "by_region": results, "n_blocks": len(blocks),
            "pooled_brier_improvement": float(gain.mean()),
            "pooled_improvement_ci95": ci[:, 0].tolist(),
            "shuffled_increment_over_class": float(placebo_increment.mean()),
            "shuffled_increment_ci95": ci[:, 1].tolist(),
            "main_pass": bool(main_pass and ci[0, 0] > 0),
            "negative_control_pass": bool(not placebo_pass),
            "passes": passed,
            "target_validation": None,
            "scope": "first 120000 road rows per scored region; two-segment endpoint-connected blocks"}
