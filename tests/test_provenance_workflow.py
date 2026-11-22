"""Real offline COG/cache/native-writer/Catalog round trips and recovery boundaries."""
import copy
import hashlib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
import zarr
from test_provenance_native_writer import item, snapshot  # noqa: F401

from s2grits import provenance_production as production
from s2grits import workflow
from s2grits import zarr_writer as writer
from s2grits.bands import variable_name
from s2grits.catalog import read_catalog
from s2grits.provenance import read_snapshot


@pytest.fixture
def scene_case(request, tmp_path, monkeypatch):
    """Keep HTTP outside this integration test; cache validation remains real."""
    native_item = request.getfixturevalue("item")
    downloads, queries, available, source_bytes = [], [], [], {}
    monkeypatch.setenv("S2GRITS_DOWNLOADER", "requests")

    def query(**kwargs):
        queries.append(kwargs)
        return list(available)

    def download(href, tmp, item_id, band):
        downloads.append((item_id, band))
        tmp.write_bytes(source_bytes[href])

    monkeypatch.setattr(workflow, "search_items", query)
    monkeypatch.setattr(writer, "_cache_asset_with_requests", download)

    def make_scene(name, day, *, increment=0, scale=0.0001, offset=-0.1):
        assets, expected = {}, {}
        for band, original in native_item.assets.items():
            with rasterio.open(original["href"]) as src:
                values = src.read(1) + increment
                profile = {k: src.profile[k] for k in
                           ("width", "height", "count", "dtype", "crs", "transform", "nodata")}
            path = tmp_path / f"{name}-{band}.tif"
            with rasterio.open(path, "w", driver="COG", **profile) as dst:
                dst.write(values, 1)
                dst.scales, dst.offsets = [scale * 10], [offset * 10]
            payload = path.read_bytes()
            href = f"https://example.invalid/{name}/{band}.tif"
            source_bytes[href] = payload
            assets[band] = {"href": href, "file:size": len(payload),
                "file:checksum": "1220" + hashlib.sha256(payload).hexdigest(),
                "raster:bands": [{"data_type": "uint16", "nodata": 0,
                                  "scale": scale, "offset": offset}]}
            expected[band] = values
        scene = replace(native_item, id=name, datetime=f"2025-01-{day:02d}T12:00:00Z", assets=assets,
                        properties={"s2:processing_baseline": "05.11"})
        return scene, expected

    root = tmp_path / "dataset"
    cfg = {"workflow": "scenes", "roi": {"manual_mgrs_tiles": [native_item.tile_id]},
        "time": {"years": [2025], "months": [1]},
        "source": {"read_mode": "local_cache_first"},
        "assets": {"bands": list(native_item.assets)},
        "output": {"base_dir": str(root), "cache_dir": str(tmp_path / "cache"),
                   "logs_dir": str(tmp_path / "logs"), "existing_item": "skip",
                   "formats": {"zarr": True, "cog": False, "preview": False, "raw_assets": False}},
        "runtime": {"chunk_size": 2, "download_scene_workers": 1,
                    "download_band_workers": 1, "downloader": "requests"},
        "metadata": {"provenance": {"enabled": True, "dataset_id": "offline-native", "run_id": "batch-1"}}}
    config_path = tmp_path / "workflow.yaml"

    def run(run_id="batch-1", scenes=None, *, overwrite=False, tracking=True):
        if scenes is not None:
            available[:] = scenes
        cfg["metadata"]["provenance"].update(run_id=run_id, enabled=tracking)
        cfg["output"]["existing_item"] = "overwrite" if overwrite else "skip"
        config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return workflow.run_scenes_workflow(config_path)

    store = root / native_item.tile_id / "scenes_L2A_native/zarr" / f"s2grits_scenes_{native_item.tile_id}_L2A_native.zarr"
    return {"root": root, "store": store, "cfg": cfg, "run": run, "scene": make_scene,
            "downloads": downloads, "queries": queries, "tile": native_item.tile_id}


