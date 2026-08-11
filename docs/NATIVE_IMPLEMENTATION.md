# Native-resolution implementation

## Storage layout

Each MGRS tile has one Zarr store with three spatial groups and one shared
time axis:

```text
{output}/{tile}/scenes_L2A_native/
  zarr/s2grits_scenes_{tile}_L2A_native.zarr/
    time
    10m/time, 10m/x, 10m/y, 10m/{blue,green,red,nir}
    20m/time, 20m/x, 20m/y, 20m/{rededge1,rededge2,rededge3,nir08,swir16,swir22,scl,cloud}
    60m/time, 60m/x, 60m/y, 60m/{coastal,nir09}
  preview/s2grits_scenes_{tile}_{datetime}_{item_id}.jpg
```

Source COG pixels are copied by raster window. The writer does not use a
warped VRT and does not resample between resolutions. Before a band is
written, its CRS, transform, dimensions, and pixel size must match the native
reference grid for its resolution group.

## Scene transaction and resume

The asset cache downloads up to two scenes concurrently and up to four bands
per scene. Completed scenes are written to Zarr in acquisition-time order.
The production configuration uses parallel Python `requests` streams because
a local benchmark against the same AWS S3 object was materially faster than
`aria2c`. If `aria2c` is selected manually, a 45-second zero-growth detector
switches the affected asset to `requests` automatically.
The three native groups are one scene transaction:

1. Resize all selected band arrays for the pending time index.
2. Write the 10 m, 20 m, and 60 m source pixels.
3. Commit the root time value and `s2grits_metadata.json` only after all bands
   succeed.
4. On any error, resize every group back to the last committed scene count.

On restart, cached COGs are reused after a raster-open validation. A scene in
the committed metadata is skipped. Uncommitted array tails are truncated to
the committed scene count before the next append.

## Terminal and file logging

The terminal displays live rows for:

- total MGRS tile progress;
- the current tile's monthly Earth Search queries;
- the current tile's completed scene count;
- each concurrently downloading scene, including band count, bytes, average
  transfer speed, active bands, elapsed time, and ETA;
- the scene being written, including resolution group, band, and chunk count.

The timestamped file log records `[PHASE] START/END/FAIL` events, every tile and
scene identifier, source size, retries, download speed, Zarr group and band,
preview/catalog work, and per-scene/tile/run elapsed time.

## Inventories

`download_manifest.csv` and `download_manifest.parquet` contain every
discovered scene, including failed scenes. Fields include MGRS tile, S2 date,
item ID, platform, cloud cover, source collection, provider, STAC API and item
URL, product metadata, per-resolution band lists, source asset JSON, declared
and downloaded bytes, stage/status, timings, output paths, and error text.

`download_events.jsonl` is append-only and records live state transitions. The
per-tile and global `catalog.parquet` files contain only committed products.

## Final audit

At workflow completion, `audit_report.json` compares the Earth Search
inventory with the tile catalogs, committed Zarr metadata, all 14 arrays and
their shared time length, native group resolution attributes, and previews.
Missing products are also written to `retry_scenes.csv`.
