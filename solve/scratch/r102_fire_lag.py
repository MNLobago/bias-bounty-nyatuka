"""Round 102: does wildfire history shift Overture place inventories in urban tracts?

Hypothesis source: discussion 34910 (reconstruction lag: fewer places in burned urban tracts) versus the
"ghost services" title of discussion 35527 (stale places stay listed). Gate fixed in PLAN.md before fitting.
Reads only supplied strata (MTBS/NIFC flags, RUCA), ACS housing and cached Overture tract counts.
No organiser target, no TIGER / Microsoft / HIFLD / CBP layer.

Run standalone (`python scratch/r102_fire_lag.py` from solve/) or call `run(work_dir)` from solution.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 102
REGIONS = ["maricopa-az", "northern-ca", "eastern-ok", "south-central-tx", "eastern-wa"]
GATE_REGIONS = ["maricopa-az", "northern-ca", "eastern-ok", "south-central-tx"]   # the four regions of 34910
N_BOOT, N_PERM = 2000, 200


def county_folds(groups, n_splits=5):
    """Identical to solution.py's county_folds (R89 folds)."""
    uniq, gidx = np.unique(np.asarray(groups), return_inverse=True)
    sizes = np.bincount(gidx)
    order = np.argsort(sizes, kind="stable")[::-1]
    load, to_fold = np.zeros(n_splits), np.zeros(len(uniq), int)
    for gi in order:
        f = int(np.argmin(load))
        load[f] += sizes[gi]
        to_fold[gi] = f
    return to_fold[gidx]


def load_frame(work_dir: Path) -> pd.DataFrame:
    ext = work_dir.parent / "data" / "external" / "bias-bounty-mapping-equity-challenge"
    cache = work_dir / "cache"
    sample = pd.read_csv(work_dir.parent / "data" / "SampleSubmission.csv", dtype={"GEOID": str}, usecols=["GEOID", "region"])
    parts = []
    for r in REGIONS:
        c = pd.read_parquet(cache / f"{r}__counts__v1.parquet", columns=["GEOID", "bld_n", "poi_all"])
        for k in ("placesrc", "nonres_bld"):
            c = c.merge(pd.read_parquet(cache / f"{r}__{k}__v1.parquet"), on="GEOID", how="left", validate="one_to_one")
        acs = pd.read_parquet(ext / "reference" / r / f"{r}-census-acs-housing.parquet", columns=["GEOID", "housing_units"])
        c = c.merge(acs.drop_duplicates("GEOID"), on="GEOID", how="left", validate="one_to_one")
        st = pd.read_parquet(ext / "strata" / r / f"{r}-strata-tract-table.parquet",
                             columns=["GEOID", "ruca_primary", "mtbs_wildfire_ever", "nifc_wildfire_ever", "mtbs_wildfire_last_year"])
        c = c.merge(st.drop_duplicates("GEOID"), on="GEOID", how="left", validate="one_to_one")
        parts.append(c)
    feats = pd.concat(parts, ignore_index=True).drop_duplicates("GEOID")
    df = sample.merge(feats, on="GEOID", how="left", validate="one_to_one")
    print(f"r102 join: {len(df)} sample tracts; match counts {df.bld_n.notna().mean():.4f}, places-src {df.poi_src_meta.notna().mean():.4f}, "
          f"nonres {df.nonres_tagged_n.notna().mean():.4f}, ACS {df.housing_units.notna().mean():.4f}, strata {df.ruca_primary.notna().mean():.4f}")
    df["county"] = df.GEOID.str[:5]
    df["fold"] = county_folds(df.county.to_numpy(), 5)
    for k in ("mtbs_wildfire_ever", "nifc_wildfire_ever"):
        df[k] = df[k].fillna(False).astype(bool)
    df["urban"] = (df.ruca_primary < 4).astype(float)   # NaN RUCA -> not urban (34910 convention: >= 4 rural)
    df["housing_units"] = df.housing_units.fillna(0.0)
    return df


def demean(x, cidx):
    x = np.asarray(x, float)
    return x - (np.bincount(cidx, weights=x) / np.bincount(cidx))[cidx]


def ols(X, y):
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta


def cluster_se(X, y, beta, cidx):
    e = y - X @ beta
    bread = np.linalg.pinv(X.T @ X)
    s = np.zeros((X.shape[1], X.shape[1]))
    for c in np.unique(cidx):
        m = cidx == c
        g = X[m].T @ e[m]
        s += np.outer(g, g)
    G = len(np.unique(cidx))
    return np.sqrt(np.diag(bread @ s @ bread) * G / max(G - 1, 1))


def heldout(y, Xb, Xa, fold):
    """Per-tract held-out Gaussian log-lik gain (alt - base) and the alt burned coefficients by training split."""
    gain = np.zeros(len(y))
    coefs = []
    for f in range(5):
        tr, te = fold != f, fold == f
        out = []
        for X in (Xb, Xa):
            b = ols(X[tr], y[tr])
            s2 = float(np.mean((y[tr] - X[tr] @ b) ** 2))
            r = y[te] - X[te] @ b
            out.append(-0.5 * (np.log(2 * np.pi * s2) + r ** 2 / s2))
            if X is Xa:
                coefs.append(b[-2:].tolist())   # [burned x urban, burned x rural]
        gain[te] = out[1] - out[0]
    return gain, np.array(coefs)


