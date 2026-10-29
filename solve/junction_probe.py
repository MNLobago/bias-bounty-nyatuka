"""Bounded, label-free Overture road-junction / reserved-place experiment."""
from collections import Counter
import hashlib
import json
import struct
import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq
from scipy.special import gammaln
from sklearn.linear_model import PoissonRegressor
from sklearn.preprocessing import StandardScaler


FORBIDDEN = ("tiger", "hifld", "cbp", "census county business", "microsoft")


def _source_masks(array):
    s = array.combine_chunks()
    f = pc.list_flatten(s)
    parents = pc.list_parent_indices(s).to_numpy()
    dataset = pc.struct_field(f, "dataset").fill_null("").to_numpy(zero_copy_only=False)
    provider = pc.struct_field(f, "provider").fill_null("").to_numpy(zero_copy_only=False)
    bad = np.zeros(len(s), bool)
    fs = np.zeros(len(s), bool)
    for i, d, p in zip(parents, dataset, provider):
        z = (str(d) + " " + str(p)).lower()
        bad[i] |= any(q in z for q in FORBIDDEN)
        fs[i] |= "foursquare" in z
    return fs & ~bad, ~fs & ~bad, bad


def _first_last(wkb):
    if wkb is None or len(wkb) < 41 or wkb[0] != 1 or struct.unpack_from('<I', wkb, 1)[0] != 2:
        return None
    n = struct.unpack_from('<I', wkb, 5)[0]
    if n < 2:
        return None
    return struct.unpack_from('<dd', wkb, 9), struct.unpack_from('<dd', wkb, 9 + 16 * (n - 1))


def _selected(df):
    out = {}
    for region, g in df.groupby('region'):
        if region == 'eastern-wa':
            continue
        counts = g.groupby('county').size().sort_values(ascending=False)
        kth = counts.head(10).index.astype(str).tolist()
        out[region] = kth
    return out


def measure(paths, df, cache_dir, tract_index, wkb_points_xy):
    selected = _selected(df)
    frames, audit = [], {}
    for region, counties in selected.items():
        f = cache_dir / f'{region}__junction_probe_v1.parquet'
        if f.exists():
            frames.append(pd.read_parquet(f))
            audit[region] = {'counties': counties, 'cached': True}
            continue
        tract = pq.read_table(paths[region]['tracts'], columns=['GEOID', 'geometry'])
        geoids = np.asarray(tract['GEOID'].to_pylist(), dtype=str)
        ti = tract_index(tract['geometry'].to_pylist())
        keep = np.isin(geoids, df.loc[(df.region == region) & df.county.astype(str).isin(counties), 'GEOID'].to_numpy(str))
        box = ti.bbox[keep]
        bounds = (box[:, 0].min(), box[:, 1].min(), box[:, 2].max(), box[:, 3].max())

        pt = pq.read_table(paths[region]['pois'], columns=['sources', 'geometry'])
        fs, nonfs, bad = _source_masks(pt['sources'])
        px, py = wkb_points_xy(pt['geometry'])
        a = ti.assign(px, py)
        fs_count = np.bincount(a[(a >= 0) & fs], minlength=len(geoids))
        nonfs_count = np.bincount(a[(a >= 0) & nonfs], minlength=len(geoids))

        road = pq.ParquetFile(paths[region]['roads'].with_name(f'{region}-overture-roads-unfiltered.parquet'))
        degree = Counter()
        coords = {}
        eligible = 0
        for batch in road.iter_batches(batch_size=40000, columns=['connectors', 'bbox', 'geometry']):
            midx = (pc.struct_field(batch['bbox'], 'xmin').to_numpy() + pc.struct_field(batch['bbox'], 'xmax').to_numpy()) / 2
            midy = (pc.struct_field(batch['bbox'], 'ymin').to_numpy() + pc.struct_field(batch['bbox'], 'ymax').to_numpy()) / 2
            coarse = (midx >= bounds[0]) & (midx <= bounds[2]) & (midy >= bounds[1]) & (midy <= bounds[3])
            ids = np.flatnonzero(coarse)
            if not len(ids):
                continue
            assigned = ti.assign(midx[ids], midy[ids])
            ids = ids[(assigned >= 0) & keep[np.maximum(assigned, 0)]]
            cs = batch['connectors'].to_pylist()
            gs = batch['geometry'].to_pylist()
            for j in ids:
                ends = _first_last(gs[j])
                if ends is None:
                    continue
                eligible += 1
                for c in cs[j] or []:
                    at = c.get('at')
                    if at is None or (abs(at) > 1e-6 and abs(at - 1) > 1e-6):
                        continue
                    key = c.get('connector_id')
                    if not key:
                        continue
                    degree[key] += 1
                    coords.setdefault(key, ends[0] if at < .5 else ends[1])
        nodes = [coords[k] for k, d in degree.items() if d >= 3]
        if not nodes:
            raise RuntimeError(f'No reliable junctions in {region}')
        jx, jy = np.asarray(nodes).T
        ja = ti.assign(jx, jy)
        junctions = np.bincount(ja[ja >= 0], minlength=len(geoids))
        frame = pd.DataFrame({'GEOID': geoids[keep], 'reserved_fs': fs_count[keep],
                              'mapped_nonfs': nonfs_count[keep], 'junctions': junctions[keep]})
        frame.to_parquet(f, index=False)
        frames.append(frame)
        audit[region] = {'counties': counties, 'cached': False, 'road_segments': eligible,
                         'junctions': len(nodes), 'fs_places': int(fs_count[keep].sum()),
                         'nonfs_places': int(nonfs_count[keep].sum()), 'excluded_provenance': int(bad.sum())}
        print('junction measurement', region, json.dumps(audit[region]))
    return pd.concat(frames, ignore_index=True), audit


