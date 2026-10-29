"""Round 103 H2 diagnostic: which Overture place inventory carries the burned-tract deficit?

R102 found burned tracts list fewer places per nonresidential building (outcome o1). R103 assumes the organisers'
CBP establishment reference does NOT share that deficit (an Overture listing lag). Overture places carry their
contributing inventory: Meta records versus places only Microsoft / BrightQuery / Foursquare contributed ("other",
the more registry-like inventory). If the deficit is as large in "other" as in Meta, a registry such as CBP plausibly
shares it (a real establishment deficit) and R103 over-states the gap; if it sits mostly in Meta, a listing lag is
the better reading. Same tract set, covariates and R89 county folds as R102; one pooled burned term (urban + rural).
Reads only supplied strata, ACS and cached Overture counts. No organiser target, no withdrawn reference layer.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _r102(work_dir: Path):
    spec = importlib.util.spec_from_file_location("r102_fire_lag", work_dir / "scratch" / "r102_fire_lag.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(work_dir: Path) -> dict:
    work_dir = Path(work_dir).resolve()
    r = _r102(work_dir)
    df = r.load_frame(work_dir)
    m, o = df.poi_src_meta.to_numpy(float), df.poi_src_other.to_numpy(float)
    k0 = (m >= 10) & (o >= 10)
    keep = k0 & (pd.Series(k0).groupby(df.county.to_numpy()).transform("sum").to_numpy() >= 3)
    d = df.loc[keep].reset_index(drop=True)
    cidx = pd.factorize(d.county)[1].get_indexer(d.county)
    fold = d.fold.to_numpy()
    nonres = (d.nonres_tagged_n + d.nonres_large_n).to_numpy(float)
    U = d.urban.to_numpy(float)
    base = [r.demean(np.log1p(d.housing_units.to_numpy(float)), cidx), r.demean(np.log1p(d.bld_n.to_numpy(float)), cidx),
            r.demean(U, cidx)]
    outcomes = {"meta_per_nonres": np.log((d.poi_src_meta.to_numpy(float) + 1) / (nonres + 1)),
                "other_per_nonres": np.log((d.poi_src_other.to_numpy(float) + 1) / (nonres + 1)),
                "all_per_nonres": np.log((d.poi_all.to_numpy(float) + 1) / (nonres + 1))}
    out = {"tracts": int(len(d)), "counties": int(cidx.max() + 1), "results": {}}
    for flag in ("mtbs_wildfire_ever", "nifc_wildfire_ever"):
        B = r.demean(d[flag].to_numpy(float), cidx)
        Xb, Xa = np.column_stack(base), np.column_stack(base + [B])
        for name, y in outcomes.items():
            y = r.demean(y, cidx)
            gain, coefs = np.zeros(len(y)), []
            for f in range(5):
                tr, te = fold != f, fold == f
                ll = []
                for X in (Xb, Xa):
                    b = r.ols(X[tr], y[tr])
                    s2 = float(np.mean((y[tr] - X[tr] @ b) ** 2))
                    res = y[te] - X[te] @ b
                    ll.append(-0.5 * (np.log(2 * np.pi * s2) + res ** 2 / s2))
                    if X is Xa:
                        coefs.append(float(b[-1]))
                gain[te] = ll[1] - ll[0]
            b = r.ols(Xa, y)
            se = r.cluster_se(Xa, y, b, cidx)
            out["results"][f"{name}|{flag}"] = {"coef_by_split": np.round(coefs, 5).tolist(), "coef_full": float(b[-1]),
                                                "se_full": float(se[-1]), "heldout_gain": float(gain.mean()),
                                                "cluster_z": float(r.cluster_mean_z(gain, cidx))}
    for flag in ("mtbs_wildfire_ever", "nifc_wildfire_ever"):
        cm = out["results"][f"meta_per_nonres|{flag}"]["coef_full"]
        co = out["results"][f"other_per_nonres|{flag}"]["coef_full"]
        out["results"][f"share_of_deficit_in_other|{flag}"] = float(co / (cm + co)) if (cm + co) != 0 else None
    return out


if __name__ == "__main__":
    wd = Path(__file__).resolve().parents[1]
    rep = run(wd)
    print(json.dumps(rep, indent=1))
    (wd / "scratch" / "r103_inventory_split.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
