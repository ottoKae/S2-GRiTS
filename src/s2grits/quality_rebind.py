"""Publish a new SCL pairing without rewriting quality data or legacy bindings.

The v1 companion remains untouched. Resolver support for these independent
pairing releases belongs to the subsequent version-aware reader integration.
"""
from __future__ import annotations

import hashlib
import os
import re

import pyarrow as pa
import pyarrow.parquet as pq

from . import quality_backfill as q
from .locking import InterProcessLock
from .provenance import atomic_json, read_current, read_snapshot, safe_relative
from .provenance_contract import canonical_json, data_revision
from .quality_provenance import _public_receipt, quality_inventory


def _hash(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _stable_quality(root):
    marker = q.load_json(root / "companion.json")
    q.require(marker.get("state") == "published" and marker.get("contract_version") == 1,
              "Quality must have a stable legacy publication")
    q.require(not list((root / "provenance/pending").glob("*.json")), "Quality provenance is pending")
    for relative, expected in marker["artifacts"].items():
        q.require(q.digest(safe_relative(root, relative)) == expected, "Published quality artifact changed")
    inventory = quality_inventory(root, marker)
    q.require(len(inventory["outputs"]) == marker["accepted_scenes"], "Quality accepted count differs")
    return marker, inventory


def _spectral_revision(root, scenes):
    snapshot = read_snapshot(root)
    pointer = snapshot["pointer"]
    if pointer:
        from .provenance_production import _validate_latest
        _validate_latest(root, snapshot["dataset"]["dataset_id"])
        rows = {(row["store_path"], row["zarr_group"], row["source_item_id"]): row
                for row in snapshot["outputs"]}
        for scene in scenes:
            for location in scene["spectral"].values():
                row = rows.get((location["store"], location["group"], scene["item"].id))
                q.require(row is not None and row["time_index"] == location["time_index"]
                          and row["grid_id"] == location["grid_id"]
                          and q.time_ns(row["time_value_utc"]) == q.time_ns(scene["item"].datetime),
                          "Spectral provenance does not describe the current group indices")
                intent = snapshot["evidence"].get("runs/" + str(row.get("run_id")) + "/intent.json", {})
                recorded = intent.get("item", {}).get("properties", {})
                for key in ("s2:product_uri", "s2:processing_baseline", "created", "updated"):
                    if recorded.get(key) is not None:
                        q.require(recorded[key] == scene["item"].properties.get(key),
                                  "Frozen source product version differs from produced spectral evidence")
        return pointer["data_revision"], "published_" + snapshot["dataset"]["execution_kind"], pointer
    witnesses = {"catalog.parquet": q.digest(root / "catalog.parquet")}
    locations = []
    for scene in scenes:
        for location in scene["spectral"].values():
            sidecar = location["store"] + "/s2grits_resume_state.json"
            witnesses[sidecar] = q.digest(safe_relative(root, sidecar))
            locations.append({"item_id": scene["item"].id, **location})
    return "observed-" + _hash({"witnesses": witnesses, "locations": locations}), "historical_structure_only", None


def _verify_reuse(scene, old_link, receipt, location, root, cfg):
    item, expected = scene["item"], scene["expected"]
    if receipt["source_sha256"] != expected["source_sha256"]:
        return False, "source_scl_content_changed", {}
    if q.time_ns(old_link["datetime"]) != q.time_ns(item.datetime):
        return False, "source_datetime_changed", {}
    recorded = receipt.get("write_intent", {}).get("source_version")
    if recorded is not None and recorded != q._source_version(scene):
        return False, "source_product_or_asset_version_changed", {}
    if location["grid_id"] != scene["spectral"]["20m"]["grid_id"]:
        return False, "quality_grid_changed", {}
    store = safe_relative(root, location["store"])
    group = q._open(store)["20m"]
    if (list(group["scl"].shape[1:]) != expected["shape"] or group.attrs["crs"] != expected["crs"]
            or not q.np.allclose(group.attrs["transform"][:6], expected["transform"][:6], rtol=0, atol=1e-9)):
        return False, "quality_grid_changed", {}
    decoded = q.decoded_scene_digest(store, location["time_index"])
    original = receipt["verification"].get("decoded_sha256")
    if original is not None:
        if receipt["verification"].get("decoded_hash_scheme") != "uint8-block512-v1" or decoded != original:
            return False, "quality_pixels_changed", {"decoded_sha256": decoded}
        return True, None, {"decoded_sha256": decoded, "integrity": "accepted_decoded_digest"}
    source = q._local_source(scene, cfg)
    if source is None:
        return False, "historical_receipt_has_no_pixel_digest_or_original_COG", {"decoded_sha256": decoded}
    verification = q.verify_scene(source, store, location["time_index"], scene)
    verification.pop("seconds", None)
    return True, None, {"decoded_sha256": decoded, "integrity": "original_COG_full_pixel_comparison", "verification": verification}


def _release(root, run_id, payload):
    directory = safe_relative(root, "provenance/pairings/" + run_id)
    current_path = safe_relative(root, "provenance/pairings/current.json")
    request_hash = _hash(payload)
    current = q.load_json(current_path) if current_path.exists() else None
    if current is not None:
        q.require(current.get("kind") == "independent_quality_pairing" and current.get("contract_version") == 1,
                  "Unknown pairing current contract")
        for name, expected in current["artifacts"].items():
            path = safe_relative(root, "provenance/pairings/" + current["run_id"] + "/" + name)
            q.require(q.digest(path) == expected, "Published pairing artifact changed")
    intent_path = directory / "intent.json"
    if intent_path.exists():
        intent = q.load_json(intent_path)
        q.require(intent["request_sha256"] == request_hash, "Pairing run_id cannot be reused with changed inputs")
        q.require(current == intent["expected_current"] or (current and current.get("run_id") == run_id),
                  "Pairing current changed after preparation")
    else:
        atomic_json(intent_path, {"request_sha256": request_hash, "expected_current": current})
    document = {key: value for key, value in payload.items() if key not in {"links", "evidence"}}
    buffer = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(payload["links"]), buffer)
    files = {"pairing.json": canonical_json(document) + b"\n",
             "quality_links.parquet": buffer.getvalue().to_pybytes(),
             "evidence.json": canonical_json(payload["evidence"]) + b"\n"}
    hashes = {}
    for name, data in files.items():
        path = directory / name
        hashes[name] = hashlib.sha256(data).hexdigest()
        if path.exists():
            q.require(q.digest(path) == hashes[name], "Immutable pairing release content differs")
        else:
            temporary = path.with_name(path.name + ".tmp")
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
    pointer = {"contract_version": 1, "kind": "independent_quality_pairing", "run_id": run_id,
               "request_sha256": request_hash, "artifacts": hashes,
               "spectral_data_revision": payload["spectral_data_revision"],
               "quality_data_revision": payload["quality_data_revision"]}
    if current != pointer:
        atomic_json(current_path, pointer)
    return {**pointer, "unchanged": current == pointer}