def _assert_pixels(case, index, expected):
    root = zarr.open_group(str(case["store"]), mode="r", use_consolidated=False)
    for band, group in [("blue", "10m"), ("swir16", "20m")]:
        array = root[group][variable_name(band)]
        assert str(array.dtype) == "uint16"
        np.testing.assert_array_equal(array[index], expected[band])


def test_two_append_batches_preserve_generation_and_per_scene_unapplied_scaling(scene_case):
    case = scene_case
    first, expected_first = case["scene"]("scene-a", 2)
    second, expected_second = case["scene"]("scene-b", 12, increment=100, scale=0.0002, offset=-0.2)
    assert case["run"](scenes=[first])["status"] == "complete"
    before = read_snapshot(case["root"])
    assert case["run"]("batch-2", [first, second])["status"] == "complete"
    after = read_snapshot(case["root"])
    assert len(after["outputs"]) == len(after["source_links"]) == len(after["sources"]) == 4
    assert {r["output_id"]: r for r in before["outputs"]} == {
        r["output_id"]: r for r in after["outputs"] if r["source_item_id"] == first.id}
    assert after["pointer"]["data_revision"] != before["pointer"]["data_revision"]
    assert len(case["queries"]) == 2 and len(case["downloads"]) == 4
    assert not list((case["root"] / "provenance/pending").glob("*.json"))
    assert len(read_catalog(case["root"] / "catalog.parquet")) == 4
    _assert_pixels(case, 0, expected_first)
    _assert_pixels(case, 1, expected_second)
    sources = {s["product_id"]: s for s in after["sources"]}
    assert sources[first.id]["declared_scale"] == 0.0001
    assert sources[first.id]["declared_offset"] == -0.1
    assert sources[second.id]["declared_scale"] == 0.0002
    assert sources[second.id]["declared_offset"] == -0.2
    for key, intent in after["evidence"].items():
        if not key.endswith("intent.json"):
            continue
        assert intent["item"]["properties"]["s2:processing_baseline"] == "05.11"
        assert not intent["processing"]["scale_applied"] and not intent["processing"]["offset_applied"]
        assert not intent["processing"]["harmonization_applied"]
        for band, actual in intent["input_assets"].items():
            declared = intent["item"]["assets"][band]["raster:bands"][0]
            assert actual["scale"] == declared["scale"] * 10
            assert actual["offset"] == declared["offset"] * 10
            assert actual["scale_offset_applied"] is False


def test_skip_existing_scene_does_not_relabel_catalog_or_sources(scene_case):
    case = scene_case
    first, _ = case["scene"]("scene-a", 2)
    case["run"](scenes=[first])
    before, pixels = read_snapshot(case["root"]), snapshot(case["store"])
    catalogs = {path: path.read_bytes() for path in case["root"].rglob("catalog.parquet")}
    revised, _ = case["scene"]("scene-a", 2, increment=100, offset=-0.3)
    n_downloads = len(case["downloads"])
    case["run"]("batch-skip", [revised])
    assert read_snapshot(case["root"]) == before
    assert snapshot(case["store"]) == pixels
    assert all(path.read_bytes() == content for path, content in catalogs.items())
    assert len(case["downloads"]) == n_downloads


