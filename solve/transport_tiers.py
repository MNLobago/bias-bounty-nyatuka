"""Round 115: transport-eligibility tiers and masked route-tag recovery on Overture connectors.

Label-free and Overture-only. A segment's "tag" is whether any of its `routes[].network`
values falls in the CORE/FM/CO route groups (the groups that feed the TIGER-like length).
Neighbours are highway-class segments that share an official Overture `connector_id`.
In five county-disjoint folds, half of all segment tags are masked; masked segments are
predicted from the visible tags of their connector neighbours. The negative control rewires
the connector graph inside each region (degree of the focal segment kept, partners random).
Nothing here reads a removed reference layer or a target value.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

CLASSES = ("motorway", "trunk", "primary", "secondary")
N_FOLDS = 5
MASK_RATE = 0.5
SEED = 11500


def _segments(path, region, route_group, tract_lookup, cache_dir):
    cache = Path(cache_dir) / f"r115_segments_{region}.parquet"
    edges_cache = Path(cache_dir) / f"r115_edges_{region}.parquet"
    if cache.exists() and edges_cache.exists():
        return pd.read_parquet(cache), pd.read_parquet(edges_cache)
    table = pq.read_table(path, columns=["class", "routes", "connectors", "bbox"])
    table = table.filter(pc.is_in(table.column("class"), value_set=pa_array(CLASSES)))
    cls = table.column("class").to_pylist()
    routes = table.column("routes").to_pylist()
    conns = table.column("connectors").to_pylist()
    bbox = table.column("bbox").to_pylist()
    rows, links = [], []
    for i, (c, r, k, b) in enumerate(zip(cls, routes, conns, bbox)):
        if b is None or b.get("xmin") is None:
            x = y = np.nan
        else:
            x, y = (b["xmin"] + b["xmax"]) / 2, (b["ymin"] + b["ymax"]) / 2
        tag = int(any(route_group(e.get("network")) < 3 for e in (r or []) if e))
        rows.append((CLASSES.index(c), tag, x, y))
        for e in k or []:
            if e and e.get("connector_id"):
                links.append((e["connector_id"], i))
    seg = pd.DataFrame(rows, columns=["cls", "tag", "x", "y"])
    ok = seg.x.notna().to_numpy()
    tract = np.full(len(seg), "outside", dtype=object)
    if ok.any():
        tract[ok] = tract_lookup(region, seg.x.to_numpy()[ok], seg.y.to_numpy()[ok])
    seg["tract"] = tract.astype(str)
    seg["county"] = np.where(seg.tract == "outside", "outside", seg.tract.str[:5])
    seg["region"] = region
    lk = pd.DataFrame(links, columns=["cid", "seg"]).drop_duplicates()
    pairs = lk.merge(lk, on="cid")
    pairs = pairs[pairs.seg_x != pairs.seg_y][["seg_x", "seg_y"]].drop_duplicates()
    edges = pd.DataFrame({"i": pairs.seg_x.to_numpy(np.int64), "j": pairs.seg_y.to_numpy(np.int64)})
    seg.to_parquet(cache, index=False)
    edges.to_parquet(edges_cache, index=False)
    return seg, edges


def pa_array(values):
    import pyarrow as pa
    return pa.array(list(values))


def _features(n, i, j, visible_tag):
    """visible_tag: 1 tagged, 0 untagged, -1 masked. Features of each focal segment from its neighbours."""
    deg = np.bincount(i, minlength=n).astype(float)
    vis_j = visible_tag[j] >= 0
    vis = np.bincount(i[vis_j], minlength=n).astype(float)
    tagged = np.bincount(i[vis_j], weights=(visible_tag[j][vis_j] == 1).astype(float), minlength=n)
    frac = np.where(vis > 0, tagged / np.maximum(vis, 1), 0.0)
    return np.column_stack((frac, (vis == 0).astype(float), np.log1p(deg), np.log1p(tagged)))


def _rewire(seg, i, j, rng, by="region"):
    """Within each region (or county), send every edge to a random partner (focal degree kept)."""
    j2 = j.copy()
    key = seg[by].to_numpy()
    for r in np.unique(key):
        ix = np.flatnonzero(key == r)
        e = np.isin(i, ix)
        j2[e] = rng.choice(ix, size=int(e.sum()), replace=True)
    keep = j2 != i
    return i[keep], j2[keep]


def run(paths, regions, route_group, tract_lookup, cache_dir, tract_frame):
    """tract_frame: GEOID, region, overture_hw_m, routed_hw_m, current_defined (bool)."""
    segs, edge_parts, offset, audit = [], [], 0, {}
    for r in regions:
        s, e = _segments(paths[r]["roads"], r, route_group, tract_lookup, cache_dir)
        audit[r] = {"segments": len(s), "tagged_share": float(s.tag.mean()) if len(s) else None,
                    "edges": len(e), "outside_share": float((s.tract == "outside").mean()) if len(s) else None}
        segs.append(s)
        edge_parts.append(e + offset)
        offset += len(s)
    seg = pd.concat(segs, ignore_index=True)
    edges = pd.concat(edge_parts, ignore_index=True)
    n = len(seg)
    i, j = edges.i.to_numpy(), edges.j.to_numpy()
    y = seg.tag.to_numpy(int)
    cls1h = np.eye(len(CLASSES))[seg.cls.to_numpy()]
    rng = np.random.default_rng(SEED)
    i_sh, j_sh = _rewire(seg, i, j, rng)
    i_sc, j_sc = _rewire(seg, i, j, rng, by="county")  # stricter control, added after the smoke run

    counties = np.array(sorted(c for c in seg.county.unique() if c != "outside"))
    fold_of_county = dict(zip(counties, rng.permutation(len(counties)) % N_FOLDS))
    fold = np.array([fold_of_county.get(c, -1) for c in seg.county], dtype=int)

    folds, prob_full = [], np.full(n, np.nan)
    true_feat = _features(n, i, j, y.copy())
    for k in range(N_FOLDS):
        mask = rng.random(n) < MASK_RATE
        vis = np.where(mask, -1, y)
        fg = _features(n, i, j, vis)
        fs = _features(n, i_sh, j_sh, vis)
        fc = _features(n, i_sc, j_sc, vis)
        tr, te = mask & (fold != k), mask & (fold == k)
        out = {"fold": k, "test_segments": int(te.sum()), "test_tagged_share": float(y[te].mean())}
        for name, x in (("graph", fg), ("shuffled_graph", fs), ("county_rewired_graph", fc), ("class_only", cls1h),
                        ("class_plus_graph", np.column_stack((cls1h, fg)))):
            m = LogisticRegression(C=1.0, max_iter=500, random_state=SEED).fit(x[tr], y[tr])
            out[f"auc_{name}"] = float(roc_auc_score(y[te], m.predict_proba(x[te])[:, 1]))
        # tract-level use: every segment of a held-out county, scored with all true neighbour tags
        m = LogisticRegression(C=1.0, max_iter=500, random_state=SEED).fit(
            np.column_stack((cls1h, fg))[tr], y[tr])
        hold = fold == k
        prob_full[hold] = m.predict_proba(np.column_stack((cls1h, true_feat))[hold])[:, 1]
        folds.append(out)

    tf = tract_frame.copy()
    tier = np.where(tf.overture_hw_m.to_numpy() <= 0, "UNDEFINED",
                    np.where(tf.routed_hw_m.to_numpy() > 0, "DEFINED", "AMBIGUOUS"))
    tf["tier"] = tier
    unt = seg[(seg.tag == 0) & (seg.tract != "outside")].assign(p=prob_full[(seg.tag == 0).to_numpy()
                                                                            & (seg.tract != "outside").to_numpy()])
    pmax = unt.groupby("tract").p.max()
    tf["p_tag_max"] = pd.Series(tf.GEOID).map(pmax).to_numpy()
    implied = np.where(tier == "DEFINED", True, np.where(tier == "UNDEFINED", False,
                       np.nan_to_num(tf.p_tag_max.to_numpy(float), nan=0.0) > 0.5))
    tf["implied_defined"] = implied.astype(bool)
    tf["reclassified"] = tf.implied_defined != tf.current_defined.astype(bool)

    graph_auc = [f["auc_graph"] for f in folds]
    shuf_auc = [f["auc_shuffled_graph"] for f in folds]
    by_region = tf.groupby("region").reclassified.sum().astype(int).to_dict()
    report = {
        "audit": audit, "segments": n, "edges": len(edges), "counties": len(counties), "folds": folds,
        "tiers": pd.Series(tier).value_counts().to_dict(),
        "tiers_by_region": tf.groupby(["region", "tier"]).size().unstack(fill_value=0).to_dict("index"),
        "current_defined_by_tier": tf.groupby("tier").current_defined.mean().to_dict(),
        "reclassified_total": int(tf.reclassified.sum()),
        "reclassified_rate": float(tf.reclassified.mean()),
        "reclassified_by_region": by_region,
        "reclassified_direction": {"to_defined": int((tf.reclassified & tf.implied_defined).sum()),
                                   "to_undefined": int((tf.reclassified & ~tf.implied_defined).sum())},
        "gate_a_reclass": bool(tf.reclassified.mean() >= 0.05 and max(by_region, key=by_region.get) == "maricopa-az"),
        "topology_auc_increment_over_region_rewire": float(np.mean(graph_auc) - np.mean(shuf_auc)),
        "topology_auc_increment_over_county_rewire": float(np.mean(graph_auc) - np.mean(
            [f["auc_county_rewired_graph"] for f in folds])),
        "gate_b_auc": bool(sum(a > 0.65 for a in graph_auc) >= 4 and max(shuf_auc) <= 0.55),
    }
    return report, tf
