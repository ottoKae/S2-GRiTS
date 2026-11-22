"""Resume and cache regressions: tiny local rasters only, no production data."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pytest
import zarr

from s2grits import cli, zarr_writer as writer
from s2grits.audit import _store_warnings
from s2grits.bands import selected_native_groups
from test_native_writer_parallel import scene


def append(item, store, bands=("blue", "green")):
    return writer.append_item_to_native_zarr(
        item=item, bands=list(bands), zarr_path=store, chunk_size=16,
        read_mode="remote_stream_first",
    )


def snapshot(store):
    return {str(path.relative_to(store)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in store.rglob("*") if path.is_file()}


@pytest.mark.parametrize("damage", ["missing", "invalid", "short_array"])
def test_resume_rejects_lost_commits_before_any_mutation(scene, tmp_path, damage):
    store = tmp_path / "archive.zarr"
    append(scene, store)
    append(replace(scene, id="scene-2", datetime="2018-02-01T12:34:56Z"), store)
    sidecar = store / "s2grits_resume_state.json"
    if damage == "missing":
        sidecar.unlink()
    elif damage == "invalid":
        sidecar.write_text("{}")
    else:
        root = zarr.open_group(str(store), mode="a")
        array = root["10m/green"]
        array.resize((1, *array.shape[1:]))
    before = snapshot(store)
    with pytest.raises(ValueError, match="commit record"):
        append(replace(scene, id="scene-3", datetime="2018-03-01T12:34:56Z"), store)
    assert snapshot(store) == before


@pytest.mark.parametrize("bands", [("blue",), ("blue", "green", "red"), ("blue", "green", "nir08")])
@pytest.mark.parametrize("repeat_item", [False, True])
def test_resume_band_change_is_rejected_before_skip_or_mutation(scene, tmp_path, bands, repeat_item):
    store = tmp_path / "archive.zarr"
    append(scene, store)
    before = snapshot(store)
    item = scene if repeat_item else replace(scene, id="scene-2")
    with pytest.raises(ValueError, match="band/group contract"):
        append(item, store, bands)
    assert snapshot(store) == before


def test_legacy_contract_resumes_and_uncommitted_tail_is_rolled_back(scene, tmp_path):
    store = tmp_path / "archive.zarr"
    append(scene, store)
    (store / "s2grits_resume_state.json").rename(store / "s2grits_metadata.json")
    root = zarr.open_group(str(store), mode="a")
    del root.attrs["band_groups"]  # infer pre-upgrade stores from commits and arrays
    first = root["10m/blue"][0].copy()
    root["10m/blue"].resize((2, *first.shape))
    root["10m/blue"][1] = 999
    assert append(replace(scene, id="scene-2"), store, ("green", "blue"))
    root = zarr.open_group(str(store), mode="r")
    np.testing.assert_array_equal(root["10m/blue"][0], first)
    np.testing.assert_array_equal(root["10m/blue"][1], first)
    assert _store_warnings(store, selected_native_groups(["blue", "green"]), 2) == []


def test_audit_covers_arrays_omitted_by_current_configuration(scene, tmp_path):
    store = tmp_path / "archive.zarr"
    append(scene, store)
    root = zarr.open_group(str(store), mode="a")
    root["10m/green"].resize((0, *root["10m/green"].shape[1:]))
    warnings = _store_warnings(store, {"10m": ["blue"]}, 1)
    assert any("contract differs" in warning for warning in warnings)
    assert any("10m/green time length=0" in warning for warning in warnings)


@pytest.mark.parametrize("status,audit_ok", [("failed", False), ("partial", True), ("success", False)])
def test_cli_failure_prints_result_and_exits_nonzero(monkeypatch, capsys, status, audit_ok):
    result = {"audit_report": {"ok": audit_ok}, "results": {"49QHE": {"status": status}}}
    monkeypatch.setattr(cli, "run_scenes_workflow", lambda _: result)
    with pytest.raises(SystemExit) as error:
        cli.main(["process_scenes", "--config", "unused.yaml"])
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == result


def test_cli_success_retains_zero_exit(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_scenes_workflow", lambda _: {
        "audit_report": {"ok": True}, "results": {"49QHE": {"status": "success"}},
    })
    assert cli.main(["process_scenes", "--config", "unused.yaml"]) is None
    assert json.loads(capsys.readouterr().out)["audit_report"]["ok"]


@pytest.fixture
def cache_source(scene, monkeypatch):
    calls = []
    source = Path(scene.assets["blue"]["href"])
    def download(href, path, item_id, band):
        calls.append((href, path.exists()))
        shutil.copyfile(source, path)
    monkeypatch.setattr(writer, "_aria2c_path", lambda: None)
    monkeypatch.setattr(writer, "_cache_asset_with_requests", download)
    monkeypatch.setattr(writer.time, "sleep", lambda _: None)
    return source, calls


def test_cache_reuses_signatures_but_rebinds_source_and_collection(cache_source, tmp_path):
    source, calls = cache_source
    cache = tmp_path / "cache"
    href = "https://example.invalid/blue.tif?versionId=1&sig=old&se=old"
    metadata = {"file:size": source.stat().st_size,
                "file:checksum": "1220" + writer._file_sha256(source)}
    def fetch(url, collection="collection-a"):
        return writer.cache_asset(url, cache, "scene", "blue", collection=collection, asset=metadata)
    out = fetch(href)
    assert fetch(href.replace("sig=old&se=old", "se=new&sig=new")) == out
    assert len(calls) == 1
    fetch(href.replace("versionId=1", "versionId=2"))
    fetch(href.replace("versionId=1", "versionId=2"), collection="collection-b")
    assert len(calls) == 3
    receipt = json.loads(out.with_suffix(".tif.source.json").read_text())
    assert receipt["binding"]["href"] == "https://example.invalid/blue.tif?versionId=2"
    assert receipt["sha256"] == writer._file_sha256(source)


def test_cache_does_not_trust_legacy_files_or_changed_bytes(cache_source, tmp_path):
    source, calls = cache_source
    out = tmp_path / "cache" / "scene" / "blue.tif"
    out.parent.mkdir(parents=True)
    shutil.copyfile(source, out)
    href = "https://example.invalid/blue.tif"
    assert writer.cache_asset(href, tmp_path / "cache", "scene", "blue") == out
    assert len(calls) == 1
    data = bytearray(out.read_bytes())
    data[-1] ^= 1
    out.write_bytes(data)
    writer.cache_asset(href, tmp_path / "cache", "scene", "blue")
    assert len(calls) == 2
    assert out.read_bytes() == source.read_bytes()


def test_cache_drops_unknown_partial_before_request(cache_source, tmp_path):
    _, calls = cache_source
    partial = tmp_path / "cache" / "scene" / "blue.tif.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"other-source-partial")
    writer.cache_asset("https://example.invalid/blue.tif", tmp_path / "cache", "scene", "blue")
    assert calls == [("https://example.invalid/blue.tif", False)]


def test_aria_fallback_restarts_instead_of_resuming_sparse_pieces(cache_source, tmp_path, monkeypatch):
    _, calls = cache_source
    monkeypatch.setattr(writer, "_aria2c_path", lambda: "aria2c")
    def incomplete(href, path, item_id, band):
        path.write_bytes(b"noncontiguous-multirange-pieces")
        Path(str(path) + ".aria2").write_text("piece-map")
        raise RuntimeError("interrupted aria2 transfer")
    monkeypatch.setattr(writer, "_cache_asset_with_aria2c", incomplete)
    writer.cache_asset("https://example.invalid/blue.tif", tmp_path / "cache", "scene", "blue")
    assert calls == [("https://example.invalid/blue.tif", False)]


@pytest.mark.parametrize("metadata", [{"file:size": 1}, {"file:checksum": "1220" + "0" * 64}])
def test_download_metadata_mismatch_never_publishes_receipt(cache_source, tmp_path, metadata):
    with pytest.raises(RuntimeError, match="declared"):
        writer.cache_asset("https://example.invalid/blue.tif", tmp_path / "cache", "scene", "blue", asset=metadata)
    assert not (tmp_path / "cache" / "scene" / "blue.tif").exists()
    assert not (tmp_path / "cache" / "scene" / "blue.tif.source.json").exists()
