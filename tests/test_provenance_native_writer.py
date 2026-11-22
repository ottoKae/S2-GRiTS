"""Tracked native commits and same-item replacement; tiny local rasters only."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import rasterio
import zarr
from rasterio.transform import from_origin

from s2grits import zarr_writer as writer
from s2grits.bands import variable_name
from s2grits.stac import S2Item


@pytest.fixture
def item(tmp_path):
    assets = {}
    for band, resolution, size in [("blue", 10, 4), ("swir16", 20, 2)]:
        path = tmp_path / (band + ".tif")
        with rasterio.open(path, "w", driver="GTiff", width=size, height=size,
                           count=1, dtype="uint16", crs="EPSG:32717",
                           transform=from_origin(500000, 9600000, resolution, resolution), nodata=0) as src:
            src.write(np.arange(size * size, dtype="uint16").reshape(size, size), 1)
        assets[band] = {"href": str(path), "raster:bands": [
            {"data_type": "uint16", "nodata": 0, "scale": 0.0001, "offset": -0.1}]}
    return S2Item(id="scene-a", collection="sentinel-2-c1-l2a", datetime="2025-01-02T12:00:00Z",
                  tile_id="17MNR", cloud_cover=0, platform="sentinel-2a", bbox=None, geometry=None,
                  assets=assets, properties={}, links=[])


def append(item, store, **kwargs):
    return writer.append_item_to_native_zarr(item=item, bands=list(item.assets), zarr_path=store,
        chunk_size=2, read_mode="remote_stream_first", **kwargs)


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class Tracker:
    def __init__(self, pending, *, fail_commit=False):
        self.pending, self.fail_commit = pending, fail_commit
        self.calls, self.arguments = [], None

    def prepare(self, **kwargs):
        self.arguments = kwargs
        self.calls.append("prepare")
        self.pending.write_text("writing", encoding="utf-8")

    def used(self, band):
        self.calls.append(band)

    def commit(self, **kwargs):
        assert kwargs["index"] == self.arguments["index"]
        entries = json.loads((kwargs["zarr_path"] / "s2grits_resume_state.json").read_text())
        assert entries["items"][kwargs["index"]]["item_id"] == kwargs["item"].id
        self.calls.append("commit")
        if self.fail_commit:
            raise RuntimeError("receipt failure")
        self.pending.unlink()


def change_sources(item):
    for asset in item.assets.values():
        with rasterio.open(asset["href"], "r+") as source:
            source.write(source.read(1) + 100, 1)


def test_default_append_and_same_item_skip_are_unchanged(item, tmp_path, monkeypatch):
    store = tmp_path / "plain.zarr"
    assert append(item, store)
    before = snapshot(store)
    monkeypatch.setattr(writer, "_resolve_asset_paths", lambda **kw: pytest.fail("Skipped scene was downloaded"))
    assert not append(item, store)
    assert snapshot(store) == before


def test_prepare_precedes_any_store_mutation_and_records_used_after_write(item, tmp_path, monkeypatch):
    store = tmp_path / "ordered.zarr"
    tracker = Tracker(tmp_path / "pending.json")
    open_group, write_band = writer.zarr.open_group, writer._write_native_band

    def checked_open(*args, **kwargs):
        if kwargs.get("mode") == "a":
            assert tracker.pending.exists() and tracker.calls == ["prepare"]
        return open_group(*args, **kwargs)

    def checked_write(**kwargs):
        assert tracker.pending.exists()
        assert kwargs["band"] not in tracker.calls
        write_band(**kwargs)

    monkeypatch.setattr(writer.zarr, "open_group", checked_open)
    monkeypatch.setattr(writer, "_write_native_band", checked_write)
    assert append(item, store, _provenance_tracker=tracker)
    assert tracker.calls == ["prepare", "blue", "swir16", "commit"]
    assert tracker.arguments["operation"] == "append" and tracker.arguments["index"] == 0
    assert not tracker.pending.exists()


def test_replacement_changes_only_target_chunks_and_preserves_other_layer(item, tmp_path, monkeypatch):
    store = tmp_path / "replace.zarr"
    second = replace(item, id="scene-b", datetime="2025-02-02T12:00:00Z")
    append(item, store)
    append(second, store)
    before = snapshot(store)
    old = {name: zarr.open_group(str(store), mode="r")[name][1].copy()
           for name in ["10m/blue", "20m/" + variable_name("swir16")]}
    change_sources(item)
    tracker = Tracker(tmp_path / "pending.json")
    monkeypatch.setattr(writer, "_resize_native_arrays", lambda *args, **kwargs: pytest.fail("Replacement resized arrays"))
    assert append(item, store, _provenance_tracker=tracker, on_item_conflict="overwrite")
    assert tracker.arguments["operation"] == "replace" and tracker.arguments["index"] == 0
    after = snapshot(store)
    changed = {name for name in before if before[name] != after[name]}
    assert changed and all("/c/0/" in name for name in changed)
    assert before.keys() == after.keys()
    root = zarr.open_group(str(store), mode="r")
    for name, expected in old.items():
        np.testing.assert_array_equal(root[name][1], expected)
        np.testing.assert_array_equal(root[name][0], expected + 100)
        assert root[name].shape[0] == 2


@pytest.mark.parametrize("changed", [
    {"id": "other-item-same-date"}, {"collection": "another-collection"},
    {"tile_id": "17MNS"}, {"datetime": "2025-01-02T12:00:01Z"},
])
def test_replacement_identity_rejected_before_inputs_or_mutation(item, tmp_path, monkeypatch, changed):
    store = tmp_path / "identity.zarr"
    append(item, store)
    before = snapshot(store)
    tracker = Tracker(tmp_path / "pending.json")
    monkeypatch.setattr(writer, "_resolve_asset_paths", lambda **kw: pytest.fail("Invalid replacement opened input"))
    with pytest.raises(ValueError):
        append(replace(item, **changed), store, _provenance_tracker=tracker, on_item_conflict="overwrite")
    assert snapshot(store) == before and tracker.calls == []


def test_overwrite_without_tracker_is_rejected(item, tmp_path):
    store = tmp_path / "untracked.zarr"
    with pytest.raises(ValueError, match="provenance tracker"):
        append(item, store, on_item_conflict="overwrite")
    assert not store.exists()


def test_direct_writer_cannot_bypass_dataset_pending(item, tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    store = dataset / item.tile_id / "scenes_L2A_native/zarr/s2grits_scenes.zarr"
    pending = dataset / "provenance/pending/op.json"
    pending.parent.mkdir(parents=True)
    pending.write_text('{"phase": "writing"}', encoding="utf-8")
    before = snapshot(dataset)
    monkeypatch.setattr(writer, "_append_item_to_native_zarr_unlocked",
                        lambda **kwargs: pytest.fail("Pending protection was bypassed"))
    with pytest.raises(ValueError, match="Unfinished provenance"):
        append(item, store)
    assert snapshot(dataset) == before and not store.exists()


@pytest.mark.parametrize("replacement", [False, True])
def test_receipt_failure_cannot_roll_back_native_commit(item, tmp_path, monkeypatch, replacement):
    store = tmp_path / "receipt.zarr"
    if replacement:
        append(item, store)
        append(replace(item, id="other", datetime="2025-02-02T12:00:00Z"), store)
        change_sources(item)
    tracker = Tracker(tmp_path / "pending.json", fail_commit=True)
    original = writer._resize_native_arrays
    calls = []
    def observed_resize(*args):
        calls.append(args[-1])
        assert "commit" not in tracker.calls
        return original(*args)
    monkeypatch.setattr(writer, "_resize_native_arrays", observed_resize)
    with pytest.raises(RuntimeError, match="receipt failure"):
        append(item, store, _provenance_tracker=tracker,
               on_item_conflict="overwrite" if replacement else "skip")
    assert tracker.pending.exists() and tracker.calls[-1] == "commit"
    assert calls == ([] if replacement else [0])
    root = zarr.open_group(str(store), mode="r")
    assert root["time"].shape[0] == (2 if replacement else 1)
    entries = json.loads((store / "s2grits_resume_state.json").read_text())
    assert entries["items"][0]["item_id"] == item.id
    for group, band in [("10m", "blue"), ("20m", "swir16")]:
        with rasterio.open(item.assets[band]["href"]) as source:
            np.testing.assert_array_equal(root[group][variable_name(band)][0], source.read(1))


def test_replacement_failure_keeps_pending_and_does_not_resize(item, tmp_path, monkeypatch):
    store = tmp_path / "failure.zarr"
    append(item, store)
    append(replace(item, id="other", datetime="2025-02-02T12:00:00Z"), store)
    before = snapshot(store)
    change_sources(item)
    tracker = Tracker(tmp_path / "pending.json")
    original = writer._write_native_band
    def fail_second(**kwargs):
        if kwargs["band"] == "swir16":
            raise RuntimeError("second band failure")
        original(**kwargs)
    monkeypatch.setattr(writer, "_write_native_band", fail_second)
    monkeypatch.setattr(writer, "_resize_native_arrays", lambda *args: pytest.fail("Partial replacement was resized"))
    with pytest.raises(RuntimeError, match="second band failure"):
        append(item, store, _provenance_tracker=tracker, on_item_conflict="overwrite")
    assert tracker.pending.exists() and "commit" not in tracker.calls
    after = snapshot(store)
    changed = {name for name in before if before[name] != after[name]}
    assert changed and all(name.startswith("10m/blue/c/0/") for name in changed)
    assert before.keys() == after.keys()


@pytest.mark.parametrize("damage", ["dtype", "grid"])
def test_tracked_preflight_checks_all_bands_before_prepare(item, tmp_path, damage):
    asset = item.assets["swir16"]
    path = Path(asset["href"])
    with rasterio.open(path) as source:
        values, profile = source.read(1), source.profile
    if damage == "dtype":
        profile.update(dtype="float32")
        values = values.astype("float32")
    else:
        profile.update(transform=from_origin(500000, 9600000, 30, 30))
    with rasterio.open(path, "w", **profile) as source:
        source.write(values, 1)
    tracker = Tracker(tmp_path / "pending.json")
    store = tmp_path / "preflight.zarr"
    with pytest.raises(ValueError):
        append(item, store, _provenance_tracker=tracker)
    assert not store.exists() and tracker.calls == []
