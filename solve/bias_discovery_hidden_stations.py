"""Bias discovery: fire stations Overture lists outside the `fire_department` category.

Reads only the challenge package (strata, sample submissions) and solution.py's cached per-tract
measurements of the package's Overture POIs. Prints group rates with county-cluster bootstrap CIs.
"""
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ws = Path(sys.argv[1])
cache = ws / "solve" / "cache"
regions = ["maricopa-az", "northern-ca", "eastern-ok", "south-central-tx"]

sample = pd.concat([pd.read_csv(f, dtype={"GEOID": str}) for f in glob.glob(str(ws / "data/external/**/*-sample-submission.csv"), recursive=True)])
strata = pd.concat([pd.read_parquet(f) for f in glob.glob(str(ws / "data/external/**/*-strata-tract-table.parquet"), recursive=True)])
strata["GEOID"] = strata.GEOID.astype(str)
rows = []
for r in regions:
    c = pd.read_parquet(cache / f"{r}__counts__v1.parquet", columns=["GEOID", "poi_fire"])
    x = pd.read_parquet(cache / f"{r}__firenames__v1.parquet")
    rows.append(c.merge(x, on="GEOID").assign(region=r))
d = pd.concat(rows).merge(strata, on="GEOID")
d = d[d.GEOID.isin(sample.GEOID)].copy()
d["county"] = d.GEOID.str[:5]
d["rural"] = d.pct_urban.fillna(0) < 0.5
d["tribal"] = d.tribal_any.fillna(0).astype(bool)
d["svi_q4"] = d.svi_overall >= d.svi_overall.quantile(0.75)
d["invisible"] = (d.poi_fire == 0) & (d.fire_x > 0)   # a category query finds no station, but one is mapped
print("tracts", len(d), "| category stations", int(d.poi_fire.sum()), "| filed elsewhere", int(d.fire_x.sum()))
print("strata columns with fire/hazard:", [c for c in strata.columns if any(k in c.lower() for k in ("wildfire", "whp", "hazard", "fire"))][:12])

rng = np.random.default_rng(0)


def share(g):   # stations filed outside the category, as a share of all mapped stations
    return g.fire_x.sum() / max(g.fire_x.sum() + g.poi_fire.sum(), 1)


def ci(stat, mask_name, B=2000):
    """County-cluster bootstrap of the in-group minus out-group hidden share (sums per county, resampled)."""
    agg = d.groupby(["county", mask_name])[["fire_x", "poi_fire"]].sum().unstack(fill_value=0)
    x_in, x_out = agg["fire_x"].get(True, 0).to_numpy(float), agg["fire_x"].get(False, 0).to_numpy(float)
    f_in, f_out = agg["poi_fire"].get(True, 0).to_numpy(float), agg["poi_fire"].get(False, 0).to_numpy(float)
    w = rng.multinomial(len(agg), np.full(len(agg), 1 / len(agg)), size=B).astype(float)
    s_in = (w @ x_in) / np.maximum(w @ (x_in + f_in), 1)
    s_out = (w @ x_out) / np.maximum(w @ (x_out + f_out), 1)
    return np.percentile(s_in - s_out, [2.5, 97.5])


for name in ("rural", "tribal", "svi_q4"):
    a, b = d[d[name]], d[~d[name]]
    lo, hi = ci(share, name)
    print(f"{name:7s} hidden share {share(a):.3f} vs {share(b):.3f} | diff 95% CI [{lo:.3f}, {hi:.3f}] | "
          f"invisible tracts {int(a.invisible.sum())}/{len(a)} ({a.invisible.mean():.1%}) vs {int(b.invisible.sum())}/{len(b)} "
          f"({b.invisible.mean():.1%}) | residents in invisible tracts {int(a.pop_total[a.invisible].sum()):,} vs {int(b.pop_total[b.invisible].sum()):,}")

print("\nby region:")
for r, g in d.groupby("region"):
    print(f"  {r:17s} hidden share {share(g):.3f} | invisible tracts {int(g.invisible.sum())} | residents {int(g.pop_total[g.invisible].sum()):,}")

d["inter"] = np.select([d.rural & d.tribal, d.rural & ~d.tribal, ~d.rural & d.tribal], ["rural tribal", "rural non-tribal", "urban tribal"], "urban non-tribal")
print("\nintersection:")
for k, g in d.groupby("inter"):
    print(f"  {k:17s} n={len(g):5d} hidden share {share(g):.3f} | invisible {g.invisible.mean():.1%} | residents {int(g.pop_total[g.invisible].sum()):,}")

print("\ncounties with most residents in invisible tracts:")
top = d[d.invisible].groupby(["region", "county"]).agg(tracts=("GEOID", "size"), residents=("pop_total", "sum"), stations=("fire_x", "sum")).sort_values("residents", ascending=False).head(10)
print(top.to_string())
places = pd.concat([pd.read_parquet(cache / f"{r}__firenames__v1_places.parquet").assign(region=r) for r in regions])
places = places[places.GEOID.isin(sample.GEOID)]
print("\ncategory of the hidden stations:", places.category.value_counts().head(6).to_dict())
inv = places[places.GEOID.isin(d.GEOID[d.invisible])].merge(d[["GEOID", "pop_total", "tribal", "svi_overall"]], on="GEOID")
print("\nexamples in invisible tracts (largest population):")
print(inv.sort_values("pop_total", ascending=False).head(12)[["GEOID", "region", "name", "category", "pop_total", "tribal", "svi_overall"]].to_string(index=False))
d[["GEOID", "region", "county", "poi_fire", "fire_x", "invisible", "rural", "tribal", "svi_overall", "pop_total"]].to_csv(sys.argv[2], index=False)