def cluster_mean_z(g, cidx):
    G = np.unique(cidx)
    sums = np.bincount(cidx, weights=g)[G]
    cnts = np.bincount(cidx)[G]
    m = g.mean()
    se = np.sqrt(np.sum((sums - m * cnts) ** 2)) / len(g)
    return m / se if se > 0 else 0.0


def evaluate(y, base_cols, B, U, cidx, fold, region, rng, boot=True):
    bu, br = demean(B * U, cidx), demean(B * (1 - U), cidx)
    Xb = np.column_stack(base_cols)
    Xa = np.column_stack(base_cols + [bu, br])
    gain, coefs = heldout(y, Xb, Xa, fold)
    sign_bu = np.sign(coefs[:, 0])
    reg_gain = {r: float(gain[region == r].mean()) for r in REGIONS if (region == r).any()}
    res = {"coef_bu_by_split": coefs[:, 0].round(5).tolist(), "coef_br_by_split": coefs[:, 1].round(5).tolist(),
           "sign_consistent": bool(abs(sign_bu.sum()) == 5), "sign": int(np.sign(sign_bu.sum())),
           "gain_by_region": {k: round(v, 6) for k, v in reg_gain.items()},
           "regions_better_of_4": int(sum(reg_gain.get(r, 0) > 0 for r in GATE_REGIONS)),
           "pooled_gain": float(gain.mean()), "cluster_z": float(cluster_mean_z(gain, cidx))}
    if boot:
        uc = np.unique(cidx)
        members = [np.flatnonzero(cidx == c) for c in uc]
        bs = []
        for _ in range(N_BOOT):
            pick = rng.integers(0, len(uc), len(uc))
            ix = np.concatenate([members[p] for p in pick])
            bs.append(gain[ix].mean())
        res["boot_ci95"] = np.quantile(bs, [0.025, 0.975]).round(6).tolist()
        b = ols(Xa, y)
        se = cluster_se(Xa, y, b, cidx)
        res["full_fit"] = {"coef_bu": float(b[-2]), "se_bu": float(se[-2]), "t_bu": float(b[-2] / se[-2]),
                           "coef_br": float(b[-1]), "se_br": float(se[-1]), "t_br": float(b[-1] / se[-1])}
        res["n_burned_urban"] = int((B * U).sum())
        res["n_burned_rural"] = int((B * (1 - U)).sum())
    return res


def perm_passes(r):
    return r["sign_consistent"] and r["regions_better_of_4"] >= 3 and r["cluster_z"] > 1.96


