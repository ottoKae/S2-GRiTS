from __future__ import annotations

from dataclasses import replace
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
import zarr

from s2grits.bands import selected_native_groups, variable_name
from s2grits.locking import InterProcessLock
from s2grits.stac import S2Item
from s2grits import zarr_writer as writer


BANDS = ["red", "blue", "green", "nir", "nir08", "rededge1", "rededge2", "rededge3", "swir16", "swir22"]


@pytest.fixture
def scene(tmp_path):
    assets = {}
    for number, band in enumerate(BANDS):
        resolution = 10 if number < 4 else 20
        size = 480 // resolution
        path = tmp_path / f"{band}.tif"
        values = np.arange(size * size, dtype="uint16").reshape(size, size) + 100 * number
        values[0, :3] = 0
        with rasterio.open(
            path, "w", driver="GTiff", height=size, width=size, count=1,
            dtype="uint16", crs="EPSG:32717",
            transform=from_origin(500000, 9600040, resolution, resolution), nodata=0,
        ) as dataset:
            dataset.write(values, 1)
        assets[band] = {
            "href": str(path), "gsd": resolution,
            "raster:bands": [{"data_type": "uint16", "nodata": 0, "scale": 0.0001, "offset": -0.1}],
        }
    return S2Item(
        id="scene-1", collection="sentinel-2-c1-l2a", datetime="2018-01-01T12:34:56.123Z",
        tile_id="17MNR", cloud_cover=20.0, platform="sentinel-2a", bbox=None, geometry=None,
        assets=assets, properties={}, links=[],
    )


def append(scene, store, **kwargs):
    return writer.append_item_to_native_zarr(
        item=scene, bands=BANDS, zarr_path=store, chunk_size=16,
        read_mode="remote_stream_first", **kwargs,
    )


@pytest.mark.parametrize("workers", [2, 4])
def test_parallel_matches_serial_pixels_metadata_and_codec(scene, tmp_path, workers):
    serial = tmp_path / "serial.zarr"
    parallel = tmp_path / "parallel.zarr"
    updates = []
    caller = threading.get_ident()
    progress = SimpleNamespace(update_scene_write=lambda **kwargs: updates.append((threading.get_ident(), kwargs)))
    for item in [scene, replace(scene, id="scene-2", datetime="2018-02-01T12:34:56.456Z")]:
        assert append(item, serial)
        assert append(item, parallel, write_workers=workers, progress=progress)

    first, second = (zarr.open_group(str(path), mode="r") for path in (serial, parallel))
    np.testing.assert_array_equal(first["time"][:], second["time"][:])
    for name, bands in selected_native_groups(BANDS).items():
        for key in first[name].array_keys():
            np.testing.assert_array_equal(first[name][key][...], second[name][key][...])
        for band in bands:
            array = second[name][variable_name(band)]
            with rasterio.open(scene.assets[band]["href"]) as source:
                np.testing.assert_array_equal(array[0], source.read(1))
            assert array.dtype == np.dtype("uint16")
            assert [codec.to_dict() for codec in array.compressors] == [
                {"name": "blosc", "configuration": {
                    "typesize": 2, "cname": "zstd", "clevel": 5,
                    "shuffle": "shuffle", "blocksize": 0,
                }}
            ]
    serial_metadata = {path.relative_to(serial): json.loads(path.read_text()) for path in serial.rglob("*.json")}
    parallel_metadata = {path.relative_to(parallel): json.loads(path.read_text()) for path in parallel.rglob("*.json")}
    assert serial_metadata == parallel_metadata
    assert len(updates) == 20
    assert all(thread == caller for thread, _ in updates)
    assert [update["completed_bands"] for _, update in updates] == list(range(1, 11)) * 2


def test_late_parallel_failure_joins_writers_before_rollback(scene, tmp_path, monkeypatch):
    store = tmp_path / "rollback.zarr"
    locks = tmp_path / "locks"
    assert append(scene, store, lock_dir=locks)
    sidecar = (store / "s2grits_resume_state.json").read_bytes()
    next_scene = replace(scene, id="scene-2", datetime="2018-02-01T12:34:56.123Z")
    write = writer._write_native_band
    resize = writer._resize_native_arrays
    peer_started, failure_raised, late_write_finished, rolled_back = (threading.Event() for _ in range(4))
    active_lock = threading.Lock()
    active = set()
    observed_peak = 0

    def injected_write(**kwargs):
        nonlocal observed_peak
        band = kwargs["band"]
        with active_lock:
            active.add(band)
            observed_peak = max(observed_peak, len(active))
        try:
            assert not rolled_back.is_set()
            assert kwargs["progress"] is None
            with pytest.raises(TimeoutError):
                with InterProcessLock(locks / "zarr" / "17MNR.lock", timeout=0):
                    pytest.fail("tile lock was released while band writers were active")
            if band == "rededge1":
                assert peer_started.wait(5)
                write(**kwargs)
                failure_raised.set()
                raise RuntimeError("late band failure")
            if band == "rededge2":
                peer_started.set()
                assert failure_raised.wait(5)
                # The failing future is now observable; a correct rollback still
                # waits for this writer, which completes actual Zarr chunk writes.
                time.sleep(0.05)
                assert not rolled_back.is_set()
                write(**kwargs)
                assert not rolled_back.is_set()
                late_write_finished.set()
            else:
                write(**kwargs)
        finally:
            with active_lock:
                active.remove(band)

    def observed_resize(root, groups, length):
        if failure_raised.is_set():
            assert not active
            assert late_write_finished.is_set()
            rolled_back.set()
        return resize(root, groups, length)

    monkeypatch.setattr(writer, "_write_native_band", injected_write)
    monkeypatch.setattr(writer, "_resize_native_arrays", observed_resize)
    with pytest.raises(RuntimeError, match="late band failure"):
        append(next_scene, store, write_workers=2, lock_dir=locks)
    assert rolled_back.is_set() and late_write_finished.is_set() and not active
    assert observed_peak == 2
    assert (store / "s2grits_resume_state.json").read_bytes() == sidecar
    root = zarr.open_group(str(store), mode="r")
    assert root["time"].shape == (1,)
    for name, bands in selected_native_groups(BANDS).items():
        assert root[name]["time"].shape == (1,)
        for band in bands:
            assert root[name][variable_name(band)].shape[0] == 1
            with rasterio.open(scene.assets[band]["href"]) as source:
                np.testing.assert_array_equal(root[name][variable_name(band)][0], source.read(1))
            tail = store / name / variable_name(band) / "c" / "1"
            assert not any(path.is_file() for path in tail.rglob("*"))
    with InterProcessLock(locks / "zarr" / "17MNR.lock", timeout=0):
        pass
    monkeypatch.setattr(writer, "_write_native_band", write)
    monkeypatch.setattr(writer, "_resize_native_arrays", resize)
    assert append(next_scene, store, write_workers=2, lock_dir=locks)
    assert zarr.open_group(str(store), mode="r")["time"].shape == (2,)


@pytest.mark.parametrize("workers", [0, 5, -1, True, 1.5, "2", None])
def test_invalid_worker_count_does_not_create_store(scene, tmp_path, workers):
    store = tmp_path / "invalid.zarr"
    with pytest.raises(ValueError, match="write_workers"):
        append(scene, store, write_workers=workers)
    assert not store.exists()


def test_default_writer_remains_serial(scene, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("legacy default unexpectedly created parallel band writers")
    monkeypatch.setattr(writer, "ThreadPoolExecutor", forbidden)
    assert append(scene, tmp_path / "default.zarr")
