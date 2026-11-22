# S2-GRiTS

**English** | [简体中文](README.zh-CN.md)

Download Sentinel-2 Level-2A scenes from Element 84 Earth Search and build
incremental, native-resolution Zarr archives indexed by MGRS tile. Each resolution
group has its own grid and a canonical Parquet catalog compatible with S1-GRiTS
schema v7. [GRiTS-Resolver](https://github.com/ottoKae/GRiTS-Resolver) provides
downstream reading, explicit temporal joins and reprojection when needed.

The default collection is `sentinel-2-c1-l2a`. The native ingestion path copies
source stored values without applying reflectance scale/offset, harmonization or
resampling. Recording source scale/offset metadata does **not** mean applying it.
Upstream processing and historical production must be established from their own
evidence, not inferred from the current source code.

## Install and run

Python **3.12** is required (`>=3.12,<3.13`). From a repository checkout:

```sh
python -m venv .venv
# Activate: .venv\Scripts\Activate.ps1 (PowerShell) or source .venv/bin/activate (bash)
python -m pip install -e .
```

Alternatively, use `conda env create -f environment.yml`, then
`conda activate py312_s2grits` and `python -m pip install -e .`.

Start with the [one-scene configuration](config/ecuador_17mpu_native_test.yaml).
Copy it into `config/local/`, choose your tile, dates and output directory, and run:

```sh
s2grits doctor --config config/local/my-scene.yaml --network
s2grits process_scenes --config config/local/my-scene.yaml
```

The second command **downloads data**. A full native tile can be large even for
one scene. `cloud_cover_max` filters scene metadata; it is not a pixel cloud mask.
The example disables monthly compositing and cloud masking. Use an independent
output root for a trial, and inspect the command's exit status and catalog.

## Native bands and storage

Configuration uses Earth Search asset keys. Zarr uses the following variable
names, with `source_band` and `source_asset` attributes preserving their identity:

| Group | Sentinel-2 bands / quality layers | Zarr variables |
| --- | --- | --- |
| `10m` | B02, B03, B04, B08 | `blue`, `green`, `red`, `nir_broad` |
| `20m` | B05, B06, B07, B8A, B11, B12 | `red_edge_1`, `red_edge_2`, `red_edge_3`, `nir_narrow`, `swir_1`, `swir_2` |
| `20m` | SCL, cloud probability | `scl`, `cloud_probability` |
| `60m` | B01, B09 | `coastal`, `water_vapor` |

Only requested assets are written. B08 and B8A remain distinct. Arrays have
dimensions `(time, y, x)` on their native grids; do not directly merge the groups
as if their pixel coordinates were identical.

```text
dataset/
  catalog.parquet
  TILE/
    catalog.parquet
    scenes_L2A_native/zarr/s2grits_scenes_TILE_L2A_native.zarr/
      s2grits_resume_state.json
      10m/...
      20m/...
      60m/...                    # if requested
  provenance/...                # if enabled
```

New arrays use Zarr v3, Blosc/Zstd level 5, byte shuffle and nominal
`(1,512,512)` chunks. Compression is lossless. Existing arrays retain their
recorded codec on resume; editing metadata alone cannot recompress them.
Logs and cache default to `../logs` and `../_cache`, resolved relative to
`output.base_dir`. Native downloading may use temporary TIFFs before writing
Zarr; `formats.cog: false` does not mean no source cache is used.

## Optional SCL companion

SCL (Scene Classification Layer) is a per-scene categorical quality layer,
**not reflectance, cloud probability or a land-cover training label**.
For a new archive, request `scl` in `assets.bands`; see the
[ten-band plus SCL example](config/tessera10_scl_native.example.yaml).

For an existing spectral archive, keep SCL in an **independent quality Zarr**:
`20m/scl(time,y,x)`, native 20 m, `uint8`, categories 0–11. Do not add bands to
an existing spectral store using ordinary resume.

1. Prepare a frozen scene manifest and source-asset manifest matching the existing
   archive. Fill the paths and digests in the
   [quality job template](config/quality_companion.example.yaml). Manifest
   creation is not automatic; placeholder digests are not valid production inputs.
2. Use Python **3.12.13** and the dedicated
   [environment lock](config/quality_environment.lock.json):
   `python -m pip install -r config/requirements-quality.txt -e ".[dev]"`.
3. Prepare the [pipeline configuration](config/pipeline_scl.example.yaml):

```yaml
workflow: pipeline
spectral:
  root: D:/data/S2-native
  job_config: null  # reuse a completed spectral archive
companions:
  scl:
    enabled: true  # false skips companion production; it does not delete data
    job_config: ./scl_companion.yaml
```

```sh
s2grits process_pipeline --config config/local/pipeline_scl.yaml --dry-run
s2grits process_pipeline --config config/local/pipeline_scl.yaml --max-new-scenes 8
```

Parent and companion job paths are resolved relative to their respective YAML
files. Dry-run checks identities and configuration, not live HTTPS transport.
The second command performs real downloads. With unchanged bindings, reruns skip
accepted scenes. `partial` means that the frozen selection is not yet complete.
For a new spectral archive, first run with the companion disabled, freeze the
completed selection, then enable the companion with `spectral.job_config: null`.

Each SCL scene is reread from disk and compared against **every source pixel**
before acceptance and release of that job's TIFF cache. The stable delivery
includes quality Zarr, catalogs, `quality_links.parquet` and `companion.json`.
Pairing uses collection, tile and item identity, with separate indices for
10 m spectra, 20 m spectra and SCL. SCL must match the spectral **20 m** grid.

No accepted quality record means unavailable quality, not clear sky. SCL `0`
remains raw nodata; cloud categories retain their original values. A validity
mask indicates coverage, not cloud-free observations. Apply the chosen quality
policy before downstream aggregation; downloading SCL does not automatically
produce clear monthly inputs. See the [detailed Chinese guide](docs/SCL_USAGE_ZH.md).

## Processing records, append and replacement

Enable `metadata.provenance.enabled` using the
[native provenance example](config/provenance_native.example.yaml). Records in
`provenance/` associate outputs with source metadata, source checksums, actual
numeric operations, code digests, grid/time indices and write generations.
STAC declarations, observed source-file properties and executed operations are
recorded separately. Missing historical evidence remains unknown.

The tracked production path currently supports native grouped Zarr, local source
caching and serial scene processing. Disable monthly compositing, cloud masking,
previews and additional TIFF outputs. Band downloads may run concurrently.

- **Append:** use a new `run_id`, retain `dataset_id`, and keep
  `output.existing_item: skip`. Existing scenes are neither redownloaded nor
  assigned invented production records.
- **Replace:** explicitly use `existing_item: overwrite` with a new `run_id`.
  Only compatible, same-identity scenes can be replaced. This changes pixels;
  there is no automatic image backup.
- **Resume:** reuse the same run ID, configuration, code and environment. A
  committed write with a reliable receipt can finish metadata publication without
  rewriting pixels. Unresolved pending writes must not be cleared manually.
- **Rebind SCL:** after a spectral update, `rebind_quality --config ... --run-id ...`
  verifies reuse and publishes an independent pairing version without downloading
  or rewriting quality pixels. Use `--dry-run` first. Legacy frozen quality jobs
  cannot simply resume with changed code or configuration.

For new pairings, a compatible GRiTS-Resolver must explicitly select
`s2_quality_pairing="current"`; its legacy mode does not automatically switch.
See [production details (Chinese)](docs/PROVENANCE_PRODUCTION_ZH.md) and the
[Resolver reading contract](https://github.com/ottoKae/GRiTS-Resolver/blob/main/docs/PROVENANCE_READING_ZH.md).
Hand over the dataset, catalogs and associated provenance/quality records with
their relative structure intact. Current metadata is not a full-library pixel
checksum and cannot prove undocumented historical transformations.

## Read one scene

Choose a relative orbit and an explicit scene identity. The reader does not fill
missing pixels from another scene or track:

```python
from s2grits.reader import list_tracks, list_scenes, read_scene

store = "data/S2/17MPU/scenes_L2A_native/zarr/s2grits_scenes_17MPU_L2A_native.zarr"
print(list_tracks(store))
print(list_scenes(store))
# Replace with an item_id returned above; select the intended orbit if needed.
patch = read_scene(store, "10m", item_id="YOUR_ITEM_ID",
                   variables=["red", "green", "blue"], window=(100, 100, 256, 256))
red, valid = patch["data"]["red"], patch["valid"]["red"]
```

`window` is `(row, column, height, width)` in the selected group's native grid.
Values are stored values, not automatically scaled reflectance. `valid` is a
source-coverage mask. Cloud probability zero can be valid; same-scene SCL is
used to distinguish coverage when available. Without it, a source declaring
zero as nodata is treated conservatively and the ambiguity is reported.

For direct xarray access, install xarray separately, open each group independently
and explicitly select the committed scene index. Use `consolidated=False`,
`mask_and_scale=False` and `use_zarr_fill_value_as_mask=False` when inspecting
raw values with a compatible xarray version. Do not equate an SCL index with an
index in a different store. See [single-scene reading (Chinese)](docs/SINGLE_SCENE_READING_ZH.md).

## Recovery and limits

Scene commits cover all selected resolution groups. Failed, uncommitted appends
roll back their tails. A missing/corrupt commit record on a nonempty store stops
resume rather than truncating data. Restore trusted records or use another output
root; do not fabricate commit state. Ordinary resume cannot add/remove bands.

Cached TIFFs and partial downloads are bound to their sources and validated
before reuse. A local checksum proves cache consistency, not that an upstream
URL has never changed. `process_scenes` exits nonzero for failed/partial tiles or
failed audit; batch automation must inspect this status. Read stable archives
between production batches, not while a write or recovery is pending.

## Development and public-release checks

```sh
python -m pip install -e ".[dev]"
python tools/check_public.py
python -B tests/run.py
```

The public-file check reports locations and categories, never matched secret
values. It checks local documentation links, prohibited files, common credential
patterns and workstation paths in tracked and nonignored files. It is a review
aid, not a guarantee that all sensitive content or Git history is clean.

The test runner isolates scratch data in `.test-tmp/` and saves reports under
`tests/reports/`. Optional integration tests require a compatible
[GRiTS-Resolver checkout](https://github.com/ottoKae/GRiTS-Resolver). For a local
sibling checkout, use `python -m pip install -e ../GRiTS-Resolver` and
`python tools/verify_grits_resolver.py`. Install it with the same interpreter used
for testing, then run `python -B tests/run.py --integration` for required reader
integration acceptance. This mode fails if dependencies are missing; a skip in
the default standalone run does not establish integration compatibility.
The older fixed SCL-only Resolver revision is not a substitute for the new
provenance/pairing interface. Tests under ignored `tests/local/` depend on local
operational helpers and are not part of the public checkout.

Keep portable templates in `config/`, private jobs in `config/local/`, public
guides in `docs/`, and operational evidence in ignored `docs/reports/` or
`tooling/diagnostics/`. Source-package exclusions also apply: `.gitignore` alone
does not protect an sdist. Before publishing, inspect both sdist and wheel,
install the wheel in a clean environment and run `s2grits --help`.
Do not upload dataset files, signed URLs, credentials or raw operational logs to
issues. Report ordinary bugs with a small reproducible example and sanitized
configuration. Ignoring a file does not remove it from existing Git history.

## Further documentation

- [Native storage implementation](docs/NATIVE_IMPLEMENTATION.md) — English.
- [SCL companion](docs/SCL_USAGE_ZH.md), [processing records](docs/PROVENANCE_PRODUCTION_ZH.md),
  [compression](docs/LOSSLESS_COMPRESSION_ZH.md), [scene reading](docs/SINGLE_SCENE_READING_ZH.md)
  and [MGRS grid generation](docs/GLOBAL_MGRS_GRID_10M_ZH.md) — Chinese detailed guides.
- [Chinese README](README.zh-CN.md) — companion overview.

## License

Copyright 2026 ottoKae.

S2-GRiTS is available under the [PolyForm Noncommercial License 1.0.0](LICENSE).
Commercial use outside that license's permissions requires a separate agreement;
see [commercial licensing](LICENSE-COMMERCIAL.md). These terms govern software
use and distribution, not the separate terms of upstream satellite data or
third-party dependencies. There are no license-based restrictions in the code.