def rebind_quality(config_path, *, run_id, dry_run=False):
    """Explicitly revalidate existing SCL; never download, rewrite or rebind old jobs."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("run_id must be a safe public identifier")
    initial_cfg, _, initial_binding = q.load_job(config_path)
    spectral = initial_cfg["quality"]["spectral_root"]
    root = initial_cfg["output"]["base_dir"]
    with InterProcessLock(spectral.parent / ("." + spectral.name + ".selected.lock"), timeout=0), \
            InterProcessLock(root.parent / ("." + root.name + ".quality.lock"), timeout=0):
        q.require(not list((spectral / "provenance/pending").glob("*.json")), "Spectral production is pending")
        cfg, scenes, binding = q.load_job(config_path)
        q.require(binding == initial_binding, "Frozen pairing configuration changed while acquiring locks")
        _marker, inventory = _stable_quality(root)
        marker_hash = q.digest(root / "companion.json")
        spectral_revision, spectral_scope, spectral_pointer = _spectral_revision(spectral, scenes)
        quality_pointer = read_snapshot(root)["pointer"]
        original_revision = data_revision(inventory["outputs"], inventory["witnesses"])
        if quality_pointer:
            q.require(quality_pointer["data_revision"] == original_revision, "Quality provenance is stale")
        original_links = q.pq.read_table(root / "quality_links.parquet").to_pylist()
        by_item = {q.identity(row): row for row in original_links}
        q.require(len(by_item) == len(original_links), "Duplicate quality scene identity")
        catalogs = {q.identity(row): row for row in q.read_catalog(root / "catalog.parquet")}
        q.require(set(catalogs) == set(by_item), "Quality Catalog and links differ")
        locations, links, evidence = {}, [], {}
        for scene in scenes:
            item = scene["item"]
            key = (item.collection, item.tile_id, item.id)
            output = {"source_collection": item.collection, "tile_id": item.tile_id,
                      "item_id": item.id, "datetime": item.datetime,
                      "source_sha256": scene["expected"]["source_sha256"]}
            for group in ("10m", "20m"):
                output.update({f"spectral_{group}_{k}": v for k, v in scene["spectral"][group].items()})
            available, reason, integrity = False, "no_accepted_quality_scene", {}
            if key in by_item:
                old_link = by_item[key]
                receipt_name = f"_task_state/accepted/{item.tile_id}_{item.id}.json"
                receipt = q.load_json(safe_relative(root, receipt_name))
                verification = receipt["verification"]
                q.require(receipt["status"] == "ACCEPTED" and receipt["link"] == old_link
                          and verification["full_pixel_comparison"] and verification["mismatched_pixels"] == 0,
                          "Accepted quality evidence differs")
                location = q._group_location(catalogs[key], root, locations)
                available, reason, integrity = _verify_reuse(scene, old_link, receipt, location, root, cfg)
                output.update({f"quality_20m_{k}": v for k, v in location.items()})
                output["quality_generation_id"] = receipt.get("generation_id")
                evidence[item.tile_id + "_" + item.id] = {"legacy_receipt_sha256": q.digest(root / receipt_name),
                    "receipt": _public_receipt(receipt), "current_integrity": integrity}
                if integrity.get("decoded_sha256"):
                    inventory["witnesses"][f"decoded/{location['store']}/20m/scl/{item.id}"] = integrity["decoded_sha256"]
            output.update(quality_available=available, unavailable_reason=reason)
            links.append(output)
        quality_revision = data_revision(inventory["outputs"], inventory["witnesses"])
        q.require(read_current(spectral) == spectral_pointer and read_current(root) == quality_pointer
                  and q.digest(root / "companion.json") == marker_hash
                  and q.digest(spectral / "catalog.parquet") == binding["spectral_catalog_sha256"],
                  "Source versions changed during pairing validation")
        public_binding = {k: v for k, v in binding.items() if k not in {"spectral_root", "quality_root", "cache_root"}}
        payload = {"contract_version": 1, "run_id": run_id, "binding": public_binding,
                   "spectral_data_revision": spectral_revision, "spectral_revision_scope": spectral_scope,
                   "quality_data_revision": quality_revision,
                   "quality_revision_scope": "production_snapshot" if quality_pointer and quality_revision == original_revision else "historical_or_changed_observation",
                   "legacy_companion_sha256": marker_hash, "links": links, "evidence": evidence,
                   "resolver_support": "requires_P4_version_aware_pairing_reader"}
        counts = {"scenes": len(links), "quality_available": sum(row["quality_available"] for row in links),
                  "quality_unavailable": sum(not row["quality_available"] for row in links)}
        if dry_run:
            return {"status": "dry_run", **counts, "download_started": False}
        pointer = _release(root, run_id, payload)
        return {"status": "published", **counts, "download_started": False, "publication": pointer}
