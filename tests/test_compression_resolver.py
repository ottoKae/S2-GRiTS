"""Exercise the optional downstream resolver against the native compressed writer."""
from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rasterio
import zarr
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject
from rasterio.windows import transform as window_transform

xr = pytest.importorskip("xarray")
pytest.importorskip("grits_resolver")
from grits_resolver import MultiSensorResolver
from grits_resolver.spatial import TargetWindow

from s2grits.catalog import upsert_records
from s2grits.stac import S2Item
from s2grits.workflow import _catalog_records
from s2grits.zarr_writer import append_item_to_native_zarr


@pytest.mark.parametrize("target_resolution", [20, 10], ids=["native", "reprojected"])
def test_compressed_mixed_band_windows_through_resolver(tmp_path, target_resolution):
    s2_root, s1_root = tmp_path / "s2", tmp_path / "s1"
    s2_root.mkdir()
    s1_root.mkdir()
    store = s2_root / "native.zarr"
    source_transform = from_origin(500000, 3600000, 20, 20)
    crs = "EPSG:32650"
    dates = ["2025-01-01T00:00:00Z", "2025-01-11T00:00:00Z"]
    reference = []
    rows, cols = np.indices((9, 11))  # Both spatial edges are partial chunks.
    for scene, date in enumerate(dates):
        arrays = {
            "nir08": (1000 + rows * 100 + cols * 10 + scene * 500).astype("uint16"),
            "scl": ((rows + cols + scene) % 11 + 1).astype("uint8"),
        }
        arrays["nir08"][3, 5] = 0
        arrays["scl"][3, 5] = 0
        reference.append(arrays)
        assets = {}
        for band, data in arrays.items():
            path = tmp_path / f"{scene}_{band}.tif"
            with rasterio.open(
                path, "w", driver="GTiff", height=9, width=11, count=1,
                dtype=data.dtype, crs=crs, transform=source_transform, nodata=0,
            ) as dataset:
                dataset.write(data, 1)
            assets[band] = {
                "href": str(path), "gsd": 20,
                "raster:bands": [{"data_type": str(data.dtype), "nodata": 0}],
            }
        item = S2Item(
            id=f"scene-{scene}", collection="sentinel-2-c1-l2a", datetime=date,
            tile_id="50RPV", cloud_cover=0, platform="synthetic", bbox=None,
            geometry=None, assets=assets, properties={}, links=[],
        )
        assert append_item_to_native_zarr(
            item=item, bands=list(arrays), zarr_path=store, chunk_size=4,
            read_mode="remote_stream_first",
        )
        upsert_records(s2_root / "catalog.parquet", _catalog_records(
            item=item, band_groups={"20m": list(arrays)}, zarr_path=store,
            preview_path=None, output_root=s2_root, stac_api="synthetic",
            provider="synthetic",
        ))

    group = zarr.open_group(store, mode="r")["20m"]
    for variable in ("nir_narrow", "scl"):
        codecs = group[variable].metadata.to_dict()["codecs"]
        assert any(codec["name"] == "blosc" for codec in codecs)

    factor = 20 // target_resolution
    height, width = 9 * factor, 11 * factor
    target_transform = from_origin(500000, 3600000, target_resolution, target_resolution)
    times = np.array([date.removesuffix("Z") for date in dates], dtype="datetime64[ns]")
    xr.Dataset(coords={"time": times}).to_zarr(
        s1_root / "monthly.zarr", zarr_format=3, consolidated=False,
    )
    pq.write_table(pa.Table.from_pylist([{
        "tile_id": "50RPV", "status": "complete", "product_type": "smonthly",
        "zarr_path": "monthly.zarr", "zarr_group": None, "crs": crs,
        "grid_id": group.attrs["grid_id"] if factor == 1 else "synthetic-10m",
        "transform": list(target_transform)[:6], "height": height, "width": width,
        "resolution_x": target_resolution, "resolution_y": target_resolution,
        "bands": [], "start_datetime": dates[0], "end_datetime": dates[-1],
    }]), s1_root / "catalog.parquet")

    resolver = MultiSensorResolver(s1_root, s2_root)
    expected_mode = "exact" if factor == 1 else "virtual_reprojection"
    assert resolver.spatial_alignment_mode(
        resolver.s2_entry("50RPV"), resolver.s1_entry("50RPV")
    ) == expected_mode
    assert [match.right_index for match in resolver.time_join("50RPV", method="exact")] == [0, 1]

    # The native window crosses chunk boundaries; reprojection includes partial edge chunks.
    window = TargetWindow(2, 3, 5, 6) if factor == 1 else TargetWindow(0, 0, height, width)
    for scene in range(2):
        actual = resolver.read_s2_variables_on_s1(
            "50RPV", ["nir_narrow", "scl"], s2_time_index=scene, window=window,
        )
        for variable, band, method, dtype, nodata in (
            ("nir_narrow", "nir08", Resampling.bilinear, "float32", np.nan),
            ("scl", "scl", Resampling.nearest, "uint8", 0),
        ):
            # Independent oracle starts with source pixels, bypassing Zarr and xarray.
            expected = np.full((window.height, window.width), nodata, dtype=dtype)
            reproject(
                source=reference[scene][band], destination=expected,
                src_transform=source_transform, src_crs=crs, src_nodata=0,
                dst_transform=window_transform(window.rasterio_window(), target_transform),
                dst_crs=crs, dst_nodata=nodata, resampling=method,
            )
            assert actual[variable].dtype == np.dtype(dtype)
            np.testing.assert_array_equal(actual[variable], expected)
