"""Opt-in native S2 scene journal under the existing selected-archive root lock."""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import pyarrow.parquet as pq
import rasterio
import zarr

from .bands import selected_native_groups, variable_name
from .catalog import CATALOG_SCHEMA, _dedupe_sort, _write_catalog, read_catalog
from .provenance import (
    _digest,
    _read_release,
    atomic_json,
    load_json,
    publish_snapshot,
    read_current,
    read_snapshot,
    safe_relative,
)
from .provenance_contract import canonical_json, logical_output_id, sanitize_asset_url
from .scene_identity import scene_identity
from .zarr_writer import _file_sha256, _load_metadata, _validate_resume_contract


def validate_tracking_config(cfg):
    metadata = cfg.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise TypeError("metadata must be a mapping")
    options = metadata.get("provenance", {})
    if not isinstance(options, dict) or type(options.get("enabled", False)) is not bool:
        raise ValueError("metadata.provenance.enabled must be true or false")
    if not options.get("enabled", False):
        if cfg.get("output", {}).get("existing_item", "skip") != "skip":
            raise ValueError("existing_item=overwrite requires provenance tracking")
        return False
    for key in ("dataset_id", "run_id"):
        if not isinstance(options.get(key), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", options[key]):
            raise ValueError("provenance " + key + " must be a safe stable identifier")
    out, runtime = cfg["output"], cfg["runtime"]
    if out.get("existing_item", "skip") not in {"skip", "overwrite"} or out.get("existing_store", "resume") != "resume":
        raise ValueError("tracked native scenes require resume and existing_item skip/overwrite")
    if not out["formats"].get("zarr", True) or any(out["formats"].get(k) for k in ("cog", "preview", "raw_assets")):
        raise ValueError("tracked scenes currently require Zarr only")
    if cfg["source"].get("read_mode") != "local_cache_first" or runtime.get("download_scene_workers", 1) != 1:
        raise ValueError("tracked scenes require local_cache_first and one scene worker")
    if any(cfg["processing"].get(k, {}).get("enabled", False) for k in ("monthly", "cloud_mask")):
        raise ValueError("tracked scenes preserve raw native values; no masks or composites")
    if "scl" in cfg["assets"]["bands"]:
        raise ValueError("archive SCL through the separate quality_companion workflow")
    return True


def assert_no_pending(root):
    if list(safe_relative(root, "provenance/pending").glob("*.json")):
        raise ValueError("unfinished provenance write blocks this dataset")


def _plain(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return _plain(value.item())
    return value


def item_declaration(item, bands):
    properties = {key: item.properties.get(key) for key in (
        "s2:processing_baseline", "s2:product_uri", "s2:product_type", "processing:level",
        "s2:generation_time", "created", "updated", "sat:relative_orbit", "sat:orbit_state")}
    assets = {}
    for band in bands:
        source = item.assets[band]
        asset = {key: source.get(key) for key in ("raster:bands", "file:checksum", "file:size",
            "checksum:multihash", "gsd", "proj:epsg", "proj:shape", "proj:transform")}
        asset["href"] = sanitize_asset_url(source["href"])
        assets[band] = asset
    return {"collection": item.collection, "item_id": item.id, "tile_id": item.tile_id,
            "datetime": item.datetime, "properties": properties, "assets": assets,
            "identity": scene_identity(item)}


def _producer():
    names = ("provenance_production.py", "provenance.py", "provenance_contract.py",
             "workflow.py", "zarr_writer.py", "asset_cache.py", "catalog.py", "scene_identity.py")
    folder = Path(__file__).parent
    return {"code_sha256": {name: _file_sha256(folder / name) for name in names},
            "dependencies": {name: importlib.metadata.version(name) for name in
                ("numpy", "rasterio", "zarr", "pyarrow", "pyproj")}}


def observe_stores(root, paths):
    outputs, witnesses = [], {}
    for relative in sorted(set(paths)):
        path = safe_relative(root, relative)
        if not path.exists():
            continue
        metadata = _load_metadata(path)
        group = zarr.open_group(str(path), mode="r", use_consolidated=False)
        band_groups = group.attrs["band_groups"]
        entries = metadata["items"]
        times = np.asarray([np.datetime64(e["datetime"].replace("Z", "").replace("+00:00", ""), "ns") for e in entries])
        witnesses[relative + "/s2grits_resume_state.json"] = _file_sha256(path / "s2grits_resume_state.json")
        for name in ["", *band_groups]:
            g = group[name] if name else group
            if not np.array_equal(g["time"][:], times):
                raise ValueError("tracked native store time/commit identity differs")
            arrays = {n: {"shape": list(a.shape), "dtype": str(a.dtype), "chunks": list(a.chunks)} for n, a in g.arrays()}
            witnesses[relative + "/" + (name + "/" if name else "") + "zarr.json"] = _digest(canonical_json(
                {"attrs": _plain(dict(g.attrs)), "arrays": arrays, "times": [str(t) for t in times]}))
        for name, bands in band_groups.items():
            g = group[name]
            variables = [variable_name(b) for b in bands]
            if any(g[v].shape[0] != len(entries) for v in variables):
                raise ValueError("native store has an uncommitted or truncated band")
            for index, entry in enumerate(entries):
                identity = {"collection": group.attrs["collection"], "tile_id": group.attrs["tile_id"],
                            "item_id": entry["item_id"], "group": name, "grid_id": g.attrs["grid_id"]}
                outputs.append({"output_id": logical_output_id(identity), "generation_id": None,
                    "store_path": relative, "zarr_group": name, "variable_scope": variables,
                    "tile_id": group.attrs["tile_id"], "geometry_group_id": group.attrs["tile_id"],
                    "grid_id": g.attrs["grid_id"], "product_type": "scenes", "record_kind": "scene",
                    "time_index": index, "time_value_utc": entry["datetime"], "period_start": None,
                    "period_end": None, "source_item_id": entry["item_id"], "run_id": None,
                    "processing_id": None, "evidence_status": "artifact_observed", "evidence_ref": None,
                    "unknown_reason": "Historical execution and source transformations are not established."})
    return {"outputs": outputs, "witnesses": witnesses}


def _carry(observed, previous):
    old = {row["output_id"]: row for row in previous["outputs"]}
    for row in observed["outputs"]:
        if row["output_id"] in old:
            for key in ("generation_id", "run_id", "processing_id", "evidence_status", "evidence_ref", "unknown_reason"):
                row[key] = old[row["output_id"]].get(key)
    return observed


def _digest_or_none(path):
    return _file_sha256(path) if path.exists() else None


def _install_catalog(target, staged):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".provenance.tmp")
    temporary.write_bytes(staged.read_bytes())
    os.replace(temporary, target)


class SceneRun:
    def __init__(self, root, dataset_id, run_id, context):
        self.root = Path(root).resolve()
        self.dataset_id, self.run_id, self.context = dataset_id, run_id, context
        self.folder = safe_relative(root, "provenance/runs/" + run_id)
        self.pending = safe_relative(root, "provenance/pending/" + run_id + ".json")
        self.used_bands = set()

    def prepare(self, *, item, bands, zarr_path, asset_paths, band_groups, index, operation, parameters):
        assert_no_pending(self.root)
        if (self.folder / "intent.json").exists():
            raise ValueError("existing scene intent must be recovered, never overwritten")
        previous = read_snapshot(self.root)
        if previous["dataset"] and previous["dataset"]["dataset_id"] != self.dataset_id:
            raise ValueError("dataset_id differs from the published snapshot")
        relative = Path(zarr_path).resolve().relative_to(self.root).as_posix()
        paths = sorted({row["store_path"] for row in previous["outputs"]} | {relative})
        before = _carry(observe_stores(self.root, paths), previous)
        old_paths = {row["store_path"] for row in previous["outputs"]}
        if previous["pointer"]:
            current_observation = _carry(observe_stores(self.root, old_paths), previous)
            if canonical_json(current_observation) != canonical_json({key: previous[key] for key in ("outputs", "witnesses")}):
                # Published Parquet normalizes null columns and sorts rows.
                old_rows = {r["output_id"]: r for r in previous["outputs"]}
                new_rows = {r["output_id"]: r for r in current_observation["outputs"]}
                if old_rows != new_rows or current_observation["witnesses"] != previous["witnesses"]:
                    raise ValueError("published spectral generation is stale")
        declaration = item_declaration(item, bands)
        sources, actual = [], {}
        for band in bands:
            path = Path(asset_paths[band])
            if not path.is_file():
                raise ValueError("tracked inputs require verified local assets")
            digest = _file_sha256(path)
            meta = item.assets[band].get("raster:bands", [{}])[0]
            with rasterio.open(path) as ds:
                actual[band] = {"sha256": digest, "bytes": path.stat().st_size,
                    "dtype": ds.dtypes[0], "nodata": ds.nodata, "scale": ds.scales[0], "offset": ds.offsets[0],
                    "scale_offset_applied": False, "BOA_ADD_OFFSET": None,
                    "boa_unknown_reason": "No upstream product XML correction history was verified."}
            sid = "source-" + _digest(canonical_json({"item": declaration["item_id"], "collection": item.collection,
                "band": band, "href": declaration["assets"][band]["href"], "sha256": digest}))
            sources.append({"source_id": sid, "collection": item.collection, "product_id": item.id,
                "asset_key": band, "href": declaration["assets"][band]["href"], "source_sha256": digest,
                "declared_scale": meta.get("scale"), "declared_offset": meta.get("offset"),
                "evidence_status": "artifact_observed", "evidence_ref": None})
        catalog_before = {name: _digest_or_none(safe_relative(self.root, name)) for name in
                          (item.tile_id + "/catalog.parquet", "catalog.parquet")}
        intent = {"schema_version": 1, "run_id": self.run_id, "dataset_id": self.dataset_id,
            "context": self.context, "item": declaration, "store_path": relative,
            "store_paths": paths, "index": index, "operation": operation,
            "generation_id": "gen-" + uuid4().hex, "previous_pointer": previous["pointer"],
            "before": before, "parameters": parameters, "sources": sources, "input_assets": actual,
            "producer": _producer(), "catalog_before": catalog_before,
            "processing": {"input": "source raster stored values", "scale_applied": False,
                "offset_applied": False, "harmonization_applied": False,
                "nodata_numeric_replacement": False, "resampling": "none",
                "operation": "Rasterio read(1, window) then same-dtype native Zarr assignment",
                "upstream_correction": "unknown; provider metadata is a declaration"}}
        self.folder.mkdir(parents=True, exist_ok=True)
        atomic_json(self.folder / "intent.json", intent)
        atomic_json(self.pending, {"run_id": self.run_id, "phase": "writing",
            "intent_sha256": _file_sha256(self.folder / "intent.json")})

    def used(self, band):
        self.used_bands.add(band)

    def commit(self, *, item, zarr_path, index, band_groups):
        from .workflow import _catalog_records
        intent = load_json(self.folder / "intent.json")
        if self.used_bands != set(intent["input_assets"]):
            raise ValueError("not every declared band reached the writer")
        observation = observe_stores(self.root, intent["store_paths"])
        targets = [r for r in observation["outputs"] if r["store_path"] == intent["store_path"] and r["source_item_id"] == item.id]
        if {r["zarr_group"] for r in targets} != set(band_groups) or any(r["time_index"] != index for r in targets):
            raise ValueError("committed scene group mapping differs")
        clean_assets = {b: {**item.assets[b], "href": intent["item"]["assets"][b]["href"]} for b in intent["input_assets"]}
        clean_item = replace(item, assets=clean_assets, links=[])
        records = _catalog_records(item=clean_item, band_groups=band_groups, zarr_path=Path(zarr_path),
            preview_path=None, output_root=self.root, stac_api=self.context["recipe"]["source"]["stac_api"],
            provider=self.context["recipe"]["source"].get("provider", ""))
        for row in records:
            assets = json.loads(row["source_assets_json"])
            for band in assets:
                assets[band]["observed_sha256"] = intent["input_assets"][band]["sha256"]
                # Invalidate old quality Catalog bindings even for same-value replacement.
                assets[band]["provenance_generation_id"] = intent["generation_id"]
            row["source_assets_json"] = json.dumps(assets, sort_keys=True)
        receipt = {"run_id": self.run_id, "intent_sha256": _file_sha256(self.folder / "intent.json"),
            "generation_id": intent["generation_id"], "observation": observation,
            "used_bands": sorted(self.used_bands), "catalog_records": _plain(records),
            "verification": "native band writes and sidecar committed; structural witness only",
            "pixel_checksum": None}
        atomic_json(self.folder / "receipt.json", receipt)
        atomic_json(self.pending, {"run_id": self.run_id, "phase": "committed_provenance_pending",
            "intent_sha256": receipt["intent_sha256"], "receipt_sha256": _file_sha256(self.folder / "receipt.json")})

    def _catalogs(self, intent, receipt):
        path = self.folder / "catalog_plan.json"
        if not path.exists():
            for relative, before in intent["catalog_before"].items():
                if _digest_or_none(safe_relative(self.root, relative)) != before:
                    raise ValueError("Catalog changed before staging")
            entries = []
            for number, (relative, before) in enumerate(intent["catalog_before"].items()):
                staged = self.folder / ("catalog-" + str(number) + ".parquet")
                rows = read_catalog(safe_relative(self.root, relative)) + receipt["catalog_records"]
                _write_catalog(staged, _dedupe_sort(rows))
                entries.append({"path": relative, "before": before, "after": _file_sha256(staged), "staged": staged.name})
            atomic_json(path, {"receipt_sha256": _file_sha256(self.folder / "receipt.json"), "entries": entries})
        plan = load_json(path)
        if plan["receipt_sha256"] != _file_sha256(self.folder / "receipt.json"):
            raise ValueError("Catalog plan receipt mismatch")
        entries = plan.get("entries")
        expected = list(intent["catalog_before"].items())
        if not isinstance(entries, list) or len(entries) != len(expected):
            raise ValueError("Catalog plan must cover both intended targets")
        for number, (entry, (relative, before)) in enumerate(zip(entries, expected)):
            if (entry.get("path") != relative or entry.get("before") != before or
                    entry.get("staged") != "catalog-" + str(number) + ".parquet" or
                    not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("after", "")))):
                raise ValueError("Catalog plan targets differ from the immutable intent")
        for entry in plan["entries"]:
            staged = safe_relative(self.folder, entry["staged"])
            if _file_sha256(staged) != entry["after"] or _digest_or_none(safe_relative(self.root, entry["path"])) not in {entry["before"], entry["after"]}:
                raise ValueError("Catalog staging changed or third state detected")
        for entry in plan["entries"]:
            target = safe_relative(self.root, entry["path"])
            if _digest_or_none(target) != entry["after"]:
                _install_catalog(target, safe_relative(self.folder, entry["staged"]))
        return plan

    def recover(self):
        intent = load_json(self.folder / "intent.json")
        if intent["dataset_id"] != self.dataset_id or intent["context"] != self.context:
            raise ValueError("scene run identity or configuration changed")
        if not (self.folder / "receipt.json").exists():
            raise ValueError("writing without durable receipt is ambiguous; no automatic rewrite")
        receipt = load_json(self.folder / "receipt.json")
        receipt_hash = _file_sha256(self.folder / "receipt.json")
        if receipt["intent_sha256"] != _file_sha256(self.folder / "intent.json") or receipt["generation_id"] != intent["generation_id"]:
            raise ValueError("receipt does not match the immutable intent")
        if (self.folder / "completed.json").exists():
            done = load_json(self.folder / "completed.json")
            if done["receipt_sha256"] != receipt_hash:
                raise ValueError("completed receipt changed")
            archive = _read_release(safe_relative(self.root, "provenance/releases/" + done["pointer"]["metadata_release_id"]), done["pointer"])
            if (archive["dataset"].get("production_run_id") != self.run_id or
                    archive["evidence"].get("runs/" + self.run_id + "/receipt.json") != receipt):
                raise ValueError("completed pointer does not bind this production receipt")
            if self.pending.exists():
                if read_current(self.root) != done["pointer"]:
                    raise ValueError("current changed before pending cleanup")
                self._validate_observation(intent, receipt)
                plan = load_json(self.folder / "catalog_plan.json")
                if (archive["evidence"].get("runs/" + self.run_id + "/catalog_plan.json") != plan or
                        any(_digest_or_none(safe_relative(self.root, e["path"])) != e["after"] for e in plan["entries"])):
                    raise ValueError("published Catalog changed before pending cleanup")
                self.pending.unlink()
            return done["pointer"]
        if not self.pending.exists():
            raise ValueError("unpublished receipt is missing pending protection")
        pending = load_json(self.pending)
        if pending["intent_sha256"] != receipt["intent_sha256"] or pending.get("receipt_sha256", receipt_hash) != receipt_hash:
            raise ValueError("pending receipt binding changed")
        self._validate_observation(intent, receipt)
        current = read_snapshot(self.root)
        if current["pointer"] != intent["previous_pointer"]:
            if not current["dataset"] or current["dataset"].get("production_run_id") != self.run_id:
                raise ValueError("metadata base changed before Catalog recovery")
            if current["evidence"].get("runs/" + self.run_id + "/receipt.json") != receipt:
                raise ValueError("published receipt disagrees")
            self._catalogs(intent, receipt)
            pointer = current["pointer"]
        else:
            inventory = self._inventory(intent, receipt, current, None)
            plan = self._catalogs(intent, receipt)
            inventory["evidence"]["runs/" + self.run_id + "/catalog_plan.json"] = plan
            pointer = publish_snapshot(self.root, dataset_id=self.dataset_id, inventory=inventory,
                extra_dataset={"production_run_id": self.run_id, "production_scope": "s2_native_scenes",
                               "quality_pairing": "requires an explicit binding to this data revision"},
                expected_release_id=(current["pointer"] or {}).get("metadata_release_id"))
            pointer = {k: v for k, v in pointer.items() if k != "unchanged"}
        atomic_json(self.folder / "completed.json", {"run_id": self.run_id, "pointer": pointer, "receipt_sha256": receipt_hash})
        self.pending.unlink()
        return pointer

    def _validate_observation(self, intent, receipt):
        if observe_stores(self.root, intent["store_paths"]) != receipt["observation"]:
            raise ValueError("committed native structure changed; recovery blocked")

    def _inventory(self, intent, receipt, previous, plan):
        observed = _carry(json.loads(canonical_json(receipt["observation"])), previous)
        targets = {r["output_id"] for r in observed["outputs"] if r["store_path"] == intent["store_path"] and r["source_item_id"] == intent["item"]["item_id"]}
        before_other = {r["output_id"]: r for r in intent["before"]["outputs"] if r["output_id"] not in targets}
        after_other = {r["output_id"]: r for r in observed["outputs"] if r["output_id"] not in targets}
        if before_other != after_other:
            raise ValueError("unrelated scene mappings changed")
        ref = "evidence/runs/" + self.run_id
        links = [r for r in previous["source_links"] if r["output_id"] not in targets]
        sources = {r["source_id"]: r for r in previous["sources"]}
        for source in intent["sources"]:
            sources[source["source_id"]] = {**source, "evidence_ref": ref + "/intent.json"}
        for row in observed["outputs"]:
            if row["output_id"] not in targets:
                continue
            row.update(generation_id=intent["generation_id"], run_id=self.run_id,
                processing_id="processing-" + _digest(canonical_json(intent["parameters"])),
                evidence_status="execution_recorded", evidence_ref=ref + "/receipt.json",
                unknown_reason="Upstream calibration is unverified; this program stores source values unchanged.")
            for source in intent["sources"]:
                if variable_name(source["asset_key"]) in row["variable_scope"]:
                    links.append({"output_id": row["output_id"], "generation_id": row["generation_id"],
                        "source_id": source["source_id"], "role": "used", "evidence_ref": ref + "/receipt.json"})
        used = {r["source_id"] for r in links}
        return {**observed, "sources": [r for key, r in sources.items() if key in used], "source_links": links,
            "variables": [{"scope": "native scene bands", "stored_values": "source raster values",
                "scale_offset_applied": False, "harmonization_applied": False, "nodata_numeric_replacement": False}],
            "limitations": [{"scope": "tracked S2 native", "upstream_processing": "unknown",
                "historical_inputs": "not inferred from current code", "verification": "no full output pixel checksum"}],
            "evidence": {**previous["evidence"], "runs/" + self.run_id + "/intent.json": intent,
                "runs/" + self.run_id + "/receipt.json": receipt, "runs/" + self.run_id + "/catalog_plan.json": plan}}