def _features(d, with_junction, shuffled=False):
    cols = [np.log1p(d.mapped_nonfs.to_numpy()), np.log1p(d.housing_units.to_numpy()),
            np.log1p(d.housing_units.to_numpy() / d.area_km2.to_numpy()),
            d.pct_urban.to_numpy()]
    if with_junction:
        j = d.shuffled_junctions if shuffled else d.junctions
        cols.append(np.log1p(j.to_numpy() / d.area_km2.to_numpy()))
    return np.column_stack(cols)


def _fit_predict(train, test, with_junction, shuffled=False):
    xtr = _features(train, with_junction, shuffled)
    xte = _features(test, with_junction, shuffled)
    scale = StandardScaler().fit(xtr)
    model = PoissonRegressor(alpha=0.1, max_iter=300, tol=1e-7)
    model.fit(scale.transform(xtr), train.reserved_fs.to_numpy())
    return np.maximum(model.predict(scale.transform(xte)), 1e-9), model.coef_[-1] if with_junction else None


def _ll(y, mu):
    return y * np.log(mu) - mu - gammaln(y + 1)


def validate(measured, df):
    d = measured.merge(df[['GEOID', 'region', 'county', 'housing_units', 'area_km2', 'pct_urban']], on='GEOID', validate='one_to_one')
    d = d[d.region != 'eastern-wa'].copy()
    d['county'] = d.county.astype(str)
    if d.reserved_fs.sum() < 100 or (d.junctions > 0).mean() < .5:
        return {'passes': False, 'reason': 'reserved inventory or junction extraction insufficient',
                'tracts': len(d), 'reserved_count': int(d.reserved_fs.sum()),
                'junction_nonzero_share': float((d.junctions > 0).mean())}
    rng = np.random.default_rng(20261027)
    d['shuffled_junctions'] = d.junctions.to_numpy()
    d['housing_bin'] = d.groupby('county').housing_units.transform(lambda s: pd.qcut(s.rank(method='first'), min(3, len(s)), labels=False, duplicates='drop'))
    d['urban_bin'] = (d.pct_urban >= .5).astype(int)
    for _, ix in d.groupby(['county', 'housing_bin', 'urban_bin']).groups.items():
        vals = d.loc[ix, 'shuffled_junctions'].to_numpy().copy()
        rng.shuffle(vals)
        d.loc[ix, 'shuffled_junctions'] = vals
    rows = []
    coefs = []
    for region in sorted(d.region.unique()):
        tr, te = d[d.region != region], d[d.region == region]
        base, _ = _fit_predict(tr, te, False)
        main, coef = _fit_predict(tr, te, True)
        sham, _ = _fit_predict(tr, te, True, shuffled=True)
        coefs.append(float(coef))
        y = te.reserved_fs.to_numpy()
        rows.append(pd.DataFrame({'region': region, 'county': te.county.to_numpy(),
                                  'gain': _ll(y, main) - _ll(y, base),
                                  'shuffle_gain': _ll(y, sham) - _ll(y, base)}))
    v = pd.concat(rows, ignore_index=True)
    by_region = v.groupby('region')[['gain', 'shuffle_gain']].mean().round(8).to_dict('index')
    counties = [g for _, g in v.groupby(['region', 'county'])]
    boot = np.empty((2000, 2))
    for b in range(len(boot)):
        chosen = rng.integers(0, len(counties), len(counties))
        a = np.concatenate([counties[i][['gain', 'shuffle_gain']].to_numpy() for i in chosen])
        boot[b] = a.mean(axis=0)
    ci = np.quantile(boot, [.025, .975], axis=0)
    reg = v.groupby('region')[['gain', 'shuffle_gain']].mean()
    main_pass = bool((reg.gain > 0).sum() >= 3 and ci[0, 0] > 0)
    shuffle_pass = bool((reg.shuffle_gain > 0).sum() >= 3 and ci[0, 1] > 0)
    return {'passes': main_pass and not shuffle_pass, 'main_pass': main_pass,
            'shuffle_pass': shuffle_pass, 'by_region': by_region,
            'pooled_gain': float(v.gain.mean()), 'pooled_shuffle_gain': float(v.shuffle_gain.mean()),
            'county_bootstrap_ci95': ci.tolist(), 'fold_junction_coefficients': coefs,
            'tracts': len(v), 'counties': len(counties), 'reserved_count': int(d.reserved_fs.sum()),
            'junction_nonzero_share': float((d.junctions > 0).mean())}