def run(work_dir: Path) -> dict:
    work_dir = Path(work_dir).resolve()
    df = load_frame(work_dir)
    m, o = df.poi_src_meta.to_numpy(float), df.poi_src_other.to_numpy(float)
    k0 = (m >= 10) & (o >= 10)
    per_cty = pd.Series(k0).groupby(df.county.to_numpy()).transform("sum").to_numpy()
    keep = k0 & (per_cty >= 3)
    d = df.loc[keep].reset_index(drop=True)
    cidx = pd.factorize(d.county)[1].get_indexer(d.county)
    fold, region = d.fold.to_numpy(), d.region.to_numpy()
    places = d.poi_all.to_numpy(float)
    nonres = (d.nonres_tagged_n + d.nonres_large_n).to_numpy(float)
    U = d.urban.to_numpy(float)
    y1 = demean(np.log((places + 1) / (nonres + 1)), cidx)
    y2 = demean(np.log(d.poi_src_other.to_numpy(float) / d.poi_src_meta.to_numpy(float)), cidx)
    base_common = [demean(np.log1p(d.housing_units.to_numpy(float)), cidx), demean(np.log1p(d.bld_n.to_numpy(float)), cidx),
                   demean(U, cidx)]
    x94 = demean(np.log((nonres + 1) / (places + 1)), cidx)
    outcomes = {"o1_log_places_per_nonres": (y1, base_common),
                "o2_log_other_over_meta": (y2, [x94] + base_common)}
    print(f"r102 tract set: {len(d)} tracts, {cidx.max() + 1} counties; burned urban MTBS {int((d.mtbs_wildfire_ever * U).sum())}, "
          f"NIFC {int((d.nifc_wildfire_ever * U).sum())}; folds {np.bincount(fold).tolist()}")
    rng = np.random.default_rng(SEED)
    report = {"tract_set": {"tracts": int(len(d)), "counties": int(cidx.max() + 1)}, "results": {}}
    strata = pd.factorize(d.county + "_" + U.astype(int).astype(str))[0]
    for oname, (y, base) in outcomes.items():
        for flag in ("mtbs_wildfire_ever", "nifc_wildfire_ever"):
            B = d[flag].to_numpy(float)
            r = evaluate(y, base, B, U, cidx, fold, region, rng)
            perm_pass, perm_gain = 0, []
            prng = np.random.default_rng([SEED, len(oname), len(flag)])
            for _ in range(N_PERM):
                Bp = B.copy()
                for s in np.unique(strata):
                    ix = np.flatnonzero(strata == s)
                    if len(ix) > 1:
                        Bp[ix] = B[prng.permutation(ix)]
                rp = evaluate(y, base, Bp, U, cidx, fold, region, prng, boot=False)
                perm_pass += perm_passes(rp)
                perm_gain.append(rp["pooled_gain"])
            r["perm_pass_rate"] = perm_pass / N_PERM
            r["perm_p_pooled_gain"] = float((np.sum(np.array(perm_gain) >= r["pooled_gain"]) + 1) / (N_PERM + 1))
            r["criteria"] = {"1_sign_all_splits": r["sign_consistent"],
                             "2_regions_ge3of4": r["regions_better_of_4"] >= 3,
                             "3_boot_ci_excludes_0": bool(r["boot_ci95"][0] > 0),   # gain interval above 0
                             "4_rural_not_significant": abs(r["full_fit"]["t_br"]) < 2,
                             "5_permutation_fails": r["perm_pass_rate"] <= 0.05}
            r["passes_flag"] = all(r["criteria"].values())
            report["results"][f"{oname}|{flag}"] = r
            print(f"r102 {oname} | {flag}: " + json.dumps({k: r[k] for k in ("coef_bu_by_split", "gain_by_region", "pooled_gain",
                                                                            "boot_ci95", "full_fit", "perm_pass_rate", "perm_p_pooled_gain",
                                                                            "criteria", "passes_flag")}))
    for oname in outcomes:
        a, b = report["results"][f"{oname}|mtbs_wildfire_ever"], report["results"][f"{oname}|nifc_wildfire_ever"]
        report["results"][f"{oname}|joint"] = {"same_sign_mtbs_nifc": a["sign"] == b["sign"] and a["sign"] != 0,
                                               "passes": bool(a["passes_flag"] and b["passes_flag"] and a["sign"] == b["sign"])}
    gate = report["results"]["o1_log_places_per_nonres|joint"]["passes"]
    report["gate_passes"] = bool(gate)

    r91 = work_dir / "submission_scored_r91_0p067183.csv"
    cand = work_dir / "scratch" / "r102_candidate.csv"
    if not gate:
        cand.unlink(missing_ok=True)   # no candidate on a failed gate (review R102); R91 stays submission.csv
        report["candidate"] = {"file": None, "decision": "gate fails (criterion 4: burned x rural significant): "
                               "no candidate written; H2 closed for this pipeline"}
        print("r102 gate passes:", report["gate_passes"], "|", report["candidate"])
        return report
    else:
        beta = 0.5 * np.mean([report["results"][f"o1_log_places_per_nonres|{f}"]["full_fit"]["coef_bu"]
                              for f in ("mtbs_wildfire_ever", "nifc_wildfire_ever")])
        sub = pd.read_csv(r91, dtype={"GEOID": str})
        f = df.set_index("GEOID").reindex(sub.GEOID)
        bu = ((f.mtbs_wildfire_ever | f.nifc_wildfire_ever) & (f.urban == 1)).to_numpy()
        old = sub.poi_gap.to_numpy(float)
        new = np.where(bu, np.clip(1 - (1 - old) * np.exp(beta), 0, 1), old)
        sub["poi_gap"] = new
        sub["coverage_gap_score"] = np.clip(sub.coverage_gap_score.to_numpy(float) + (new - old) / 3, 0, 1)
        sub.to_csv(cand, index=False, float_format="%.10f")
        report["candidate"] = {"file": "scratch/r102_candidate.csv", "identical_to_r91": False, "beta_half": float(beta),
                               "tracts_shifted": int(bu.sum()), "rms_move": float(np.sqrt(np.mean(((new - old) / 3) ** 2)))}
    # File checks on the candidate
    s = pd.read_csv(work_dir.parent / "data" / "SampleSubmission.csv", dtype={"GEOID": str})
    c = pd.read_csv(cand, dtype={"GEOID": str})
    gaps = [k for k in c.columns if k.endswith("_gap") or k.startswith("poi_gap") or k == "coverage_gap_score"]
    assert list(c.columns) == list(s.columns) and np.array_equal(c.GEOID.values, s.GEOID.values)
    assert c.notna().all().all() and np.isfinite(c[gaps].to_numpy(float)).all()
    assert ((c[gaps] >= 0) & (c[gaps] <= 1)).all().all()
    report["candidate"]["checks"] = "GEOID order, columns, finite, [0,1]: ok"
    print("r102 gate passes:", report["gate_passes"], "|", report["candidate"])
    return report


if __name__ == "__main__":
    wd = Path(__file__).resolve().parents[1]
    rep = run(wd)
    (wd / "scratch" / "r102_report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