def test_replacement_updates_only_target_generations_and_keeps_unrelated_scene(scene_case):
    case = scene_case
    first, _ = case["scene"]("scene-a", 2)
    second, second_values = case["scene"]("scene-b", 12, increment=50)
    case["run"](scenes=[first, second])
    before, pixels = read_snapshot(case["root"]), snapshot(case["store"])
    other_catalog = [r for r in read_catalog(case["root"] / "catalog.parquet") if r["item_id"] == second.id]
    revised, new_values = case["scene"]("scene-a", 2, increment=100, offset=-0.3)
    case["run"]("batch-replace", [revised], overwrite=True)
    after = read_snapshot(case["root"])
    old_rows = {r["output_id"]: r for r in before["outputs"]}
    assert len(after["outputs"]) == 4
    for row in after["outputs"]:
        old = old_rows[row["output_id"]]
        assert row["time_index"] == old["time_index"]
        if row["source_item_id"] == second.id:
            assert row == old
        else:
            assert row["generation_id"] != old["generation_id"]
    assert after["pointer"]["data_revision"] != before["pointer"]["data_revision"]
    assert [r for r in read_catalog(case["root"] / "catalog.parquet") if r["item_id"] == second.id] == other_catalog
    after_pixels = snapshot(case["store"])
    changed = {name for name in pixels if pixels[name] != after_pixels[name]}
    assert changed and all("/c/0/" in name for name in changed)
    assert pixels.keys() == after_pixels.keys()
    assert len(case["downloads"]) == 6
    _assert_pixels(case, 0, new_values)
    _assert_pixels(case, 1, second_values)


def test_receipt_then_catalog_failure_recovers_without_query_download_or_pixel_write(scene_case, monkeypatch):
    case = scene_case
    first, expected = case["scene"]("scene-a", 2)
    original, installs = production._install_catalog, []

    def fail_global(target, staged):
        installs.append(target)
        if target.name == "catalog.parquet" and target.parent.name == case["root"].name:
            raise RuntimeError("injected global Catalog publication failure")
        return original(target, staged)

    monkeypatch.setattr(production, "_install_catalog", fail_global)
    with pytest.raises(RuntimeError, match="global Catalog"):
        case["run"](scenes=[first])
    assert len(installs) == 2
    pending = list((case["root"] / "provenance/pending").glob("*.json"))
    assert len(pending) == 1
    assert (case["root"] / "provenance/runs" / pending[0].stem / "receipt.json").exists()
    assert (case["root"] / case["tile"] / "catalog.parquet").exists()
    assert not (case["root"] / "catalog.parquet").exists()
    pixel_files = snapshot(case["store"])
    counts = len(case["queries"]), len(case["downloads"])
    monkeypatch.setattr(production, "_install_catalog", original)
    monkeypatch.setattr(workflow, "search_items", lambda **kw: pytest.fail("Recovery queried STAC"))
    monkeypatch.setattr(writer, "_cache_asset_with_requests", lambda *args: pytest.fail("Recovery downloaded"))
    monkeypatch.setattr(writer, "_write_native_band", lambda **kw: pytest.fail("Recovery rewrote imagery"))
    assert case["run"]()["resumed"] is True
    assert snapshot(case["store"]) == pixel_files
    assert (len(case["queries"]), len(case["downloads"])) == counts
    assert not pending[0].exists()
    assert len(read_snapshot(case["root"])["outputs"]) == 2
    assert len(read_catalog(case["root"] / "catalog.parquet")) == 2
    _assert_pixels(case, 0, expected)


def test_partial_replacement_without_receipt_blocks_ordinary_workflow(scene_case, monkeypatch):
    case = scene_case
    first, _ = case["scene"]("scene-a", 2)
    case["run"](scenes=[first])
    before = read_snapshot(case["root"])
    original = writer._write_native_band

    def fail_second(**kwargs):
        if kwargs["band"] == "swir16":
            raise RuntimeError("injected partial replacement")
        return original(**kwargs)

    revised, _ = case["scene"]("scene-a", 2, increment=100)
    monkeypatch.setattr(writer, "_write_native_band", fail_second)
    with pytest.raises(RuntimeError, match="partial replacement"):
        case["run"]("batch-broken", [revised], overwrite=True)
    pending = list((case["root"] / "provenance/pending").glob("*.json"))
    assert len(pending) == 1
    assert not (case["root"] / "provenance/runs" / pending[0].stem / "receipt.json").exists()
    assert read_snapshot(case["root"])["pointer"] == before["pointer"]
    after_failure = snapshot(case["root"])
    monkeypatch.setattr(workflow, "search_items", lambda **kw: pytest.fail("Blocked job queried STAC"))
    with pytest.raises(ValueError, match="unfinished provenance"):
        case["run"]("ordinary", tracking=False)
    assert snapshot(case["root"]) == after_failure


