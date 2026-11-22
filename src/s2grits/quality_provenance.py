"""Optional SCL execution snapshots; the existing quality producer owns its lock."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def validate_options(cfg):
    options = (cfg.get("metadata") or {}).get("provenance") or {}
    if not isinstance(options, dict) or type(options.get("enabled", False)) is not bool:
        raise ValueError("metadata.provenance.enabled must be boolean")
    if not options.get("enabled", False):
        return None
    for key in ("dataset_id", "run_id"):
        if not isinstance(options.get(key), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", options[key]):
            raise ValueError("quality provenance requires safe dataset_id/run_id")
    return options


def _inventory_digest(inventory):
    from .provenance_contract import TABLE_SCHEMAS, canonical_json
    value = {name: sorted([{key: row.get(key) for key in schema.names} for row in inventory[name]], key=canonical_json)
             for name, schema in TABLE_SCHEMAS.items()}
    value.update({name: inventory[name] for name in ("variables", "limitations", "witnesses", "evidence")})
    return hashlib.sha256(canonical_json(value)).hexdigest()


def begin_snapshot(root, cfg, binding):
    options = validate_options(cfg)
    if options is None:
        return None
    from . import quality_backfill as q
    from .provenance import read_snapshot
    from .provenance_contract import canonical_json
    root = Path(root)
    pending = root / "provenance/pending" / (options["run_id"] + ".json")
    others = [p for p in pending.parent.glob("*.json") if p != pending]
    q.require(not others, "Another provenance operation is pending")
    fingerprint = hashlib.sha256(canonical_json(binding)).hexdigest()
    snapshot = read_snapshot(root)
    current = snapshot["pointer"]
    if current:
        q.require(snapshot["dataset"]["dataset_id"] == options["dataset_id"], "Quality dataset identity changed")
    if pending.exists():
        value = q.load_json(pending)
        q.require(value.get("binding_sha256") == fingerprint and value.get("dataset_id") == options["dataset_id"],
                  "Quality snapshot pending identity differs")
        current_id = current["metadata_release_id"] if current else None
        if current_id != value["expected_release_id"]:
            dataset = snapshot["dataset"] or {}
            q.require(dataset.get("execution_kind") == "production"
                      and dataset.get("production_scope") == "native_scl_accepted_copies"
                      and dataset.get("run_id") == value["run_id"]
                      and value.get("publication_payload_sha256") == _inventory_digest(snapshot),
                      "Quality publication changed after preparation")
            # Finish the prior publication before permitting more scenes in this frozen batch.
            value["expected_release_id"] = current_id
            value.pop("publication_payload_sha256", None)
            q.atomic_json(pending, value)
        return value
    value = {"run_id": options["run_id"], "dataset_id": options["dataset_id"],
             "binding_sha256": fingerprint,
             "expected_release_id": current["metadata_release_id"] if current else None}
    q.atomic_json(pending, value)
    return value


def _public_receipt(receipt):
    from copy import deepcopy
    from .provenance_contract import sanitize_asset_url
    result = deepcopy(receipt)
    for binding in (result.get("binding", {}), result.get("write_intent", {}).get("binding", {})):
        for key in ("spectral_root", "quality_root", "cache_root"):
            binding.pop(key, None)
    source = result.get("write_intent", {}).get("source_version", {})
    if source.get("href"):
        source["href"] = sanitize_asset_url(source["href"])
    return result


def quality_inventory(root, marker):
    """Use accepted writes only; old generations remain explicitly unknown."""
    from . import quality_backfill as q
    from .provenance_contract import canonical_json, logical_output_id, sanitize_asset_url
    root = Path(root)
    inventory = {"outputs": [], "sources": [], "source_links": [], "variables": [
        {"name": "scl", "dtype": "uint8", "raw_nodata": 0, "units": "scene_classification",
         "meaning": "Original classes 0..11; missing quality does not mean clear sky",
         "scale_applied": False, "offset_applied": False}],
        "limitations": [{"scope": "SCL native copy", "upstream": "source-declared, not independently calibrated",
                         "used": "Native categorical source copied without numerical scaling or resampling"}],
        "witnesses": {}, "evidence": {}}
    links = q.pq.read_table(root / "quality_links.parquet").to_pylist()
    for link in links:
        receipt_path = f"_task_state/accepted/{link['tile_id']}_{link['item_id']}.json"
        q.require(receipt_path in marker["artifacts"], "Quality receipt is not published")
        receipt = q.load_json(root / receipt_path)
        q.require(q.digest(root / receipt_path) == marker["artifacts"][receipt_path], "Quality receipt changed")
        intent = receipt.get("write_intent") or {}
        generation = receipt.get("generation_id")
        ref = f"evidence/quality/{link['tile_id']}_{link['item_id']}.json"
        inventory["evidence"][ref.removeprefix("evidence/")] = _public_receipt(receipt)
        item = {"collection": link["source_collection"], "tile_id": link["tile_id"],
                "item_id": link["item_id"], "group": "20m", "grid_id": link["quality_20m_grid_id"], "variables": ["scl"]}
        output = {"output_id": logical_output_id(item), "generation_id": generation,
                  "store_path": link["quality_20m_store"], "zarr_group": "20m", "variable_scope": ["scl"],
                  "tile_id": link["tile_id"], "geometry_group_id": None, "grid_id": link["quality_20m_grid_id"],
                  "product_type": "quality_scl", "record_kind": "scene", "time_index": link["quality_20m_time_index"],
                  "time_value_utc": link["datetime"], "period_start": None, "period_end": None,
                  "source_item_id": link["item_id"],
                  "run_id": "quality-" + intent["binding"]["config_sha256"] if intent else None,
                  "processing_id": "processing-" + hashlib.sha256(canonical_json(receipt["executed_processing"])).hexdigest() if intent else None,
                  "evidence_status": "execution_recorded" if generation else "historical_reported",
                  "evidence_ref": ref, "unknown_reason": "Upstream classification is not independently verified."
                    if generation else "Historical write generation and runtime are unknown; accepted copy evidence is preserved."}
        inventory["outputs"].append(output)
        version = intent.get("source_version") or {}
        asset = {"collection": link["source_collection"], "product_id": link["item_id"], "asset_key": "scl",
                 "href": sanitize_asset_url(version["href"]) if version.get("href") else None,
                 "source_sha256": receipt["source_sha256"]}
        source_id = "source-" + hashlib.sha256(canonical_json(asset)).hexdigest()
        inventory["sources"].append({"source_id": source_id, **asset,
                                     "declared_scale": version.get("raster_metadata", {}).get("scale"),
                                     "declared_offset": version.get("raster_metadata", {}).get("offset"),
                                     "evidence_status": "source_declared", "evidence_ref": ref})
        inventory["source_links"].append({"output_id": output["output_id"], "generation_id": generation,
                                          "source_id": source_id, "role": "used", "evidence_ref": ref})
        store = link["quality_20m_store"]
        sidecar = store + "/s2grits_resume_state.json"
        inventory["witnesses"][sidecar] = q.digest(root / sidecar)
        decoded = receipt["verification"].get("decoded_sha256")
        if decoded:
            inventory["witnesses"][f"decoded/{store}/20m/scl/{link['item_id']}"] = decoded
    return inventory



class QualityCopyTracker:
    """Authorize one native SCL copy against its durable intent and pending owner."""
    def __init__(self, root, pending, intent):
        self.root, self.pending, self.intent = Path(root), pending, intent
        self.used_bands = set()
        self.committed = False

    def prepare(self, *, item, bands, zarr_path, asset_paths, band_groups, index, operation, parameters):
        from . import quality_backfill as q
        from .provenance_contract import canonical_json
        pending_path = self.root / "provenance/pending" / (self.pending["run_id"] + ".json")
        q.require(list(pending_path.parent.glob("*.json")) == [pending_path]
                  and q.load_json(pending_path) == self.pending, "Quality native write has no matching pending owner")
        version = self.intent["source_version"]
        q.require(self.pending["binding_sha256"] == hashlib.sha256(canonical_json(self.intent["binding"])).hexdigest(),
                  "Quality intent and pending binding disagree")
        q.require(bands == ["scl"] and band_groups == {"20m": ["scl"]} and operation == "append"
                  and index == self.intent["index"]
                  and (item.collection, item.tile_id, item.id, item.datetime) ==
                      (version["collection"], version["tile_id"], version["item_id"], version["datetime"])
                  and Path(zarr_path).resolve() == q.store_path(self.root, item.tile_id).resolve(),
                  "Quality native write differs from its frozen scene intent")
        q.require(q.digest(asset_paths["scl"]) == version["source_sha256"], "Quality native source content changed")

    def used(self, band):
        self.used_bands.add(band)

    def commit(self, **kwargs):
        from . import quality_backfill as q
        q.require(self.used_bands == {"scl"}, "Quality native write did not consume the declared SCL asset")
        self.committed = True


def finish_snapshot(root, cfg, marker, pending):
    if pending is None:
        return None
    from .provenance import publish_snapshot
    root = Path(root)
    inventory = quality_inventory(root, marker)
    if not inventory["outputs"]:
        (root / "provenance/pending" / (pending["run_id"] + ".json")).unlink()
        return None
    from .provenance import atomic_json
    pending["publication_payload_sha256"] = _inventory_digest(inventory)
    atomic_json(root / "provenance/pending" / (pending["run_id"] + ".json"), pending)
    result = publish_snapshot(root, dataset_id=pending["dataset_id"], inventory=inventory,
                              execution_kind="production",
                              extra_dataset={"production_scope": "native_scl_accepted_copies", "run_id": pending["run_id"]},
                              expected_release_id=pending["expected_release_id"])
    (root / "provenance/pending" / (pending["run_id"] + ".json")).unlink()
    return result
