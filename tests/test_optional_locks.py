from types import SimpleNamespace

import pytest

from s2grits.catalog import read_catalog, resync_global_catalog, upsert_records
from s2grits.locking import InterProcessLock
from s2grits import zarr_writer as writer


@pytest.mark.parametrize("locked", [False, True])
def test_optional_catalog_lock_preserves_upsert_and_resync(tmp_path, locked):
    lock_path = tmp_path / "locks" / "catalog.lock" if locked else None
    assert resync_global_catalog(tmp_path, lock_path=lock_path) is None
    assert not (tmp_path / "catalog.parquet").exists()
    tile_catalog = tmp_path / "50RPV" / "catalog.parquet"
    record = {
        "sensor": "sentinel-2", "product_type": "scenes", "tile_id": "50RPV",
        "item_id": "scene-1", "resolution_group": "20m", "cloud_cover": 10,
    }
    upsert_records(tile_catalog, [record], lock_path=lock_path)
    rows = upsert_records(tile_catalog, [{**record, "cloud_cover": 20}], lock_path=lock_path)
    assert len(rows) == 1 and rows[0]["cloud_cover"] == 20
    assert resync_global_catalog(tmp_path, lock_path=lock_path) == tmp_path / "catalog.parquet"
    assert read_catalog(tmp_path / "catalog.parquet") == read_catalog(tile_catalog) == rows
    assert (tmp_path / "locks" / "catalog.lock").exists() == locked
    if locked:
        with InterProcessLock(lock_path, timeout=0):
            pass


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("operation", ["writer", "cache"])
def test_wrappers_forward_arguments_and_release_optional_lock(
    tmp_path, monkeypatch, locked, fails, operation,
):
    lock_dir = tmp_path / "locks" if locked else None
    if operation == "writer":
        lock_path = tmp_path / "locks" / "zarr" / "50RPV.lock"
        arguments = {
            "item": SimpleNamespace(tile_id="50RPV"), "bands": ["blue"],
            "zarr_path": tmp_path / "store.zarr", "chunk_size": 7, "read_retries": 2,
            "cache_dir": tmp_path / "cache", "read_mode": "remote_stream_first",
            "progress": object(), "compression": {"clevel": 1}, "write_workers": 1,
        }
        expected_args, expected_kwargs = (), arguments
        underlying_name = "_append_item_to_native_zarr_unlocked"
        result = True

        def invoke():
            return writer.append_item_to_native_zarr(**arguments, lock_dir=lock_dir)
    else:
        lock_path = tmp_path / "locks" / "cache" / "scene_a" / "blue_x.lock"
        expected_args = ("https://example.invalid/blue.tif", tmp_path / "cache", "scene/a", "blue/x")
        expected_kwargs = {"collection": None, "asset": None}
        underlying_name = "_cache_asset_unlocked"
        result = tmp_path / "cached.tif"

        def invoke():
            return writer.cache_asset(*expected_args, lock_dir=lock_dir)

    def underlying(*args, **kwargs):
        assert args == expected_args and kwargs == expected_kwargs
        if locked:
            with pytest.raises(TimeoutError):
                with InterProcessLock(lock_path, timeout=0):
                    pytest.fail("wrapper did not hold its lock")
        else:
            assert not (tmp_path / "locks").exists()
        if fails:
            raise OSError("simulated underlying failure")
        return result

    monkeypatch.setattr(writer, underlying_name, underlying)
    if fails:
        with pytest.raises(OSError, match="simulated underlying failure"):
            invoke()
    else:
        assert invoke() == result
    if locked:
        with InterProcessLock(lock_path, timeout=0):
            pass
