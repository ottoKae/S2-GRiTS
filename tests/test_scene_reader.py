import json
from dataclasses import replace

import numpy as np
import pytest
import rasterio
import zarr
from rasterio.transform import from_origin

from s2grits.reader import list_scenes, list_tracks, read_scene
from s2grits.scene_identity import scene_identity
from s2grits.stac import S2Item
from s2grits.workflow import _catalog_records
from s2grits.zarr_writer import append_item_to_native_zarr


def make_item(item_id="scene-a", orbit=32, day="20241126", assets=None):
    return S2Item(
        id=item_id, collection="sentinel-2-c1-l2a", datetime=f"{day[:4]}-{day[4:6]}-{day[6:]}T03:00:00Z",
        tile_id="49QHE", cloud_cover=0.0, platform="sentinel-2b", bbox=None,
        geometry=None, assets=assets or {}, links=[], properties={
            "s2:product_uri": f"S2B_MSIL2A_{day}T030000_N0511_R{orbit:03d}_T49QHE_{day}T050000.SAFE",
            "s2:datatake_id": f"GS2B_{day}T030000_040000_N05.11",
        },
    )


@pytest.fixture
def native_store(tmp_path):
    store = tmp_path / "native.zarr"
    for index, (name, orbit, day) in enumerate([
        ("scene-a", 32, "20241126"), ("scene-b", 132, "20241126"),
        ("scene-c", 32, "20241129"),
    ]):
        assets = {}
        for band, resolution in (("blue", 10), ("scl", 20), ("cloud", 20), ("coastal", 60)):
            path = tmp_path / f"{name}-{band}.tif"
            size = 120 // resolution
            dtype = "uint8" if band in {"scl", "cloud"} else "uint16"
            values = np.full((size, size), index + 1, dtype=dtype)
            values[index % size, 0] = 0
            with rasterio.open(path, "w", driver="GTiff", width=size, height=size,
                               count=1, dtype=dtype, crs="EPSG:32649", nodata=0,
                               transform=from_origin(799980, 2500020, resolution, resolution)) as ds:
                ds.write(values, 1)
            assets[band] = {"href": str(path), "gsd": resolution,
                            "raster:bands": [{"data_type": dtype, "nodata": 0}]}
        item = make_item(name, orbit, day, assets)
        append_item_to_native_zarr(item=item, bands=list(assets), zarr_path=store,
                                  chunk_size=4, read_mode="remote_stream_first")
    return store


def test_metadata_keeps_distinct_tracks_and_same_timestamp_scenes(native_store):
    scenes = list_scenes(native_store)
    assert [scene["item_id"] for scene in scenes] == ["scene-a", "scene-b", "scene-c"]
    assert scenes[0]["datetime"] == scenes[1]["datetime"]
    assert [track["relative_orbit"] for track in list_tracks(native_store)] == [32, 132]
    assert [scene["item_id"] for scene in list_scenes(native_store, relative_orbit=32)] == [
        "scene-a", "scene-c"]
    records = _catalog_records(item=make_item(), band_groups={"10m": ["blue"]},
        zarr_path=native_store, preview_path=None, output_root=native_store.parent,
        stac_api="https://example.test", provider="test")
    assert records[0]["track"] == 32


def test_exact_scene_masks_do_not_fill_from_other_orbit(native_store):
    a = read_scene(native_store, item_id="scene-a", window=(0, 0, 2, 2))
    b = read_scene(native_store, relative_orbit=132, window=(0, 0, 2, 2))
    assert a["data"]["blue"].tolist() == [[0, 1], [1, 1]]
    assert b["data"]["blue"].tolist() == [[2, 2], [0, 2]]
    assert a["valid"]["blue"].tolist() == [[False, True], [True, True]]
    assert a["validity_method"]["blue"] == "source_nodata"
    assert b["scene"]["item_id"] == "scene-b"
    assert a["transform"] == [10, 0, 799980, 0, -10, 2500020]
    for group, name, shape in (("20m", "scl", (6, 6)), ("60m", "coastal", (2, 2))):
        result = read_scene(native_store, group, item_id="scene-b", variables=[name])
        assert result["data"][name].shape == shape
        assert len(result["x"]) == shape[1]


@pytest.mark.parametrize("selection", [{}, {"relative_orbit": 32}])
def test_mixed_orbit_and_multiple_dates_require_exact_scene(native_store, selection):
    with pytest.raises(ValueError, match="Ambiguous"):
        read_scene(native_store, **selection)