def _validate_latest(root, dataset_id):
    snapshot = read_snapshot(root)
    if snapshot["dataset"] and snapshot["dataset"]["dataset_id"] != dataset_id:
        raise ValueError("dataset_id differs from the published snapshot")
    if not snapshot["pointer"]:
        return
    paths = {r["store_path"] for r in snapshot["outputs"]}
    observed = _carry(observe_stores(root, paths), snapshot)
    if ({r["output_id"]: r for r in observed["outputs"]} != {r["output_id"]: r for r in snapshot["outputs"]}
            or observed["witnesses"] != snapshot["witnesses"]):
        raise ValueError("current spectral data changed outside recorded production")
    op = snapshot["dataset"].get("production_run_id")
    if op:
        plan = load_json(safe_relative(root, "provenance/runs/" + op + "/catalog_plan.json"))
        if snapshot["evidence"].get("runs/" + op + "/catalog_plan.json") != plan:
            raise ValueError("published Catalog plan evidence changed")
        if any(_digest_or_none(safe_relative(root, e["path"])) != e["after"] for e in plan["entries"]):
            raise ValueError("current Catalog changed outside coordinated publication")


def run_tracked_scenes(cfg):
    """Caller holds the shared root lock; use native cache/writer and Catalog code."""
    from .asset_cache import cache_scene_assets
    from .config import date_intervals, output_path
    from .grid import enumerate_mgrs_tiles, roi_bbox
    from .workflow import search_items
    from .zarr_writer import append_item_to_native_zarr
    os.environ["S2GRITS_DOWNLOADER"] = str(cfg["runtime"].get("downloader", "auto")).lower()
    root = output_path(cfg, "base_dir").resolve()
    root.mkdir(parents=True, exist_ok=True)
    options = cfg["metadata"]["provenance"]
    batch_id, dataset_id = options["run_id"], options["dataset_id"]
    public_keys = {
        "roi": ("manual_mgrs_tiles", "wkt", "bbox"),
        "time": ("years", "months", "start_date", "end_date", "allow_recent", "query_interval"),
        "assets": ("bands", "resolution_mode", "resampling"),
        "processing": ("monthly", "cloud_mask"),
        "runtime": ("chunk_size", "read_retries", "download_scene_workers", "download_band_workers",
                    "downloader", "max_tiles", "max_scenes_per_tile"),
        "source": ("stac_api", "collection", "provider", "cloud_cover_max", "page_limit",
                   "request_timeout_seconds", "max_retries", "retry_backoff_seconds", "read_mode",
                   "local_cache_on_read_failure"),
    }
    recipe = {section: {key: cfg[section].get(key) for key in keys}
              for section, keys in public_keys.items()}
    recipe["roi_sha256"] = _digest(canonical_json(cfg["roi"]))
    recipe["source"]["stac_api"] = sanitize_asset_url(cfg["source"]["stac_api"])
    recipe["output"] = {k: cfg["output"].get(k) for k in ("existing_store", "existing_item", "zarr_compression")}
    context = {"workflow_batch_id": batch_id, "recipe": recipe, "recipe_sha256": _digest(canonical_json(recipe))}
    batch_path = safe_relative(root, "provenance/batches/" + batch_id + ".json")
    state = load_json(batch_path) if batch_path.exists() else {"dataset_id": dataset_id,
        "context": context, "producer": _producer(), "plans": {}, "completed": {}, "skipped": {}}
    if state["dataset_id"] != dataset_id or state["context"] != context:
        raise ValueError("run_id cannot be reused for changed inputs/configuration")
    pending = list(safe_relative(root, "provenance/pending").glob("*.json"))
    if len(pending) > 1:
        raise ValueError("multiple pending operations need explicit recovery")
    for path in pending:
        SceneRun(root, dataset_id, path.stem, context).recover()
    tiles = enumerate_mgrs_tiles(cfg)
    if cfg["runtime"].get("max_tiles"):
        tiles = tiles[:int(cfg["runtime"]["max_tiles"])]
    bands = cfg["assets"]["bands"]
    for relative in ["catalog.parquet", *[t + "/catalog.parquet" for t in tiles]]:
        path = safe_relative(root, relative)
        if path.exists():
            table = pq.read_table(path)
            if table.schema != CATALOG_SCHEMA:
                raise ValueError("tracked production requires the existing canonical Catalog schema")
            if any(r.get("status") != "complete" or not r.get("item_id") or not r.get("zarr_group")
                   for r in table.to_pylist()):
                raise ValueError("Catalog contains incomplete or ambiguous source records")
    def completed(op):
        folder = safe_relative(root, "provenance/runs/" + op)
        if (folder / "completed.json").exists():
            SceneRun(root, dataset_id, op, context).recover()
            state["completed"][op] = True
            return True
        return op in state["skipped"]
    if set(state["plans"]) == set(tiles) and all(completed(op) for plan in state["plans"].values() for op in plan):
        _validate_latest(root, dataset_id)
        atomic_json(batch_path, state)
        return {"status": "complete", "results": {t: {"status": "success"} for t in tiles},
                "audit_report": {"ok": True, "scope": "tracked structural and Catalog consistency"}, "resumed": True}
    if state.get("producer") != _producer():
        raise ValueError("unfinished batch producer code/dependencies changed; use an explicit new run")
    _validate_latest(root, dataset_id)
    atomic_json(batch_path, state)
    for tile in tiles:
        if tile in state["plans"] and all(completed(op) for op in state["plans"][tile]):
            continue
        items = {}
        for interval in date_intervals(cfg):
            for item in search_items(stac_api=cfg["source"]["stac_api"], collection=cfg["source"]["collection"],
                    datetime_interval=interval, tiles=[tile], bands=bands, bbox=roi_bbox(cfg),
                    cloud_cover_max=float(cfg["source"]["cloud_cover_max"]), page_limit=int(cfg["source"]["page_limit"]),
                    timeout=float(cfg["source"]["request_timeout_seconds"]), max_retries=int(cfg["source"]["max_retries"]),
                    backoff_seconds=float(cfg["source"]["retry_backoff_seconds"]), grid_code=tile):
                if item.tile_id != tile or item.collection != cfg["source"]["collection"]:
                    raise ValueError("query source identity differs from request")
                items[item.id] = item
        ordered = sorted(items.values(), key=lambda item: (item.datetime, item.id))
        if cfg["runtime"].get("max_scenes_per_tile"):
            ordered = ordered[:int(cfg["runtime"]["max_scenes_per_tile"])]
        plan = {"op-" + _digest(canonical_json([dataset_id, batch_id, tile, item.collection, item.id]))[:32]: item_declaration(item, bands) for item in ordered}
        if tile in state["plans"] and state["plans"][tile] != plan:
            raise ValueError("queried source membership changed for the same run_id")
        state["plans"][tile] = plan
        atomic_json(batch_path, state)
        for op, item in zip(plan, ordered):
            if completed(op):
                continue
            store = root / tile / "scenes_L2A_native/zarr" / ("s2grits_scenes_" + tile + "_L2A_native.zarr")
            entries = _load_metadata(store)
            _validate_resume_contract(store, entries, selected_native_groups(bands), item)
            if cfg["output"].get("existing_item", "skip") == "skip" and any(e["item_id"] == item.id for e in entries["items"]):
                state["skipped"][op] = "existing scene retained; no current source claim or Catalog rewrite"
                atomic_json(batch_path, state)
                continue
            cache = cache_scene_assets(item=item, bands=bands, cache_dir=output_path(cfg, "cache_dir"),
                lock_dir=output_path(cfg, "logs_dir") / "locks", max_band_workers=int(cfg["runtime"]["download_band_workers"]))
            if cache.status != "ok":
                raise RuntimeError("scene cache failed: " + "; ".join(cache.errors))
            tracker = SceneRun(root, dataset_id, op, context)
            append_item_to_native_zarr(item=item, bands=bands, zarr_path=store,
                chunk_size=int(cfg["runtime"]["chunk_size"]), compression=cfg["output"]["zarr_compression"],
                read_retries=int(cfg["runtime"]["read_retries"]), cache_dir=output_path(cfg, "cache_dir"),
                read_mode="local_cache_first", lock_dir=output_path(cfg, "logs_dir") / "locks",
                _provenance_tracker=tracker, on_item_conflict=cfg["output"].get("existing_item", "skip"))
            tracker.recover()
            state["completed"][op] = True
            atomic_json(batch_path, state)
    _validate_latest(root, dataset_id)
    return {"status": "complete", "results": {t: {"status": "success"} for t in tiles},
            "audit_report": {"ok": True, "scope": "tracked structural and Catalog consistency"}, "resumed": False}