@pytest.mark.parametrize("changed", ["config", "source"])
def test_same_run_rejects_changed_configuration_or_frozen_source_declaration(scene_case, changed):
    case = scene_case
    scene, _ = case["scene"]("scene-a", 2)
    invalid = copy.deepcopy(scene.assets)
    invalid["swir16"]["raster:bands"][0]["data_type"] = "float32"
    scene = replace(scene, assets=invalid)
    with pytest.raises(ValueError, match="dtype"):
        case["run"](scenes=[scene])
    assert not list((case["root"] / "provenance/pending").glob("*.json"))
    before = snapshot(case["root"])
    counts = len(case["queries"]), len(case["downloads"])
    if changed == "config":
        case["cfg"]["runtime"]["chunk_size"] = 4
        match = "changed inputs/configuration"
    else:
        changed_assets = copy.deepcopy(scene.assets)
        changed_assets["blue"]["raster:bands"][0]["offset"] = -0.2
        scene = replace(scene, assets=changed_assets)
        match = "source membership changed"
    with pytest.raises(ValueError, match=match):
        case["run"](scenes=[scene])
    assert len(case["downloads"]) == counts[1]
    assert len(case["queries"]) == counts[0] + (changed == "source")
    assert snapshot(case["root"]) == before


def test_dataset_identity_change_rejected_before_query_cache_or_replacement(scene_case, monkeypatch):
    case = scene_case
    scene, _ = case["scene"]("scene-a", 2)
    case["run"](scenes=[scene])
    before = snapshot(case["root"])
    counts = len(case["queries"]), len(case["downloads"])
    case["cfg"]["metadata"]["provenance"]["dataset_id"] = "another-dataset"
    monkeypatch.setattr(workflow, "search_items", lambda **kw: pytest.fail("Identity mismatch queried STAC"))
    with pytest.raises(ValueError, match="dataset_id differs"):
        case["run"]("wrong-dataset", [scene], overwrite=True)
    assert snapshot(case["root"]) == before
    assert (len(case["queries"]), len(case["downloads"])) == counts


@pytest.mark.parametrize("section,key,value", [
    ("metadata.provenance", "dataset_id", 123),
    ("metadata.provenance", "run_id", "../escape"),
    ("metadata.provenance", "enabled", "true"),
    ("output", "existing_item", "replace-any"),
    ("output.formats", "preview", True),
    ("runtime", "download_scene_workers", 2),
    ("source", "read_mode", "remote_stream_first"),
    ("assets", "bands", ["blue", "swir16", "scl"]),
])
def test_tracking_limits_reject_before_network_or_dataset_creation(scene_case, monkeypatch, section, key, value):
    case = scene_case
    cfg = case["cfg"]
    for part in section.split("."):
        cfg = cfg[part]
    cfg[key] = value
    # run() sets these two switches; write the configuration directly for this check.
    config = case["root"].parent / "invalid.yaml"
    config.write_text(yaml.safe_dump(case["cfg"]), encoding="utf-8")
    monkeypatch.setattr(workflow, "search_items", lambda **kw: pytest.fail("Invalid config queried STAC"))
    with pytest.raises(ValueError):
        workflow.run_scenes_workflow(config)
    assert not case["root"].exists() and not case["downloads"]


def test_documented_native_example_and_pairing_cli_contract():
    from s2grits.cli import build_parser
    from s2grits.config import load_config, validate_config
    config = Path(__file__).resolve().parents[1] / "config/provenance_native.example.yaml"
    assert validate_config(load_config(config)) == []
    args = build_parser().parse_args(["rebind_quality", "--config", "local.yaml", "--run-id", "pair-v2", "--dry-run"])
    assert args.run_id == "pair-v2" and args.dry_run
