from dataclasses import replace
import json

import numpy as np
import pytest
import rasterio
import xarray as xr
import zarr
from rasterio.transform import from_origin
from zarr.codecs import ZstdCodec

from s2grits.bands import NATIVE_BANDS, selected_native_groups, variable_name
from s2grits.compression import DEFAULT_COMPRESSION, compression_settings
from s2grits.config import apply_defaults, validate_config
from s2grits.stac import S2Item
from s2grits import zarr_writer as writer


@pytest.fixture
def scene(tmp_path):
    assets, expected = {}, {}
    rng = np.random.default_rng(47)
    for group, bands in selected_native_groups(list(NATIVE_BANDS)).items():
        resolution = int(group[:-1])
        size = 180 // resolution
        for band in bands:
            dtype = np.dtype("uint8" if band in {"scl", "cloud"} else "uint16")
            values = rng.integers(0, np.iinfo(dtype).max + 1, (size, size), dtype=dtype)
            values.flat[:3] = [0, 1, np.iinfo(dtype).max]
            path = tmp_path / f"{band}.tif"
            with rasterio.open(
                path, "w", driver="GTiff", height=size, width=size, count=1,
                dtype=dtype, crs="EPSG:32650",
                transform=from_origin(500000, 3600000, resolution, resolution),
            ) as dst:
                dst.write(values, 1)
            assets[band] = {"href": str(path), "gsd": resolution, "raster:bands": [
                {"data_type": str(dtype), "nodata": 0, "scale": 0.0001, "offset": -0.1}
            ]}
            expected[group, variable_name(band)] = values
    return S2Item(
        id="scene-1", collection="sentinel-2-c1-l2a", datetime="2025-01-01T00:00:00Z",
        tile_id="50RPV", cloud_cover=10, platform="sentinel-2b", bbox=None,
        geometry=None, assets=assets, properties={}, links=[],
    ), expected


def append(scene, store, **kwargs):
    return writer.append_item_to_native_zarr(
        item=scene, bands=list(NATIVE_BANDS), zarr_path=store,
        chunk_size=kwargs.pop("chunk_size", 7), read_mode="remote_stream_first", **kwargs,
    )


def test_native_roundtrip_all_bands_edges_and_resume(scene, tmp_path):
    item, expected = scene
    store = tmp_path / "compressed.zarr"
    assert append(item, store)
    second = replace(item, id="scene-2", datetime="2025-01-11T00:00:00Z")
    assert append(second, store)
    assert not append(second, store)
    root = zarr.open_group(store, mode="r")
    for (group, variable), values in expected.items():
        array = root[group][variable]
        codec = array.compressors[0].to_dict()["configuration"]
        assert codec == {**DEFAULT_COMPRESSION, "typesize": values.dtype.itemsize, "blocksize": 0}
        assert array.dtype == values.dtype
        assert array[:].tobytes() == np.stack([values, values]).tobytes()
        assert array.attrs["scale"] == 0.0001
        assert array.attrs["offset"] == -0.1
        assert array.attrs["nodata"] == 0
    for group in ("10m", "20m", "60m"):
        with xr.open_zarr(store, group=group, consolidated=False, mask_and_scale=False) as ds:
            for (name, variable), values in expected.items():
                if name == group:
                    np.testing.assert_array_equal(ds[variable].values, np.stack([values, values]))
            np.testing.assert_array_equal(ds.time.values, root["time"][:])
    state = json.loads((store / "s2grits_resume_state.json").read_text())
    assert len(state["items"]) == 2


