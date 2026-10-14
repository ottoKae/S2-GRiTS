"""Frozen-scene SCL companion production with bounded cache and publication.

No STAC search, spectral writes, cloud masking, or monthly compositing. Readers
consume only a published companion generation while this producer is stopped.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlparse

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from rasterio.windows import Window
import requests
import yaml
import zarr

from .catalog import CATALOG_SCHEMA, read_catalog, resync_global_catalog, upsert_records
from .compression import make_compressor
from .locking import InterProcessLock
from .stac import feature_to_item
from .workflow import _catalog_records
from .zarr_writer import append_item_to_native_zarr

COMPRESSION = {"cname": "zstd", "clevel": 5, "shuffle": "shuffle"}
ENV_PACKAGES = ("zarr", "xarray", "numcodecs", "numpy", "rasterio", "pyarrow")
GIB = 1024 ** 3


class PixelMismatchError(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__(f"Full-pixel mismatch: {report}")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def environment_versions():
    return {"zarr_format": 3, "python": sys.version.split()[0],
            **{name: importlib.metadata.version(name) for name in ENV_PACKAGES}}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def time_ns(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    require(stamp.tzinfo is not None, "UTC timezone missing")
    return np.datetime64(stamp.astimezone(timezone.utc).replace(tzinfo=None), "ns")


def safe_path(path, root=None):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        require(not parent.is_symlink() and not parent.is_junction(), f"Linked path: {parent}")
    if root is not None:
        root = Path(root).resolve()
        require(path.resolve().is_relative_to(root) and path.resolve() != root,
                f"Path escapes owned root: {path}")
    return path


def store_path(root, tile):
    return Path(root) / tile / "scenes_L2A_native/zarr" / f"s2grits_scenes_{tile}_L2A_native.zarr"


def identity(row):
    return (row["source_collection"], row["tile_id"], row["item_id"])


def _open(path, mode="r"):
    group = zarr.open_group(str(path), mode=mode, use_consolidated=False, zarr_format=3)
    require(group.metadata.zarr_format == 3, "Expected Zarr format 3")
    return group


def _no_consolidated(path):
    for metadata in (Path(path) / "zarr.json", Path(path) / "20m/zarr.json"):
        if metadata.exists():
            require(not load_json(metadata).get("consolidated_metadata"), "Unexpected consolidated metadata")


def _group_location(row, spectral_root, cache):
    relative = str(row["zarr_path"])
    require(not Path(relative).is_absolute(), "Catalog path must be relative")
    path = safe_path(Path(spectral_root) / relative, spectral_root)
    key = (str(path), row["zarr_group"])
    if key not in cache:
        root = _open(path)
        sidecar = load_json(path / "s2grits_resume_state.json")["items"]
        require(len({item["item_id"] for item in sidecar}) == len(sidecar), "Duplicate source sidecar identity")
        require(np.array_equal(root["time"][:], np.asarray([time_ns(item["datetime"]) for item in sidecar])),
                "Spectral root time/sidecar mismatch")
        group = root[row["zarr_group"]]
        times = list(group["time"][:].astype("datetime64[ns]"))
        require(len(set(times)) == len(times), "Duplicate source UTC is unsupported; do not drop items")
        cache[key] = (group, {stamp: index for index, stamp in enumerate(times)},
                      {item["item_id"]: item for item in sidecar})
    group, indices, sidecar = cache[key]
    stamp = time_ns(row["datetime"])
    require(stamp in indices and row["item_id"] in sidecar, "Source scene missing from time/identity axes")
    require(time_ns(sidecar[row["item_id"]]["datetime"]) == stamp, "Source identity/UTC differs")
    attrs = dict(group.attrs)
    require(attrs["grid_id"] == row["grid_id"] and attrs["crs"] == row["crs"] and
            attrs["width"] == row["width"] and attrs["height"] == row["height"] and
            np.allclose(attrs["transform"][:6], row["transform"][:6], rtol=0, atol=1e-9),
            "Source Catalog/grid mismatch")
    return {"store": relative, "group": row["zarr_group"],
            "time_index": indices[stamp], "grid_id": row["grid_id"]}


def load_job(config_path):
    """Read-only preflight; no directory creation or network access."""
    config_path = Path(config_path).resolve()
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    require(cfg.get("workflow") == "quality_companion", "Expected quality_companion workflow")
    require(cfg.get("assets") == {"bands": ["scl"], "resolution_mode": "native_groups", "resampling": "none"},
            "Exact SCL native asset contract required")
    require(not any((cfg.get("processing", {}).get(name) or {}).get("enabled", False)
                    for name in ("monthly", "cloud_mask")), "Quality archive does not process masks/composites")
    quality, output = cfg["quality"], cfg["output"]
    for mapping, names in ((quality, ("spectral_root", "frozen_features", "backfill_manifest", "environment_lock")),
                           (output, ("base_dir", "cache_dir"))):
        for name in names:
            path = Path(mapping[name])
            mapping[name] = safe_path(path if path.is_absolute() else config_path.parent / path)
    spectral, target, cache_root = quality["spectral_root"], output["base_dir"], output["cache_dir"]
    roots = [spectral, target, cache_root]
    require(all(not a.resolve().is_relative_to(b.resolve()) for a in roots for b in roots if a != b)
            and len({str(path.resolve()) for path in roots}) == 3, "Source/quality/cache roots overlap")
    require(cache_root.name != "_cache", "Shared cache cannot be owned by this job")
    require(output["formats"] == {"zarr": True, "cog": False, "preview": False, "raw_assets": False},
            "Only quality Zarr output is supported")
    require(output.get("zarr_compression", COMPRESSION) == COMPRESSION, "Expected Zstd5/byte shuffle")
    require(load_json(quality["environment_lock"]) == environment_versions(), "Locked dependency versions differ")
    limits = cfg["cache"]
    require(limits["retention"] == "delete_after_verified" and limits["max_scenes"] == 1 and
            type(limits["max_bytes"]) is int and 0 < limits["max_bytes"] <= 64 * 1024 ** 2,
            "Expected single-scene cache, at most 64 MiB")
    runtime = cfg["runtime"]
    require(runtime.get("prefetch_scenes", 0) == 0 and runtime.get("chunk_size", 512) == 512,
            "Expected zero prefetch and 512 chunks")
    require(type(runtime.get("max_download_attempts", 3)) is int and
            1 <= runtime.get("max_download_attempts", 3) <= 3, "At most three download attempts")
    require(0 < runtime.get("request_timeout_seconds", 120) <= 120, "Timeout must be in (0,120]")
    require(0 <= runtime.get("min_free_output_gib", 20) and 0 < runtime.get("max_output_gib", 100),
            "Invalid output budget")
    for name in ("frozen_features", "backfill_manifest"):
        require(digest(quality[name]) == quality[name + "_sha256"], f"Changed {name}")
    require(digest(spectral / "catalog.parquet") == quality["spectral_catalog_sha256"], "Spectral Catalog changed")
    features = [json.loads(line) for line in quality["frozen_features"].read_text(encoding="utf-8").splitlines() if line.strip()]
    flat = [json.loads(line) for line in quality["backfill_manifest"].read_text(encoding="utf-8").splitlines() if line.strip()]
    require(features and len(features) == len(flat), "Frozen source/quality manifest counts differ")
    rows = read_catalog(spectral / "catalog.parquet")
    catalog = defaultdict(list)
    for row in rows:
        require(row["status"] == "complete", "Source Catalog contains incomplete rows")
        catalog[(*identity(row), row["zarr_group"])].append(row)
    group_cache, seen, last, scenes = {}, set(), {}, []
    for feature, expected in zip(features, flat):
        item = feature_to_item(feature)
        require(item is not None and item.collection == "sentinel-2-c1-l2a", "Invalid frozen STAC item")
        require(re.fullmatch(r"[A-Za-z0-9_-]+", item.id) and re.fullmatch(r"\d{2}[A-Z]{3}", item.tile_id), "Unsafe identity")
        key = (item.collection, item.tile_id, item.id)
        require(key not in seen, "Duplicate source identity")
        seen.add(key)
        require(item.dt.tzinfo is not None and (item.tile_id not in last or item.dt > last[item.tile_id]),
                "Per-tile UTC must be strictly chronological")
        last[item.tile_id] = item.dt
        asset = item.assets["scl"]
        meta = asset.get("raster:bands", [{}])[0]
        require(meta.get("data_type") == "uint8" and meta.get("nodata") == 0, "SCL uint8/nodata contract differs")
        require(type(asset.get("file:size")) is int and asset["file:size"] > 0 and
                re.fullmatch(r"1220[0-9a-f]{64}", asset.get("file:checksum", "")), "Frozen size/SHA256 missing")
        url = urlparse(asset["href"])
        require(url.scheme == "https" and url.hostname == "e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com",
                "Unexpected frozen public source")
        require((expected["collection"], expected["tile_id"], expected["item_id"]) == key and
                time_ns(expected["datetime_utc"]) == time_ns(item.datetime) and
                expected["href"] == asset["href"] and expected["source_file_bytes"] == asset["file:size"] and
                expected["source_sha256"] == asset["file:checksum"][4:], "Frozen manifests disagree")
        locations = {}
        for group in ("10m", "20m"):
            matches = catalog[(*key, group)]
            require(len(matches) == 1, f"Source Catalog identity/group ambiguity: {key}/{group}")
            row = matches[0]
            require(time_ns(row["datetime"]) == time_ns(item.datetime), "Source Catalog UTC differs")
            locations[group] = _group_location(row, spectral, group_cache)
            if group == "20m":
                require([row["height"], row["width"]] == expected["shape"] and row["crs"] == expected["crs"] and
                        row["grid_id"] == expected["spectral_grid_id"] and
                        np.allclose(row["transform"][:6], expected["transform"][:6], rtol=0, atol=1e-9), "SCL/spectral 20m grids differ")
        scenes.append({"item": item, "expected": expected, "spectral": locations})
    binding = {"config_sha256": digest(config_path), "frozen_features_sha256": quality["frozen_features_sha256"],
               "backfill_manifest_sha256": quality["backfill_manifest_sha256"],
               "spectral_catalog_sha256": quality["spectral_catalog_sha256"],
               "spectral_root": str(spectral.resolve()), "quality_root": str(target.resolve()),
               "cache_root": str(cache_root.resolve()), "environment": environment_versions(),
               "source_sha256": {name: digest(Path(__file__).parent / name) for name in
                                 ("quality_backfill.py", "zarr_writer.py", "workflow.py", "catalog.py",
                                  "canonical_catalog_schema.py", "compression.py", "locking.py", "stac.py")}}
    return cfg, scenes, binding


def _local_source(scene, cfg):
    item = scene["item"]
    for directory in cfg["quality"].get("local_source_candidates", []):
        for path in (Path(directory) / f"{item.id}_SCL.tif", Path(directory) / item.id / "scl.tif"):
            if path.is_file():
                verify_source(path, scene)
                return path
    return None


def verify_source(path, scene):
    expected = scene["expected"]
    require(Path(path).stat().st_size == expected["source_file_bytes"] and digest(path) == expected["source_sha256"],
            "Source COG size/SHA256 differs")
    with rasterio.open(path) as source:
        require(source.count == 1 and source.dtypes == ("uint8",) and source.nodata == 0 and
                [source.height, source.width] == expected["shape"] and str(source.crs) == expected["crs"] and
                np.allclose(list(source.transform)[:6], expected["transform"][:6], rtol=0, atol=1e-9),
                "Source COG native contract differs")


def download_source(scene, path, cfg):
    """Bounded streaming GET; only this owned .part may be replaced on retry."""
    expected = scene["expected"]
    part = path.with_suffix(".tif.part")
    last = None
    for attempt in range(cfg["runtime"].get("max_download_attempts", 3)):
        try:
            with requests.get(expected["href"], stream=True, allow_redirects=False,
                              timeout=(20, cfg["runtime"].get("request_timeout_seconds", 120))) as response:
                require(response.status_code == 200, f"HTTP {response.status_code}")
                if response.headers.get("Content-Length"):
                    require(int(response.headers["Content-Length"]) == expected["source_file_bytes"], "Content-Length changed")
                count = 0
                with part.open("wb") as output:
                    for block in response.iter_content(1024 * 1024):
                        count += len(block)
                        require(count <= expected["source_file_bytes"], "Source exceeds frozen byte budget")
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
            verify_source(part, scene)
            os.replace(part, path)
            return
        except Exception as exc:
            last = exc
            if attempt + 1 < cfg["runtime"].get("max_download_attempts", 3):
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Source download failed: {last}") from last


def cache_inventory(root):
    """Only the owned single-scene cache, never all produced chunks."""
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=_enumeration_error):
        for name in dirs + names:
            safe_path(Path(directory) / name, root)
        for name in names:
            path = Path(directory) / name
            files.append((path, path.stat().st_size))
    return files


def release_cache(root, scene, binding):
    require(load_json(root / ".owner.json") == binding, "Unknown cache ownership")
    directory = safe_path(root / scene["item"].id, root)
    if not directory.exists():
        return
    paths = list(directory.iterdir())
    require({path.name for path in paths} <= {"scl.tif", "scl.tif.part"}, "Unexpected cache files; hold")
    for path in paths:
        safe_path(path, root)
        require(path.is_file(), "Nonregular cache entry")
    for path in paths:
        path.unlink()
    directory.rmdir()


def verify_scene(source_path, store, index, scene):
    """Reopen both persisted products, compare every pixel in bounded windows."""
    started = time.perf_counter()
    root = _open(store)
    _no_consolidated(store)
    group = root["20m"]
    target = group["scl"]
    expected = scene["expected"]
    require(target.dtype == np.dtype("uint8") and list(target.shape[1:]) == expected["shape"], "SCL Zarr dtype/shape differs")
    require(target.chunks == (1, min(512, target.shape[1]), min(512, target.shape[2])) and
            [codec.to_dict() for codec in target.compressors] == [make_compressor(np.dtype("uint8"), COMPRESSION).to_dict()], "SCL codec/chunks differ")
    require(group.attrs["grid_id"] == expected["spectral_grid_id"] and group.attrs["crs"] == expected["crs"] and
            np.allclose(group.attrs["transform"][:6], expected["transform"][:6], rtol=0, atol=1e-9), "SCL Zarr grid differs")
    require(root["time"][index] == group["time"][index] == time_ns(scene["item"].datetime), "SCL Zarr UTC differs")
    compared = mismatched = 0
    with rasterio.open(source_path) as source:
        require(source.dtypes == ("uint8",) and (source.height, source.width) == target.shape[1:], "Source shape/dtype changed")
        for row in range(0, source.height, 512):
            for col in range(0, source.width, 512):
                height, width = min(512, source.height-row), min(512, source.width-col)
                a = source.read(1, window=Window(col, row, width, height))
                b = np.asarray(target[index, row:row+height, col:col+width])
                require(a.dtype == b.dtype and a.shape == b.shape, "Pixel block contract differs")
                require(bool(np.all(a <= 11)), "Invalid source SCL class")
                compared += a.size
                mismatched += int(np.count_nonzero(a != b))
    report = {"compared_pixels": compared, "mismatched_pixels": mismatched,
              "full_pixel_comparison": True, "seconds": time.perf_counter()-started}
    if mismatched:
        raise PixelMismatchError(report)
    return report


def rollback_tail(store, accepted, expected, state):
    """Recover only an owned unaccepted tail; record intent before mutation."""
    _no_consolidated(store)
    sidecar_path = store / "s2grits_resume_state.json"
    require(sidecar_path.exists(), "Nonempty store missing sidecar; hold")
    metadata = load_json(sidecar_path)
    entries = metadata["items"]
    require(all(_receipt_path(state, scene["item"]).exists() for scene in expected[:accepted]) and
            not any(_receipt_path(state, scene["item"]).exists() for scene in expected[accepted:]),
            "Cannot roll back ACCEPTED prefix")
    require(len(entries) in {accepted, accepted+1}, "More than one unaccepted tail")
    require([row["item_id"] for row in entries] == [s["item"].id for s in expected[:len(entries)]], "Sidecar is not frozen prefix")
    root = _open(store, "a")
    prefix = np.asarray([time_ns(s["item"].datetime) for s in expected[:accepted]], dtype="datetime64[ns]")
    for path in ("time", "20m/time", "20m/scl"):
        array = root[path]
        require(array.shape[0] in {accepted, accepted+1}, "Unexpected tail length")
        if path != "20m/scl":
            require(np.array_equal(array[:accepted], prefix), "ACCEPTED time prefix corrupted")
    atomic_json(state / "recovery.json", {"store": str(store), "accepted": accepted, "status": "rollback_intent"})
    for path in ("20m/scl", "20m/time", "time"):
        array = root[path]
        array.resize((accepted, *array.shape[1:]))
    metadata["items"] = entries[:accepted]
    atomic_json(sidecar_path, metadata)
    atomic_json(state / "recovery.json", {"store": str(store), "accepted": accepted, "status": "rolled_back"})


def _file_totals(root):
    count = size = 0
    try:
        Path(root).stat()
    except FileNotFoundError:
        return {"files": 0, "bytes": 0}
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=_enumeration_error):
        for name in dirs + names:
            safe_path(Path(directory) / name, root)
        for name in names:
            count += 1
            size += (Path(directory) / name).stat().st_size
    return {"files": count, "bytes": size}


def _enumeration_error(error):
    raise error


def _receipt_path(state, item):
    return state / "accepted" / f"{item.tile_id}_{item.id}.json"


def _link(scene, store, root, index):
    item = scene["item"]
    row = {"source_collection": item.collection, "tile_id": item.tile_id, "item_id": item.id,
           "datetime": item.datetime, "source_sha256": scene["expected"]["source_sha256"]}
    for group in ("10m", "20m"):
        row.update({f"spectral_{group}_{name}": value for name, value in scene["spectral"][group].items()})
    row.update({"quality_20m_store": store.relative_to(root).as_posix(), "quality_20m_group": "20m",
                "quality_20m_time_index": index, "quality_20m_grid_id": scene["expected"]["spectral_grid_id"]})
    return row


def publish(root, state, binding, scenes, accepted, links):
    """Last replacement is the only stable-generation publication point."""
    result = resync_global_catalog(root, lock_path=state / "locks/global_catalog.lock")
    if result is None:
        pq.write_table(pa.Table.from_pylist([], schema=CATALOG_SCHEMA), root / "catalog.parquet")
    rows = read_catalog(root / "catalog.parquet")
    require(len(rows) == len(links) == sum(accepted.values()), "Catalog/link/accepted counts differ")
    require({identity(row) for row in rows} == {identity(row) for row in links}, "Catalog/link identities differ")
    for tile, count in accepted.items():
        store = store_path(root, tile)
        if not store.exists():
            require(count == 0, "Accepted store missing")
            continue
        group = _open(store)
        require(len(load_json(store / "s2grits_resume_state.json")["items"]) == count and
                all(group[name].shape[0] == count for name in ("time", "20m/time", "20m/scl")),
                "Cannot publish an unaccepted/uncommitted tail")
    temporary = root / "quality_links.parquet.tmp"
    if links:
        pq.write_table(pa.Table.from_pylist(links), temporary)
    else:
        pq.write_table(pa.table({"item_id": pa.array([], type=pa.string())}), temporary)
    os.replace(temporary, root / "quality_links.parquet")
    totals = _file_totals(root)  # Once per batch, not once per scene.
    artifacts = {path.relative_to(root).as_posix(): digest(path)
                 for path in [root / "quality_links.parquet", *root.glob("*/catalog.parquet"),
                              *state.glob("accepted/*.json")]}
    if (root / "catalog.parquet").exists():
        artifacts["catalog.parquet"] = digest(root / "catalog.parquet")
    previous = load_json(root / "companion.json")
    marker = {"state": "published", "contract_version": 1, "zarr_format": 3,
              "consolidated": False, "generation": previous.get("generation", 0)+1,
              "binding": binding, "environment": environment_versions(), "artifacts": artifacts,
              "accepted_prefixes": dict(accepted), "accepted_scenes": sum(accepted.values()),
              "expected_scenes": len(scenes), "complete": sum(accepted.values()) == len(scenes),
              "batch_file_totals_before_marker": totals, "published_utc": datetime.now(timezone.utc).isoformat()}
    atomic_json(root / "companion.json", marker)
    return marker


def run_quality_backfill(config_path, *, dry_run=False, max_new_scenes=None, tiles=None,
                         max_new_scenes_per_tile=None):
    cfg, scenes, binding = load_job(config_path)
    require(max_new_scenes is None or max_new_scenes >= 0, "Negative scene limit")
    require(max_new_scenes_per_tile is None or max_new_scenes_per_tile >= 0, "Negative per-tile scene limit")
    if tiles is not None:
        require(set(tiles) <= {s["item"].tile_id for s in scenes}, "Unknown requested tile")
    root, cache = cfg["output"]["base_dir"], cfg["output"]["cache_dir"]
    reusable = [s for s in scenes if _local_source(s, cfg) is not None]
    if dry_run:
        return {"status": "dry_run", "scenes": len(scenes), "tiles": len({s["item"].tile_id for s in scenes}),
                "reusable_local_COGs": len(reusable), "source_bytes": sum(s["expected"]["source_file_bytes"] for s in scenes),
                "download_started": False, "binding": binding}
    state = root / "_task_state"
    lock = root.parent / ("." + root.name + ".quality.lock")
    with InterProcessLock(lock, timeout=0):
        if root.exists() and any(root.iterdir()):
            require((root / "task_binding.json").exists() and load_json(root / "task_binding.json") == binding,
                    "Unknown or changed nonempty quality root")
        else:
            root.mkdir(parents=True, exist_ok=True)
            atomic_json(root / "task_binding.json", binding)
        if cache.exists() and any(cache.iterdir()):
            require((cache / ".owner.json").exists() and load_json(cache / ".owner.json") == binding, "Unknown cache owner")
        else:
            cache.mkdir(parents=True, exist_ok=True)
            atomic_json(cache / ".owner.json", binding)
        previous = load_json(root / "companion.json") if (root / "companion.json").exists() else {}
        if previous.get("state") == "published":
            for relative, expected_hash in previous["artifacts"].items():
                require(digest(safe_path(root / relative, root)) == expected_hash, "Published artifact changed")
        atomic_json(root / "companion.json", {**previous, "state": "updating", "binding": binding})
        per_tile = defaultdict(list)
        for scene in scenes:
            per_tile[scene["item"].tile_id].append(scene)
        accepted, links = defaultdict(int), []
        for tile, sequence in per_tile.items():
            for index, scene in enumerate(sequence):
                receipt = _receipt_path(state, scene["item"])
                if not receipt.exists():
                    break
                value = load_json(receipt)
                require(value["status"] == "ACCEPTED" and value["binding"] == binding and value["index"] == index,
                        "Accepted receipt identity/binding differs")
                require(value["verification"]["mismatched_pixels"] == 0 and value["verification"]["compared_pixels"] ==
                        int(np.prod(scene["expected"]["shape"])), "Accepted receipt lacks full-pixel evidence")
                accepted[tile] += 1
                require(value["link"] == _link(scene, store_path(root, tile), root, index), "Accepted link differs from source")
                links.append(value["link"])
                release_cache(cache, scene, binding)
            # Receipts after a gap are not allowed to masquerade as a prefix.
            require(not any(_receipt_path(state, s["item"]).exists() for s in sequence[accepted[tile]:]),
                    "Accepted receipt gap")
            store = store_path(root, tile)
            if store.exists():
                require((store / "s2grits_resume_state.json").exists(), "Nonempty store missing sidecar; hold")
                entries = load_json(store / "s2grits_resume_state.json")["items"]
                require(len(entries) in {accepted[tile], accepted[tile]+1}, "Unexpected committed tail count")
                require([e["item_id"] for e in entries] == [s["item"].id for s in sequence[:len(entries)]], "Wrong frozen store prefix")
                group = _open(store)
                prefix = np.asarray([time_ns(s["item"].datetime) for s in sequence[:accepted[tile]]], dtype="datetime64[ns]")
                for name in ("time", "20m/time"):
                    require(np.array_equal(group[name][:accepted[tile]], prefix), "ACCEPTED prefix time differs")
                require(group["20m/scl"].shape[0] >= accepted[tile], "ACCEPTED raster prefix truncated")
        known_cache = {s["item"].id for s in scenes if _receipt_path(state, s["item"]).exists()}
        pending_ids = {sequence[accepted[tile]]["item"].id for tile, sequence in per_tile.items() if accepted[tile] < len(sequence)}
        cache_files = cache_inventory(cache)
        require(sum(size for _, size in cache_files) <= cfg["cache"]["max_bytes"], "Existing cache exceeds budget")
        directories = {p.relative_to(cache).parts[0] for p, _ in cache_files if p.name != ".owner.json"}
        require(len(directories) <= 1 and directories <= known_cache | pending_ids, "Unexpected cache members")
        # A batch-start inventory is sufficient; subsequent checks use deltas.
        output_bytes = _file_totals(root)["bytes"]
        newly_accepted = downloaded_bytes = 0
        new_per_tile = defaultdict(int)
        try:
            for scene in scenes:
                item, expected = scene["item"], scene["expected"]
                sequence = per_tile[item.tile_id]
                index = sequence.index(scene)
                if index < accepted[item.tile_id] or (tiles is not None and item.tile_id not in tiles):
                    continue
                if max_new_scenes is not None and newly_accepted >= max_new_scenes:
                    break
                if max_new_scenes_per_tile is not None and new_per_tile[item.tile_id] >= max_new_scenes_per_tile:
                    continue
                raw_bytes = int(np.prod(expected["shape"]))
                reserve = raw_bytes + 4 * 1024 ** 2
                require(shutil.disk_usage(root).free >= cfg["runtime"].get("min_free_output_gib", 20)*GIB + reserve,
                        "Insufficient output free space")
                require(output_bytes + reserve <= cfg["runtime"].get("max_output_gib", 100)*GIB, "Output budget exhausted")
                own_files = cache_inventory(cache)
                other = [p for p, _ in own_files if p.name != ".owner.json" and p.relative_to(cache).parts[0] != item.id]
                require(not other, "Previous scene cache not released")
                require(sum(size for _, size in own_files) + expected["source_file_bytes"] <= cfg["cache"]["max_bytes"], "Cache reservation exceeds budget")
                require(shutil.disk_usage(cache).free >= expected["source_file_bytes"] + 1024 ** 2, "Insufficient cache free space")
                directory = safe_path(cache / item.id, cache)
                directory.mkdir(exist_ok=True)
                path = safe_path(directory / "scl.tif", cache)
                source_kind = "cache_hit"
                if not path.exists():
                    local = _local_source(scene, cfg)
                    if local is not None:
                        source_kind = "local_reuse"
                        shutil.copyfile(local, path)  # Never remove or mutate the historical source.
                    else:
                        source_kind = "download"
                        download_source(scene, path, cfg)
                        downloaded_bytes += expected["source_file_bytes"]
                verify_source(path, scene)
                atomic_json(state / "pending.json", {"item_id": item.id, "index": index, "state": "SOURCE_VERIFIED"})
                store = store_path(root, item.tile_id)
                # Only this scene's t-chunk directory is measured per scene.
                scene_chunks = store / "20m/scl/c" / str(index)
                before = _file_totals(scene_chunks)["bytes"]
                if store.exists():
                    _no_consolidated(store)
                    g = _open(store)
                    count = len(load_json(store / "s2grits_resume_state.json")["items"])
                    tail_ok = all(g[name].shape[0] == count for name in ("time", "20m/time", "20m/scl"))
                    if count == index+1 and tail_ok:
                        try:
                            verify_scene(path, store, index, scene)
                        except PixelMismatchError:
                            rollback_tail(store, index, sequence, state)
                    elif count != index or not tail_ok:
                        rollback_tail(store, index, sequence, state)
                else:
                    store.mkdir(parents=True)
                    atomic_json(store / "s2grits_resume_state.json", {"items": []})
                # The writer sees an already verified local href, not its legacy downloader.
                from dataclasses import replace
                source_assets = {**item.assets, "scl": {**item.assets["scl"], "href": str(path)}}
                local_item = replace(item, assets=source_assets)
                append_item_to_native_zarr(item=local_item, bands=["scl"], zarr_path=store,
                    chunk_size=512, compression=COMPRESSION, read_mode="remote_stream_first", lock_dir=state / "locks")
                atomic_json(state / "pending.json", {"item_id": item.id, "index": index, "state": "ZARR_COMMITTED"})
                verification = verify_scene(path, store, index, scene)
                records = _catalog_records(item=item, band_groups={"20m": ["scl"]}, zarr_path=store,
                    preview_path=None, output_root=root, stac_api="https://earth-search.aws.element84.com/v1",
                    provider="Element 84 Earth Search / AWS Open Data")
                for record in records:
                    record["product_variant"] = "quality_scl_v1"
                upsert_records(root / item.tile_id / "catalog.parquet", records, lock_path=state / "locks/tile_catalog.lock")
                link = _link(scene, store, root, index)
                receipt = {"status": "ACCEPTED", "binding": binding, "item_id": item.id, "tile_id": item.tile_id,
                           "index": index, "source_sha256": expected["source_sha256"], "source_kind": source_kind,
                           "verification": verification, "link": link}
                atomic_json(_receipt_path(state, item), receipt)
                accepted[item.tile_id] += 1
                links.append(link)
                atomic_json(state / "pending.json", {"item_id": item.id, "index": index, "state": "cache_release_pending"})
                release_cache(cache, scene, binding)
                atomic_json(state / "pending.json", {"item_id": item.id, "index": index, "state": "CACHE_RELEASED"})
                output_bytes += max(0, _file_totals(scene_chunks)["bytes"] - before) + 256 * 1024
                newly_accepted += 1
                new_per_tile[item.tile_id] += 1
            marker = publish(root, state, binding, scenes, accepted, links)
            return {"status": "published", "accepted_this_run": newly_accepted,
                    "accepted_total": marker["accepted_scenes"], "complete": marker["complete"],
                    "generation": marker["generation"], "download_started": downloaded_bytes > 0,
                    "downloaded_source_bytes_this_run": downloaded_bytes}
        except BaseException as exc:
            atomic_json(state / "failure.json", {"error": str(exc), "accepted_prefixes": dict(accepted),
                                                   "verification": getattr(exc, "report", None),
                                                   "utc": datetime.now(timezone.utc).isoformat()})
            raise
