"""R104: Overture-only, label-free road-continuity screen.

This deliberately predicts masked Overture named-route presence, never a TIGER label.
"""

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from sklearn.linear_model import LogisticRegression

PROBE_M = 150


def _fold(county):
    return int.from_bytes(hashlib.sha256(county.encode()).digest()[:4], "little") % 5


def _features(approaches, diagonal_m):
    """Count aligned arrivals separated across a tract, without using in-tract roads."""
    out = np.zeros((len(diagonal_m), 2), float)
    for t, rec in approaches.items():
        if len(rec) < 2:
            continue
        a = np.asarray(rec[:120], float)
        dx = (a[:, 0, None] - a[None, :, 0]) * 111_195 * math.cos(math.radians(float(np.mean(a[:, 1]))))
        dy = (a[:, 1, None] - a[None, :, 1]) * 111_195
        sep = np.hypot(dx, dy)
        dot = a[:, 2, None] * a[None, :, 2] + a[:, 3, None] * a[None, :, 3]
        pair = np.triu((dot < -0.75) & (sep > np.maximum(100.0, 0.25 * diagonal_m[t])), 1)
        out[t, 0] = float(pair.any())
        out[t, 1] = min(int(pair.sum()), 20) / 20.0
    return out


def _logloss(y, p):
    p = np.clip(p, 1e-5, 1 - 1e-5)
    return -(y * np.log(p) + (1 - y) * np.log1p(-p))