def test_legacy_codec_preserved_and_writes_align_existing_chunks(scene, tmp_path, monkeypatch, caplog):
    item, expected = scene
    store = tmp_path / "legacy.zarr"
    with monkeypatch.context() as patch:
        patch.setattr(writer, "make_compressor", lambda *args: ZstdCodec(level=0))
        append(item, store)
    root = zarr.open_group(store, mode="r")
    signature = root["10m"].attrs["processing_signature"]
    windows = []
    original = writer._read_window_with_retry

    def record(src, band, href, window, retries):
        if band == "blue":
            windows.append(window)
        return original(src, band, href, window, retries)

    monkeypatch.setattr(writer, "_read_window_with_retry", record)
    assert append(replace(item, id="scene-2"), store, chunk_size=2)
    assert len(windows) == 9  # 18 x 18 source on stored 7 x 7 chunks, not 2 x 2.
    root = zarr.open_group(store, mode="r")
    assert root["10m"].attrs["processing_signature"] == signature
    assert "preserving existing codec" in caplog.text
    for (group, variable), values in expected.items():
        array = root[group][variable]
        assert array.compressors == (ZstdCodec(level=0),)
        assert array[:].tobytes() == np.stack([values, values]).tobytes()


def test_failure_rolls_back_compressed_scene_and_allows_retry(scene, tmp_path, monkeypatch):
    item, expected = scene
    store = tmp_path / "rollback.zarr"
    append(item, store)
    state = (store / "s2grits_resume_state.json").read_bytes()
    second = replace(item, id="scene-2")
    original = writer._write_native_band

    def fail(**kwargs):
        if kwargs["band"] == "scl":
            raise OSError("simulated read failure after spectral bands")
        return original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(writer, "_write_native_band", fail)
        with pytest.raises(OSError, match="simulated"):
            append(second, store)
    root = zarr.open_group(store, mode="r")
    assert (store / "s2grits_resume_state.json").read_bytes() == state
    assert root["time"].shape == (1,)
    for (group, variable), values in expected.items():
        assert root[group][variable].shape[0] == 1
        np.testing.assert_array_equal(root[group][variable][0], values)
    assert append(second, store)


def test_dtype_mismatch_rejected_without_commit(scene, tmp_path):
    item, _ = scene
    # Incorrect STAC metadata would otherwise cause silent uint16 -> uint8 casts.
    item.assets["blue"]["raster:bands"][0]["data_type"] = "uint8"
    store = tmp_path / "bad-dtype.zarr"
    with pytest.raises(ValueError, match="Refusing lossy dtype conversion"):
        append(item, store)
    assert not (store / "s2grits_resume_state.json").exists()
    assert zarr.open_group(store, mode="r")["10m"]["blue"].shape[0] == 0


@pytest.mark.parametrize("settings", [
    {"clevel": 10}, {"clevel": -1}, {"clevel": True}, {"clevel": "5"},
    {"shuffle": "byte"}, {"cname": "other"}, {"quantize": 2}, "zstd",
])
def test_invalid_compression_rejected(settings):
    with pytest.raises(ValueError):
        compression_settings(settings)
    cfg = {"output": {"zarr_compression": settings}}
    apply_defaults(cfg)
    assert any("zarr_compression" in error for error in validate_config(cfg))


def test_configuration_defaults_are_independent_and_custom_level_applies(scene, tmp_path):
    first, second = {}, {}
    apply_defaults(first)
    apply_defaults(second)
    first["output"]["zarr_compression"]["clevel"] = 1
    assert second["output"]["zarr_compression"] == DEFAULT_COMPRESSION
    partial = {"output": {"zarr_compression": {"clevel": 1}}}
    apply_defaults(partial)
    assert partial["output"]["zarr_compression"] == {**DEFAULT_COMPRESSION, "clevel": 1}
    append(scene[0], tmp_path / "level1.zarr", compression={"clevel": 1})
    codec = zarr.open_group(tmp_path / "level1.zarr", mode="r")["10m"]["blue"].compressors[0]
    assert codec.to_dict()["configuration"]["clevel"] == 1


@pytest.mark.parametrize("size", [0, -1, True, "512"])
def test_invalid_chunk_size_fails_before_store_creation(scene, tmp_path, size):
    store = tmp_path / "invalid.zarr"
    with pytest.raises(ValueError, match="chunk_size"):
        append(scene[0], store, chunk_size=size)
    assert not store.exists()
