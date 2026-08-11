# S2-GRiTS Design Notes

## Data Source

S2-GRiTS uses Element84 Earth Search:

```text
https://earth-search.aws.element84.com/v1
```

Default collection:

```yaml
source:
  collection: "sentinel-2-l2a"
```

The newer `sentinel-2-c1-l2a` can be selected in config, but the default keeps
the broader historical L2A COG archive.

## Streaming Model

The workflow defaults to S1-GRiTS-like local caching:

```yaml
source:
  read_mode: "local_cache_first"
```

For each scene, selected band COGs are downloaded to the configurable external
`output.cache_dir`, verified, and then read locally into Zarr. This is more stable than issuing thousands of
HTTP range requests directly against remote COGs.

The cache stage follows the HLS-GRiTS scene downloader pattern:

```yaml
runtime:
  download_scene_workers: 2
  download_band_workers: 3
```

Within each MGRS tile, multiple scenes can be cached concurrently, and each
scene can download multiple bands concurrently. Zarr writing remains serial per
tile so that the store is not modified by multiple threads at once.

Remote streaming is still available:

```yaml
source:
  read_mode: "remote_stream_first"
  local_cache_on_read_failure: true
```

In that mode, the workflow streams public HTTPS COG assets with GDAL/rasterio
range requests and falls back to local cache when a band read fails. It does not
write separate raw products unless this is enabled:

```yaml
output:
  formats:
    raw_assets: true
```

Each selected scene is read by 512 x 512 windows and appended to a per-MGRS Zarr
store.

## Band Handling

Default bands:

```yaml
assets:
  bands: ["blue", "green", "red", "nir", "scl", "cloud"]
```

Common Sentinel-2 L2A COG assets:

| Resolution | Assets |
| --- | --- |
| 10 m | `blue`, `green`, `red`, `nir` |
| 20 m | `rededge1`, `rededge2`, `rededge3`, `nir08`, `swir16`, `swir22`, `scl`, `cloud`, `snow`, `aot`, `wvp` |
| 60 m | `coastal`, `nir09` |

All selected bands are written to one target grid:

```yaml
assets:
  target_resolution: 10
  resampling: "nearest"
```

This means 20 m and 60 m assets are upsampled to the 10 m reference grid using
nearest neighbor. The `visual`, `preview`, and `thumbnail` assets are not scalar
analysis bands and are intentionally not allowed in `assets.bands`.

## Cloud Control

Cloud filtering is item-level only in this build:

```yaml
source:
  cloud_cover_max: 100
```

This maps to STAC `eo:cloud_cover`. Set it lower, for example `80`, to reduce
download volume.

Pixel-level masks are not generated:

```yaml
processing:
  cloud_mask:
    enabled: false
```

If needed later, include `scl` and/or `cloud` in `assets.bands` and create
application-specific masks during analysis.

## Output Layout

```text
E:/JiangSu_S2/
  catalog.parquet
  50RQV/
    catalog.parquet
    scenes_L2A_10m/
      zarr/
        s2grits_scenes_50RQV_L2A_10m.zarr/
      cog/
        s2grits_scenes_50RQV_20250130T025933_S2C_50RQV_20250130_0_L2A.tif
      preview/
        s2grits_scenes_50RQV_20250130T025933_S2C_50RQV_20250130_0_L2A.jpg
```
