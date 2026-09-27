# Native storage implementation

S2-GRiTS stores Sentinel-2 L2A as three independent native grids (`10m`, `20m`, `60m`). Each array has dimensions `(time, y, x)`; no resampling occurs during ingestion.

New band arrays use Zarr format 3 with explicit Blosc/Zstd level 5, byte shuffle,
and `typesize=dtype.itemsize`. `output.zarr_compression` can adjust the lossless
codec settings. Band chunks remain `(1, min(chunk_size, height), min(chunk_size, width))`.
Coordinate and scalar arrays retain their small default Zarr encodings. Existing
band codecs are never changed on resume; a mismatch is logged and requires a
verified separate copy to convert. Writes align with existing stored chunks even
if the requested runtime chunk size changes. Raster/store dtype disagreement
raises before assignment to prevent an implicit lossy cast.

Storage codecs do not enter the scientific processing signature or grid ID.
See [compression configuration and usage](LOSSLESS_COMPRESSION_ZH.md).

Optional catalog and native-store locks now wrap a single write path using
`contextlib.nullcontext` when no lock is requested. This removes duplicate
branches without changing the critical sections, public arguments or rollback
behavior. Local tests, benchmarks, reports and plans are excluded from Git;
published configuration examples and launchers contain no workstation paths.

## Transaction and resume boundary

For each scene the writer:

1. resolves or downloads all requested assets into `output.cache_dir`;
2. validates each native grid against the existing group;
3. extends and writes all selected arrays;
4. rolls every group back to the prior committed length if any write fails;
5. commits time coordinates and `s2grits_resume_state.json` only after success.

The old `s2grits_metadata.json` is read for compatibility, but new stores write only the explicit resume-state name.

## Stable variables

Zarr variable names describe physical meaning rather than provider-specific keys. Attributes preserve both provenance levels:

- `source_band`: real Sentinel-2 band identifier, such as `B08` or `B8A`;
- `source_asset`: Earth Search asset key, such as `nir` or `nir08`.

In particular, `B08 -> nir_broad`, `B8A -> nir_narrow`, and `B05 -> red_edge_1`.

## Catalog contract

Every successfully committed scene contributes one canonical schema v7 row per native resolution group. Asset paths are relative to the global dataset root. Grid IDs use the same normalized CRS/transform/shape hashing contract as S1-GRiTS, enabling a resolver to distinguish exact grid equality from lazy reprojection.

## Operational files

`output.logs_dir` and `output.cache_dir` are external shared directories, conventionally named `logs` and `_cache`. The normal workflow writes one task-qualified run log and no manifest/status/summary/audit sidecars. When final validation finds missing products, it writes a dataset-qualified retry Parquet to the shared log directory.