def test_conflicting_selectors_and_unknown_variables_rejected(native_store):
    with pytest.raises(KeyError, match="No committed"):
        read_scene(native_store, item_id="scene-a", relative_orbit=132)
    with pytest.raises(KeyError, match="Variables"):
        read_scene(native_store, item_id="scene-a", variables=["coastal"])
    with pytest.raises(ValueError, match="outside"):
        read_scene(native_store, item_id="scene-a", window=(0, 0, 20, 20))


def test_legacy_unknown_orbits_remain_readable_by_item_id(native_store):
    state = native_store / "s2grits_resume_state.json"
    metadata = json.loads(state.read_text())
    for item in metadata["items"]:
        item.pop("relative_orbit")
    state.write_text(json.dumps(metadata))
    assert list_tracks(native_store)[0]["relative_orbit"] is None
    assert len(list_tracks(native_store)) == 3
    assert all(len(track["item_ids"]) == 1 for track in list_tracks(native_store))
    assert list_scenes(native_store, relative_orbit=32) == []
    assert read_scene(native_store, item_id="scene-a", window=(0, 0, 1, 1))["data"]["blue"][0, 0] == 0


def test_uncommitted_tail_and_wrong_time_are_rejected(native_store):
    root = zarr.open_group(str(native_store), mode="a", use_consolidated=False)
    root["10m/blue"].resize((4, 12, 12))
    with pytest.raises(ValueError, match="Uncommitted"):
        list_scenes(native_store)
    root["10m/blue"].resize((3, 12, 12))
    root["20m/time"][0] = np.datetime64("2020-01-01", "ns")
    with pytest.raises(ValueError, match="time axis"):
        read_scene(native_store, item_id="scene-a")


def test_zero_cloud_probability_uses_exact_scene_scl_coverage(native_store):
    root = zarr.open_group(str(native_store), mode="a", use_consolidated=False)
    root["20m/cloud_probability"][0, :2, :3] = np.array([[0, 0, 100], [0, 101, 255]], dtype="uint8")
    patch = read_scene(native_store, "20m", item_id="scene-a", variables=["cloud_probability"],
                       window=(0, 0, 2, 3))
    assert patch["data"]["cloud_probability"].tolist() == [[0, 0, 100], [0, 101, 255]]
    # Scene A has fill at [0,0]; scene B has coverage there. Never borrow B's SCL.
    assert patch["valid"]["cloud_probability"].tolist() == [[False, True, True], [True, False, False]]
    assert patch["validity_method"]["cloud_probability"] == "same_scene_scl_coverage_and_probability_range"
    assert patch["band_metadata"]["cloud_probability"]["nodata"] == 0


def test_zero_cloud_without_scl_explicitly_reports_ambiguous_validity(native_store):
    root = zarr.open_group(str(native_store), mode="a", use_consolidated=False)
    root["20m/cloud_probability"][0, :2, :2] = np.array([[0, 50], [100, 101]], dtype="uint8")
    state = native_store / "s2grits_resume_state.json"
    metadata = json.loads(state.read_text())
    metadata["items"][0]["resolution_groups"]["20m"].remove("scl")
    state.write_text(json.dumps(metadata))
    patch = read_scene(native_store, "20m", item_id="scene-a", variables=["cloud_probability"],
                       window=(0, 0, 2, 2))
    assert patch["valid"]["cloud_probability"].tolist() == [[False, True], [True, False]]
    assert patch["validity_method"]["cloud_probability"] == "conservative_source_nodata_zero_ambiguous"
    assert "not proven missing" in patch["band_metadata"]["cloud_probability"]["validity_note"]


def test_product_uri_orbit_fallback_and_acquisition_key_ignore_baseline():
    item = make_item()
    identity = scene_identity(item)
    assert identity["relative_orbit"] == 32
    assert identity["acquisition_id"] == "S2B_20241126T030000_040000"
    updated = replace(item, properties={**item.properties,
        "s2:datatake_id": "GS2B_20241126T030000_040000_N06.00"})
    assert scene_identity(updated)["acquisition_id"] == identity["acquisition_id"]
    assert scene_identity(replace(item, properties={}))["relative_orbit"] is None
    assert scene_identity(make_item(orbit=143))["relative_orbit"] == 143


@pytest.mark.parametrize("changes", [
    {"sat:relative_orbit": 132}, {"sat:relative_orbit": 32.5},
    {"sat:relative_orbit": True}, {"sat:relative_orbit": 144},
    {"s2:datatake_id": "GS2A_20241126T030000_040000_N05.11"},
    {"s2:product_uri": "invalid"}, {"sat:orbit_state": "unknown"},
])
def test_invalid_or_conflicting_source_identity_is_rejected(changes):
    item = make_item()
    with pytest.raises(ValueError):
        scene_identity(replace(item, properties={**item.properties, **changes}))