def run(paths, regions, sample, tract_index, wkb_parts, best_route_group, cache_dir, highway_counts):
    rng = np.random.default_rng(10427)
    records, audits = [], {}
    source_hash = hashlib.sha256(Path(__file__).read_bytes() + repr((PROBE_M,)).encode()).hexdigest()[:12]
    for region in regions:
        cached = cache_dir / f"{region}__r104_approaches_{source_hash}.parquet"
        cached_audit = cache_dir / f"{region}__r104_approaches_{source_hash}.json"
        if cached.exists() and cached_audit.exists():
            part = pd.read_parquet(cached)
            records.extend(part.itertuples(index=False, name=None))
            audits[region] = json.loads(cached_audit.read_text(encoding="utf-8"))
            print("R104", region, audits[region], "cached", flush=True)
            continue
        tab = pq.read_table(paths[region]["tracts"], columns=["GEOID", "geometry"])
        geoids = np.asarray(tab.column("GEOID").to_pylist(), dtype=str)
        idx = tract_index(tab.column("geometry").to_pylist())
        bbox = idx.bbox
        diagonal = np.hypot((bbox[:, 2] - bbox[:, 0]) * 111_195 * np.cos(np.radians((bbox[:, 1] + bbox[:, 3]) / 2)),
                            (bbox[:, 3] - bbox[:, 1]) * 111_195)
        counts = highway_counts[region][["GEOID", "overture_hw_m"]].set_index("GEOID")
        hw_m = counts.loc[geoids, "overture_hw_m"].to_numpy(float)
        assert np.isfinite(hw_m).all() and (hw_m >= 0).all()
        approaches = defaultdict(list)
        pf = pq.ParquetFile(paths[region]["roads"])
        n_named = n_parts = 0
        for batch in pf.iter_batches(batch_size=30_000, columns=["routes", "geometry"]):
            groups = best_route_group(batch.column(0))
            selected = np.flatnonzero(groups <= 2)
            if not len(selected):
                continue
            n_named += len(selected)
            xs, ys, po, _ = wkb_parts(batch.column(1).take(selected).to_pylist())
            valid = np.flatnonzero(np.diff(po) >= 2)
            if not len(valid):
                continue
            n_parts += len(valid)
            a, b = po[valid], po[valid + 1] - 1
            ex = np.r_[xs[a], xs[b]]
            ey = np.r_[ys[a], ys[b]]
            prevx = np.r_[xs[a + 1], xs[b - 1]]
            prevy = np.r_[ys[a + 1], ys[b - 1]]
            vx = (ex - prevx) * np.cos(np.radians(ey))
            vy = ey - prevy
            norm = np.maximum(np.hypot(vx, vy), 1e-12)
            ux, uy = vx / norm, vy / norm
            probe_x = ex + ux * PROBE_M / (111_195 * np.cos(np.radians(ey)))
            probe_y = ey + uy * PROBE_M / 111_195
            target = idx.assign(probe_x, probe_y)
            endpoint_tract = idx.assign(ex, ey)
            good = np.flatnonzero((target >= 0) & (endpoint_tract != target))
            for j in good:
                approaches[int(target[j])].append((ex[j], ey[j], ux[j], uy[j]))
        features = _features(approaches, diagonal)
        audits[region] = {"tracts": len(geoids), "named_segments": n_named, "named_parts": n_parts,
                          "with_highway": int((hw_m > 0).sum()),
                          "natural_empty": int((hw_m == 0).sum()),
                          "natural_continuity_flags": int(((hw_m == 0) & (features[:, 0] > 0)).sum())}
        part = pd.DataFrame({"GEOID": geoids, "region": region,
                             "county": [g[:5] for g in geoids], "y": (hw_m > 0).astype(int),
                             "overture_hw_m": hw_m,
                             "pair_flag": features[:, 0], "pair_strength": features[:, 1]})
        records.extend(part.itertuples(index=False, name=None))
        part.to_parquet(cached, index=False)
        cached_audit.write_text(json.dumps(audits[region]), encoding="utf-8")
        print("R104", region, audits[region], flush=True)

    frame = pd.DataFrame(records, columns=["GEOID", "region", "county", "y", "overture_hw_m", "pair_flag", "pair_strength"])
    frame = sample[["GEOID"]].merge(frame, on="GEOID", how="left", validate="one_to_one")
    assert frame.y.notna().all() and len(frame) == len(sample)
    y = frame.y.to_numpy(int)
    county = frame.county.to_numpy(str)
    folds = np.array([_fold(c) for c in county])
    x = frame[["pair_flag", "pair_strength"]].to_numpy(float)
    x_shuffle = x.copy()
    # Move complete endpoint-derived feature records among tracts inside each county.
    for c in np.unique(county):
        ii = np.flatnonzero(county == c)
        if len(ii) > 1:
            x_shuffle[ii] = x[rng.permutation(ii)]
    loss_base = np.empty(len(y)); loss_main = np.empty(len(y)); loss_shuffle = np.empty(len(y))
    loss_zero_rule = _logloss(y, 0.01)  # clipped hard-zero rule, same binary log loss
    fold_report = []
    for f in range(5):
        tr, te = folds != f, folds == f
        assert tr.sum() and te.sum() and np.unique(y[tr]).size == 2
        prior = (y[tr].sum() + 1) / (tr.sum() + 2)
        loss_base[te] = _logloss(y[te], prior)
        def fit_predict(xx):
            model = LogisticRegression(C=0.2, max_iter=300, random_state=10427)
            model.fit(xx[tr], y[tr])
            return model.predict_proba(xx[te])[:, 1]
        loss_main[te] = _logloss(y[te], fit_predict(x))
        loss_shuffle[te] = _logloss(y[te], fit_predict(x_shuffle))
        fold_report.append({"fold": f, "tracts": int(te.sum()), "positives": int(y[te].sum()),
                            "base_logloss": float(loss_base[te].mean()),
                            "zero_rule_logloss": float(loss_zero_rule[te].mean()),
                            "continuity_logloss": float(loss_main[te].mean()),
                            "shuffled_logloss": float(loss_shuffle[te].mean()),
                            "gain": float((loss_base[te] - loss_main[te]).mean()),
                            "shuffle_gain": float((loss_base[te] - loss_shuffle[te]).mean())})
    gain = loss_base - loss_main
    shuffle_gain = loss_base - loss_shuffle
    paired_gain = loss_shuffle - loss_main
    unique_counties = np.unique(county)
    county_sum = np.array([gain[county == c].sum() for c in unique_counties])
    shuffle_sum = np.array([shuffle_gain[county == c].sum() for c in unique_counties])
    paired_sum = np.array([paired_gain[county == c].sum() for c in unique_counties])
    county_size = np.array([(county == c).sum() for c in unique_counties])
    boot = np.empty((2000, 3))
    for j in range(len(boot)):
        selected = rng.integers(0, len(unique_counties), len(unique_counties))
        denom = county_size[selected].sum()
        boot[j] = [v[selected].sum() / denom for v in (county_sum, shuffle_sum, paired_sum)]
    ci, shuffle_ci, paired_ci = [np.quantile(boot[:, k], [0.025, 0.975]).tolist() for k in range(3)]
    main_pass = sum(z["gain"] > 0 for z in fold_report) >= 4 and ci[0] > 0
    shuffle_pass = sum(z["shuffle_gain"] > 0 for z in fold_report) >= 4 and shuffle_ci[0] > 0
    paired_fold_gains = [z["shuffled_logloss"] - z["continuity_logloss"] for z in fold_report]
    paired_pass = sum(v > 0 for v in paired_fold_gains) >= 4 and paired_ci[0] > 0
    natural = (y == 0)
    return frame, {"folds": fold_report, "audit": audits,
                   "zero_rule_logloss": float(loss_zero_rule.mean()),
                   "calibrated_no_geometry_logloss": float(loss_base.mean()),
                   "continuity_logloss": float(loss_main.mean()),
                   "main_gain": float(gain.mean()), "main_county_bootstrap_ci95": ci,
                   "shuffle_gain": float(shuffle_gain.mean()),
                   "shuffle_county_bootstrap_ci95": shuffle_ci,
                   "paired_geometry_gain": float(paired_gain.mean()),
                   "paired_geometry_county_bootstrap_ci95": paired_ci,
                   "paired_geometry_fold_gains": paired_fold_gains,
                   "main_pass": bool(main_pass), "shuffle_pass": bool(shuffle_pass),
                   "paired_geometry_pass": bool(paired_pass),
                   "passes": bool(paired_pass),
                   "natural_empty": int(natural.sum()),
                   "natural_flags": int((natural & (frame.pair_flag.to_numpy() > 0)).sum()),
                   "target_validation": None,
                   "interpretation": "Class-filtered Overture highway presence; external route endpoints only; no inference about hidden reference presence"}
