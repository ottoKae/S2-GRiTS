"""Read one committed native Sentinel-2 scene; never fill from another track."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import zarr
from affine import Affine

from .bands import variable_name
from .scene_identity import relative_orbit_number


def _snapshot(store):
    store = Path(store)
    state = store / "s2grits_resume_state.json"
    if not state.exists():
        state = store / "s2grits_metadata.json"
    raw = state.read_bytes()
    items = json.loads(raw).get("items", [])
    if not items or len({item["item_id"] for item in items}) != len(items):
        raise ValueError("Empty or duplicate Sentinel-2 scene commits")
    root = zarr.open_group(str(store), mode="r", use_consolidated=False)
    expected = np.array([
        np.datetime64(item["datetime"].replace("Z", "").replace("+00:00", ""), "ns")
        for item in items
    ])
    groups = root.attrs["resolution_groups"]
    if np.isnat(expected).any() or not np.array_equal(root["time"][:], expected):
        raise ValueError("Uncommitted or inconsistent Sentinel-2 root time axis")
    for name in groups:
        group = root[name]
        if not np.array_equal(group["time"][:], expected):
            raise ValueError(f"Uncommitted or inconsistent Sentinel-2 {name} time axis")
        for array_name in group.array_keys():
            array = group[array_name]
            if array.ndim == 3 and array.shape != (
                len(items), group.attrs["height"], group.attrs["width"]
            ):
                raise ValueError(f"Uncommitted or inconsistent Sentinel-2 {name}/{array_name}")
    scenes = []
    for index, item in enumerate(items):
        if item["tile_id"] != root.attrs["tile_id"]:
            raise ValueError("Scene commit and Sentinel-2 store tile disagree")
        scenes.append({**item, "time_index": index,
                       "relative_orbit": relative_orbit_number(item.get("relative_orbit"))})
    return root, scenes, state, raw


def _unchanged(state, raw):
    if state.read_bytes() != raw:
        raise ValueError("Sentinel-2 archive changed during reading; retry on a stable archive")


def _select(scenes, *, item_id=None, relative_orbit=None, platform=None):
    orbit = relative_orbit_number(relative_orbit)
    return [scene for scene in scenes
            if (item_id is None or scene["item_id"] == item_id)
            and (orbit is None or scene["relative_orbit"] == orbit)
            and (platform is None or scene.get("platform") == platform)]


def list_scenes(store, *, relative_orbit=None, platform=None) -> list[dict]:
    """List committed observations, retaining every item and time, including unknown orbits."""
    _, scenes, state, raw = _snapshot(store)
    result = _select(scenes, relative_orbit=relative_orbit, platform=platform)
    _unchanged(state, raw)
    return result


def list_tracks(store) -> list[dict]:
    """List relative orbits and their scene IDs; None means unrecorded legacy metadata."""
    grouped = {}
    for scene in list_scenes(store):
        key = (scene["relative_orbit"], scene.get("orbit_state"),
               scene["item_id"] if scene["relative_orbit"] is None else None)
        track = grouped.setdefault(key, {
            "relative_orbit": key[0], "orbit_state": key[1], "item_ids": [],
        })
        track["item_ids"].append(scene["item_id"])
    return sorted(grouped.values(), key=lambda track: (
        track["relative_orbit"] is None, track["relative_orbit"] or 0,
        track["orbit_state"] or "",
    ))


def read_scene(store, group="10m", *, item_id=None, relative_orbit=None,
               platform=None, variables=None, window=None) -> dict:
    """Load one scene's raw (y,x) arrays, with declared validity and provenance.

    ``window`` is (row, column, height, width) on this native resolution group.
    Orbit-only selection is allowed only when exactly one scene matches.
    Cloud probability zero is valid within same-scene SCL coverage. Without
    SCL, zero-declared nodata remains ambiguous and is flagged conservatively.
    Per-band validity means source coverage, not cloud screening. No mosaic,
    temporal aggregation, physical scaling or cross-resolution resampling occurs.
    """
    root, scenes, state, raw = _snapshot(store)
    selected = _select(scenes, item_id=item_id, relative_orbit=relative_orbit, platform=platform)
    if not selected:
        raise KeyError("No committed Sentinel-2 scene matches the requested identity")
    if len(selected) != 1:
        raise ValueError("Ambiguous Sentinel-2 selection; choose one item_id from list_scenes()")
    scene = selected[0]
    if group not in scene["resolution_groups"]:
        raise KeyError(f"Scene has no native group {group!r}")
    source = root[group]
    available = [variable_name(band) for band in scene["resolution_groups"][group]]
    if variables is None:
        variables = available
    if isinstance(variables, str) or not variables or len(set(variables)) != len(variables):
        raise ValueError("variables must be a nonempty sequence of unique variable names")
    if any(name not in available for name in variables):
        raise KeyError(f"Variables must belong to native {group}: {available}")
    height, width = source.attrs["height"], source.attrs["width"]
    if window is None:
        window = (0, 0, height, width)
    if len(window) != 4 or any(type(value) is not int for value in window):
        raise ValueError("window must be integer (row, column, height, width)")
    row, column, rows, columns = window
    if min(row, column) < 0 or min(rows, columns) <= 0 or (
        row + rows > height or column + columns > width
    ):
        raise ValueError("window is outside the native Sentinel-2 grid")
    arrays, masks, metadata, methods = {}, {}, {}, {}
    for name in variables:
        array = source[name]
        values = array[scene["time_index"], row:row + rows, column:column + columns]
        nodata = array.attrs.get("nodata")
        valid = np.isfinite(values)
        if nodata is not None:
            valid &= values != nodata
        method = "source_nodata"
        if name == "cloud_probability":
            in_range = np.isfinite(values) & (values >= 0) & (values <= 100)
            if "scl" in available:
                # CLD declares nodata=0, which also means 0% cloud. Only this
                # exact scene's native SCL can distinguish clear zero from fill.
                scl = source["scl"][scene["time_index"], row:row + rows, column:column + columns]
                valid = in_range & (scl != 0)
                method = "same_scene_scl_coverage_and_probability_range"
            else:
                valid &= in_range
                method = ("conservative_source_nodata_zero_ambiguous" if nodata == 0
                          else "source_nodata_and_probability_range")
        arrays[name], masks[name] = values, valid
        metadata[name] = {**dict(array.attrs), "validity_method": method}
        if method == "conservative_source_nodata_zero_ambiguous":
            metadata[name]["validity_note"] = (
                "SCL is unavailable for this scene. Zero may mean 0% cloud or fill; "
                "zero pixels are conservatively excluded, not proven missing."
            )
        methods[name] = method
    _unchanged(state, raw)
    transform = Affine(*source.attrs["transform"][:6]) * Affine.translation(column, row)
    return {
        "scene": scene, "group": group, "data": arrays, "valid": masks,
        "validity_method": methods,
        "band_metadata": metadata, "crs": source.attrs["crs"],
        "transform": list(transform)[:6],
        "x": source["x"][column:column + columns],
        "y": source["y"][row:row + rows],
    }
