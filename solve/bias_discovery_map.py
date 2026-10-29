"""Map of tracts where a `fire_department` search finds no station but one is mapped under another category."""
import glob
import struct
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from matplotlib.collections import PolyCollection

ws, table, out = sys.argv[1], sys.argv[2], sys.argv[3]
d = pd.read_csv(table, dtype={"GEOID": str})
hidden = set(d.GEOID[d.invisible])
rural = set(d.GEOID[d.rural])


def rings(wkb):
    """Exterior rings of a little-endian WKB Polygon / MultiPolygon, as (n, 2) arrays."""
    out, pos = [], 0

    def polygon(pos):
        (n_rings,) = struct.unpack_from("<I", wkb, pos)
        pos += 4
        for i in range(n_rings):
            (n,) = struct.unpack_from("<I", wkb, pos)
            pos += 4
            xy = np.frombuffer(wkb, "<f8", 2 * n, pos).reshape(n, 2)
            pos += 16 * n
            if i == 0:
                out.append(xy)
        return pos

    order, kind = struct.unpack_from("<BI", wkb, 0)
    assert order == 1, "big-endian WKB"
    pos = 5
    if kind % 1000 == 3:
        polygon(pos)
    elif kind % 1000 == 6:
        (n,) = struct.unpack_from("<I", wkb, pos)
        pos += 4
        for _ in range(n):
            pos = polygon(pos + 5)
    return out


titles = {"maricopa-az": "Maricopa County, AZ", "northern-ca": "Northern California",
          "eastern-ok": "Eastern Oklahoma", "south-central-tx": "South-Central Texas"}
fig, axes = plt.subplots(2, 2, figsize=(11, 9.2), dpi=180)
for ax, r in zip(axes.flat, titles):
    f = glob.glob(f"{ws}/data/external/**/{r}-census-tracts.parquet", recursive=True)[0]
    t = pq.read_table(f, columns=["GEOID", "geometry"]).to_pydict()
    base, rur, hid = [], [], []
    for g, w in zip(t["GEOID"], t["geometry"]):
        if g not in set(d.GEOID[d.region == r]):
            continue
        polys = rings(w)
        (hid if g in hidden else rur if g in rural else base).extend(polys)
    ax.add_collection(PolyCollection(base, facecolor="#e6e6e6", edgecolor="white", linewidth=0.15))
    ax.add_collection(PolyCollection(rur, facecolor="#c9c9c9", edgecolor="white", linewidth=0.15))
    ax.add_collection(PolyCollection(hid, facecolor="#d1495b", edgecolor="#8c1c2b", linewidth=0.3))
    ax.autoscale_view()
    ax.set_aspect(1 / np.cos(np.deg2rad(np.mean(ax.get_ylim()))))
    n = int(d[(d.region == r) & d.invisible].shape[0])
    people = int(d[(d.region == r) & d.invisible].pop_total.sum())
    ax.set_title(f"{titles[r]}: {n} tracts, {people:,} residents", fontsize=10.5, loc="left")
    ax.set_axis_off()
fig.suptitle("Where a fire_department search finds no station, but one is mapped under another category",
             fontsize=12.5, x=0.02, ha="left", y=0.985)
handles = [plt.Rectangle((0, 0), 1, 1, fc=c) for c in ("#d1495b", "#c9c9c9", "#e6e6e6")]
fig.legend(handles, ["station hidden by its category", "rural tract", "urban tract"], loc="lower center",
           bbox_to_anchor=(0.5, 0.035), ncol=3, frameon=False, fontsize=9.5)
fig.text(0.02, 0.008, "Source: challenge package (Overture POIs, census tracts, strata table); analysis "
         "bias_discovery_hidden_stations.py", fontsize=7.5, color="#666666")
fig.tight_layout(rect=(0, 0.08, 1, 0.96))
fig.savefig(out, facecolor="white")
print("wrote", out)
