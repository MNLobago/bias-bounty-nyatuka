# Bias Bounty: Mapping Equity Challenge — code for team Nyatuka

Code behind my entry to the Zindi [Bias Bounty: Mapping Equity Challenge](https://zindi.world/competitions/bias-bounty-mapping-equity-challenge) (team Nyatuka, a team of one). It rebuilds:

- **the methodology submission** `PZvvofKQ` (public RMSE 0.071164771), the model-only file described in my writeup;
- **the calibrated final** `KnVCJ2bt` (public RMSE 0.066973971), the same model plus a layer fitted to my own public scores. It is disclosed as an explored step, not part of the method;
- **the Best Bias Discovery analysis**: Overture fire stations filed outside the `fire_department` category.

The method is explained in the methodology writeup submitted with the entry. In short, each tract's prediction is the expected value of the organisers' published coverage-gap formula. Every withdrawn reference count is replaced by a probability distribution built only from the challenge package, and the expectation is taken over 16,000 seeded Monte Carlo draws.

## Layout

```
solve/
  solution.py                  the whole pipeline (a percent-format notebook script)
  road_continuity.py           helper modules imported by solution.py
  r104_road_continuity.py
  transport_tiers.py
  junction_probe.py
  scratch/                     three diagnostic scripts that solution.py runs
  anchors/                     earlier scored files the script builds on, SHA-256 in MANIFEST.json
  submission_*.csv, *.json     18 earlier outputs and 2 reports the script reads back (traced; nothing else)
  bias_discovery_hidden_stations.py
  bias_discovery_map.py
submissions/                   three exact uploads (R91, R113, R116) the script reads back to check values
archive/                       every scored submission, with a table (a record, not a tuning input)
data/                          not included: put the challenge package here (see below)
```

**What depends on what.**
- The methodology file needs the code, the data and one earlier model-only output (`solve/anchors/`, scored as `jhDQXakK`, itself produced by an earlier version of this script).
- The calibrated file needs more: it is built from the exact scored files in `solve/anchors/`, because its steps were fitted to those files' scores.

## 1. Get the data

The challenge data is not redistributed here. Download it from the challenge's Data page and arrange it like this, next to `solve/`:

```
data/
  SampleSubmission.csv
  external/bias-bounty-mapping-equity-challenge/
    reference/<region>/<region>-census-tracts.parquet, -overture-roads.parquet, -overture-pois.parquet, ...
    strata/<region>/<region>-strata-tract-table.parquet
    boundaries/...
```

`<region>` is `maricopa-az`, `northern-ca`, `eastern-ok`, `south-central-tx` and `eastern-wa`.

On Windows, clone into a short path such as `C:\bb\`: the deepest data paths are long, and past Windows' 260-character limit Python reports those files as missing. The script checks that every input file is present and stops with the list of any that are missing. To keep the data elsewhere, set `COS_DATA_DIR` (the folder that holds `reference/`) and `COS_SAMPLE_PATH`. The three scripts in `scratch/` always read `data/` next to `solve/`, so the layout above is the simplest choice.

## 2. Install

Python 3.12 and the pinned packages:

```
pip install -r requirements.txt
```

No geospatial library is needed: WKB parsing and point-in-polygon are written in numpy.

## 3. Run

```
cd solve
python solution.py
```

- **Runtime.** From an empty `cache/` the first run takes a long time (35 minutes or more on a laptop CPU), because the Monte Carlo accumulators are built from scratch. Later runs reuse `cache/` and take about 5 minutes.
- **Outputs:**
  - `submission_model_only_r77_cbp_business.csv`, the methodology submission. Expected SHA-256: `3524c53e545552f906544a37cbebce7c30f47a090c2137890b1bf0516be5376f`.
  - `submission.csv`, the calibrated final (round 118). Its values equal the uploaded `KnVCJ2bt`; the bytes can differ in number formatting.
  - `cos_report.json`, a summary of every check and diagnostic.
- **What the script asserts:** every anchor matches its SHA-256 in `anchors/MANIFEST.json`, the output has the sample's rows and columns in order with GEOIDs read as text, and all values are finite and in [0, 1].

## 4. The Bias Discovery analysis

Run it after `solution.py`, which caches the per-tract counts it reads:

```
cd solve
python bias_discovery_hidden_stations.py .. bias_discovery_hidden_stations_by_tract.csv
python bias_discovery_map.py .. bias_discovery_hidden_stations_by_tract.csv hidden_stations_map.png
```

The first script prints every number in the discovery section of the writeup: group shares with county-cluster bootstrap intervals, the 316 tracts, residents, counties and named examples. It also writes the per-tract table. The second draws the map.

## Rules and data use

- No withdrawn reference layer (TIGER, Microsoft footprints, USGS facilities, CBP) or copy of one is read. Neither are the strata columns derived from building-footprint datasets.
- Only open-source tools are used, and no AutoML.
- The calibrated final uses my own aggregate public scores. These are listed in the script's `LEADERBOARD_PUBLIC_RMSE*` tables, so a rebuild needs no leaderboard access. The organisers have not said whether that practice is allowed, so the model-only file is documented as the methodology submission.
- Since 29 October 2026, no parameter, weight, step or adoption decision is set from public scores. All scored submissions are kept in `archive/` as a record.
