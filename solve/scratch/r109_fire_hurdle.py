"""Provider-disjoint fire observation diagnostic; called by solution.py after the scored file is selected."""
import json
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pyarrow.compute as pc
from scipy.special import gammaln, expit
from sklearn.linear_model import LogisticRegression, PoissonRegressor
from sklearn.preprocessing import StandardScaler


def run_fire_hurdle(g):
    # Never use a record's category or name to predict that same record as an outcome.
    df, regions = g["R109_FEATURE_FRAME"], g["REGIONS"]
    rows = []
    for region in regions:
        path = g["PATHS"][region]["pois"]
        t = pq.read_table(path, columns=["names", "categories", "sources", "geometry"])
        cat = pc.struct_field(t["categories"].combine_chunks(), "primary").fill_null("").to_numpy(zero_copy_only=False)
        names = pc.struct_field(t["names"].combine_chunks(), "primary").fill_null("").to_numpy(zero_copy_only=False)
        iscat = np.asarray([c == "fire_department" for c in cat], dtype=bool)
        isname = np.asarray([bool(g["FIRE_STATION_NAME"].search(s)) and not g["FIRE_NOT_STATION"].search(s) for s in names])
        relevant = iscat | isname
        src = t["sources"].combine_chunks()
        datasets = pc.struct_field(pc.list_flatten(src), "dataset").fill_null("").to_numpy(zero_copy_only=False)
        parent = pc.list_parent_indices(src).to_numpy()
        meta = np.bincount(parent[datasets == "meta"], minlength=len(t)) > 0
        # A place with Meta and another provider belongs to Meta only; no shared records cross the split.
        keep = np.flatnonzero(relevant)
        tr = pq.read_table(g["PATHS"][region]["tracts"], columns=["GEOID", "geometry"])
        idx = g["TractIndex"](tr["geometry"].to_pylist())
        x, y = g["wkb_points_xy"](t["geometry"].take(keep))
        assigned = idx.assign(x, y)
        ids = np.asarray(tr["GEOID"].to_pylist(), dtype=object)
        ok = assigned >= 0
        block = pd.DataFrame({"GEOID": ids[assigned[ok]], "meta": meta[keep][ok],
                              "category": iscat[keep][ok], "name": isname[keep][ok]})
        agg = block.groupby("GEOID", sort=False).apply(lambda v: pd.Series({
            "heldout_fire": int((~v["meta"].to_numpy(bool) & v["category"].to_numpy(bool)).sum()),
            "meta_category": int((v["meta"].to_numpy(bool) & v["category"].to_numpy(bool)).sum()),
            "meta_name": int((v["meta"].to_numpy(bool) & v["name"].to_numpy(bool) & ~v["category"].to_numpy(bool)).sum()),
            "meta_provider": int(v["meta"].to_numpy(bool).sum())}), include_groups=False).reset_index()
        rows.append(agg)
    z = df[["GEOID", "county", "region", "pop_total", "housing_units", "area_km2", "pct_urban", "ruca_primary"]].merge(
        pd.concat(rows, ignore_index=True), on="GEOID", how="left", validate="one_to_one")
    for col in ("heldout_fire", "meta_category", "meta_name", "meta_provider"):
        z[col] = z[col].fillna(0).astype(int)
    target = z.heldout_fire.to_numpy(int)
    demo = np.column_stack([np.log1p(z.pop_total), np.log1p(z.housing_units),
                            np.log(np.maximum(z.area_km2, 1e-3)), z.pct_urban,
                            pd.to_numeric(z.ruca_primary, errors="coerce").fillna(10).to_numpy()])
    channels = np.column_stack([(z.meta_category > 0).to_numpy(int),
                                (z.meta_name > 0).to_numpy(int),
                                (z.meta_provider > 0).to_numpy(int)])
    fold_pairs = g["county_folds"](z.county.to_numpy(), 5)

    def nb_logpmf(y, mu, k):
        return gammaln(y + k) - gammaln(k) - gammaln(y + 1) + k * np.log(k / (k + mu)) + y * np.log(mu / (k + mu))

    def positive_ll(y, conditional_mean, k):
        # Solve E[Y|Y>0] of NB(k,mu) for its latent mu, then use the truncated NB mass.
        mu = np.maximum(conditional_mean - 0.5, 1e-5).astype(float)
        for _ in range(20):
            p0 = (k / (k + mu)) ** k
            fun = mu / np.maximum(1 - p0, 1e-12) - conditional_mean
            deriv = 1 / (1 - p0) - mu * k * p0 / ((k + mu) * np.maximum((1 - p0) ** 2, 1e-12))
            mu = np.maximum(mu - fun / np.maximum(deriv, 1e-6), 1e-7)
        p0 = (k / (k + mu)) ** k
        return nb_logpmf(y, mu, k) - np.log1p(-p0)

    gains, controls, base_ll, hurdle_ll = [], [], [], []
    rng = np.random.default_rng(20261028)
    for train, test in fold_pairs:
        scale = StandardScaler().fit(demo[train])
        xb, xv = scale.transform(demo[train]), scale.transform(demo[test])
        ytr, yte = target[train], target[test]
        base = PoissonRegressor(alpha=0.03, max_iter=500).fit(xb, ytr)
        mu = np.maximum(base.predict(xv), 1e-8)
        kgrid = (0.5, 1, 2, 5, 20, 200)
        k = max(kgrid, key=lambda kk: nb_logpmf(ytr, np.maximum(base.predict(xb), 1e-8), kk).mean())
        lb = nb_logpmf(yte, mu, k).mean()
        base_ll.append(float(lb))

        def fit_hurdle(ch):
            xtr = np.column_stack([xb, ch[train]])
            xte = np.column_stack([xv, ch[test]])
            occ = LogisticRegression(C=0.5, max_iter=500).fit(xtr, ytr > 0)
            p = np.clip(occ.predict_proba(xte)[:, 1], 1e-7, 1 - 1e-7)
            positive = ytr > 0
            count = PoissonRegressor(alpha=0.1, max_iter=500).fit(xtr[positive], ytr[positive])
            cm = np.maximum(count.predict(xte), 1.001)
            ll = np.where(yte > 0, np.log(p), np.log1p(-p))
            pos = yte > 0
            ll[pos] += positive_ll(yte[pos], cm[pos], 3.0)
            return float(ll.mean())

        lh = fit_hurdle(channels)
        hurdle_ll.append(lh)
        gains.append(lh - lb)
        shuffled = channels.copy()
        # Shuffle the name channel within county on the *whole* table. County holdout stays intact.
        for _, ix in z.groupby("county").indices.items():
            shuffled[ix, 1] = rng.permutation(shuffled[ix, 1])
        controls.append(fit_hurdle(shuffled) - lb)
    gains, controls = np.asarray(gains), np.asarray(controls)
    se = float(gains.std(ddof=1) / np.sqrt(5))
    result = {"proxy": "non-Meta fire-category count predicted from disjoint Meta category, name and source channels",
              "counts": {"rows": len(z), "heldout_positive": int((target > 0).sum()),
                         "meta_category_positive": int((channels[:, 0] > 0).sum()),
                         "meta_name_positive": int((channels[:, 1] > 0).sum())},
              "baseline_ll": base_ll, "hurdle_ll": hurdle_ll,
              "gain_by_fold": gains.tolist(), "mean_gain": float(gains.mean()), "se": se,
              "folds_better": int((gains > 0).sum()),
              "name_shuffle_gain_by_fold": controls.tolist(),
              "name_shuffle_mean_gain": float(controls.mean()),
              "passes": bool((gains > 0).sum() >= 4 and gains.mean() > 2 * se and controls.mean() <= 0),
              "target_validation": None,
              "limitation": "Overture provider-inventory likelihood does not validate hidden USGS fire reference counts."}
    return result
