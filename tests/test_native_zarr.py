from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
import zarr
from rasterio.transform import from_origin

from s2grits.audit import _store_warnings
from s2grits.bands import NATIVE_BANDS, selected_native_groups
from s2grits.stac import S2Item
from s2grits.zarr_writer import append_item_to_native_zarr


class NativeZarrWriterTest(unittest.TestCase):
    def test_writes_three_native_resolution_groups_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_value:
            tmp = Path(tmp_value)
            assets = {}
            groups = selected_native_groups(list(NATIVE_BANDS))
            for group_name, bands in groups.items():
                resolution = int(group_name[:-1])
                size = 120 // resolution
                for index, band in enumerate(bands, start=1):
                    path = tmp / f"{band}.tif"
                    with rasterio.open(
                        path,
                        "w",
                        driver="GTiff",
                        height=size,
                        width=size,
                        count=1,
                        dtype="uint8" if band in {"scl", "cloud"} else "uint16",
                        crs="EPSG:32650",
                        transform=from_origin(500000, 3600000, resolution, resolution),
                    ) as dataset:
                        dataset.write(np.full((size, size), index, dtype=dataset.dtypes[0]), 1)
                    assets[band] = {
                        "href": str(path),
                        "gsd": resolution,
                        "raster:bands": [
                            {
                                "data_type": "uint8" if band in {"scl", "cloud"} else "uint16",
                                "spatial_resolution": resolution,
                                "nodata": 0,
                            }
                        ],
                    }

            def item(item_id: str, dt: str) -> S2Item:
                return S2Item(
                    id=item_id,
                    collection="sentinel-2-c1-l2a",
                    datetime=dt,
                    tile_id="50RPV",
                    cloud_cover=10.0,
                    platform="sentinel-2b",
                    bbox=None,
                    geometry=None,
                    assets=assets,
                    properties={},
                    links=[],
                )

            store = tmp / "native.zarr"
            self.assertTrue(
                append_item_to_native_zarr(
                    item=item("scene-1", "2025-01-01T00:00:00Z"),
                    bands=list(NATIVE_BANDS),
                    zarr_path=store,
                    chunk_size=4,
                    read_mode="remote_stream_first",
                )
            )
            self.assertTrue(
                append_item_to_native_zarr(
                    item=item("scene-2", "2025-01-11T00:00:00Z"),
                    bands=list(NATIVE_BANDS),
                    zarr_path=store,
                    chunk_size=4,
                    read_mode="remote_stream_first",
                )
            )
            self.assertFalse(
                append_item_to_native_zarr(
                    item=item("scene-2", "2025-01-11T00:00:00Z"),
                    bands=list(NATIVE_BANDS),
                    zarr_path=store,
                    chunk_size=4,
                    read_mode="remote_stream_first",
                )
            )

            root = zarr.open_group(str(store), mode="r")
            self.assertEqual(root["time"].shape, (2,))
            self.assertEqual(root["10m"]["blue"].shape, (2, 12, 12))
            self.assertEqual(root["10m"]["time"].shape, (2,))
            self.assertEqual(root["20m"]["scl"].shape, (2, 6, 6))
            self.assertEqual(root["60m"]["coastal"].shape, (2, 2, 2))
            self.assertEqual(root["20m"].attrs["resampling"], "none")
            dataset_10m = xr.open_zarr(store, group="10m", consolidated=False)
            self.assertEqual(dataset_10m.sizes, {"time": 2, "y": 12, "x": 12})
            self.assertIn("blue", dataset_10m.data_vars)
            self.assertEqual(_store_warnings(store, groups, 2), [])


if __name__ == "__main__":
    unittest.main()
