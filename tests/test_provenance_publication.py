"""Self-contained metadata publication tests; no image or network operations."""
import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

from s2grits import provenance as p
from s2grits import provenance_contract as contract

CANONICAL_SHA256 = "a127983ad7a609682dbc7913c55b23f2297c79d84830319a640083f368a8eeff"


@pytest.fixture
def inventory():
    ref = "evidence/runs/run1.json"
    return {"outputs": [{"output_id": "output-scene1-10m", "generation_id": "gen-1",
                "store_path": "17MNR/scenes.zarr", "zarr_group": "10m",
                "variable_scope": ["red"], "tile_id": "17MNR", "geometry_group_id": None,
                "grid_id": "grid-10m", "product_type": "scenes", "record_kind": "scene",
                "time_index": 0, "time_value_utc": "2025-01-01T12:00:00Z",
                "period_start": None, "period_end": None, "source_item_id": "scene1",
                "run_id": "run1", "processing_id": "raw-copy", "evidence_status": "execution_recorded",
                "evidence_ref": ref, "unknown_reason": "Upstream calibration is unverified."}],
            "sources": [{"source_id": "source1", "collection": "sentinel-2-c1-l2a",
                "product_id": "scene1", "asset_key": "red", "href": "https://example.invalid/a.tif",
                "source_sha256": "ab" * 32, "declared_scale": 0.0001, "declared_offset": -0.1,
                "evidence_status": "source_declared", "evidence_ref": ref}],
            "source_links": [{"output_id": "output-scene1-10m", "generation_id": "gen-1",
                "source_id": "source1", "role": "used", "evidence_ref": ref}],
            "witnesses": {"17MNR/scenes.zarr/commit.json": "c" * 64},
            "evidence": {"runs/run1.json": {"applied_scale": False, "applied_offset": False}},
            "variables": [{"variable": "red", "units": "stored digital number"}],
            "limitations": ["No upstream calibration verification."]}


def publish(root, inventory, **kwargs):
    return p.publish_snapshot(root, dataset_id="sample-s2", inventory=inventory, **kwargs)


def files(root):
    root = p._io_path(root)
    return {str(path.relative_to(root)): p._digest(path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def test_vendor_is_exact_versioned_canonical_contract():
    body = Path(contract.__file__).read_bytes().split(b"\n", 3)[3]
    assert hashlib.sha256(body).hexdigest() == CANONICAL_SHA256
    assert contract.SCHEMA_VERSION == 1
    assert contract.TABLE_SCHEMAS["outputs"].field("time_index").type.bit_width == 64


def test_canonical_schema_and_revision_compatibility_when_checkout_available(inventory):
    canonical = Path(__file__).resolve().parents[2] / "S1-GRiTS/src/s1grits/provenance_contract.py"
    if not canonical.exists():
        # The pinned full-file hash also tests compatibility in an isolated S2 checkout.
        test_vendor_is_exact_versioned_canonical_contract()
        return
    spec = importlib.util.spec_from_file_location("grits_canonical_contract", canonical)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert canonical.read_bytes() == Path(contract.__file__).read_bytes().split(b"\n", 3)[3]
    assert all(schema.equals(module.TABLE_SCHEMAS[name], check_metadata=True)
               for name, schema in contract.TABLE_SCHEMAS.items())
    assert contract.data_revision(inventory["outputs"], inventory["witnesses"]) == module.data_revision(
        inventory["outputs"], inventory["witnesses"])


def test_roundtrip_and_repeated_publish_are_byte_stable(tmp_path, inventory):
    assert p.read_snapshot(tmp_path)["pointer"] is None
    first = publish(tmp_path, inventory)
    snap = p.read_snapshot(tmp_path)
    assert snap["outputs"] == inventory["outputs"]
    assert snap["sources"] == inventory["sources"]
    assert snap["source_links"] == inventory["source_links"]
    assert snap["evidence"] == inventory["evidence"]
    assert snap["dataset"]["delivery_kind"] == "metadata_only"
    before = files(tmp_path)
    repeat = publish(tmp_path, inventory)
    assert repeat["unchanged"] and repeat["metadata_release_id"] == first["metadata_release_id"]
    assert files(tmp_path) == before


def test_metadata_recheck_changes_release_not_data_revision(tmp_path, inventory):
    first = publish(tmp_path, inventory)
    changed = deepcopy(inventory)
    changed["limitations"].append("More precise explanation.")
    changed["evidence"]["clarification.json"] = {"reviewed": True}
    second = publish(tmp_path, changed, execution_kind="metadata_recheck",
                     expected_release_id=first["metadata_release_id"])
    assert second["metadata_release_id"] != first["metadata_release_id"]
    assert second["data_revision"] == first["data_revision"]
    assert p.read_snapshot(tmp_path)["outputs"][0]["generation_id"] == "gen-1"


def test_replace_changes_generation_and_keeps_historical_release(tmp_path, inventory):
    first = publish(tmp_path, inventory)
    prior = p.safe_relative(tmp_path, "provenance/releases/" + first["metadata_release_id"])
    before = files(prior)
    changed = deepcopy(inventory)
    changed["outputs"][0]["generation_id"] = "gen-2"
    changed["source_links"][0]["generation_id"] = "gen-2"
    second = publish(tmp_path, changed, expected_release_id=first["metadata_release_id"])
    assert second["data_revision"] != first["data_revision"]
    assert p.read_snapshot(tmp_path)["outputs"][0]["output_id"] == inventory["outputs"][0]["output_id"]
    assert files(prior) == before


def test_stale_expected_release_rejects_before_writes(tmp_path, inventory):
    publish(tmp_path, inventory)
    before = files(tmp_path)
    changed = deepcopy(inventory)
    changed["limitations"].append("new information")
    with pytest.raises(ValueError, match="stale expected"):
        publish(tmp_path, changed)
    assert files(tmp_path) == before


def test_orphan_release_recovered_without_new_release(tmp_path, inventory, monkeypatch):
    original = p.atomic_json
    def interrupt(path, value):
        if Path(path).name == "current.json":
            raise OSError("simulated pointer interruption")
        return original(path, value)
    monkeypatch.setattr(p, "atomic_json", interrupt)
    with pytest.raises(OSError, match="pointer interruption"):
        publish(tmp_path, inventory)
    assert p.read_current(tmp_path) is None
    releases = list(p.safe_relative(tmp_path, "provenance/releases").iterdir())
    assert len(releases) == 1
    before = files(releases[0])
    monkeypatch.setattr(p, "atomic_json", original)
    publish(tmp_path, inventory)
    assert len(list(p.safe_relative(tmp_path, "provenance/releases").iterdir())) == 1
    assert files(releases[0]) == before
    assert p.read_snapshot(tmp_path)["dataset"]["counts"]["outputs"] == 1


def test_missing_evidence_does_not_publish(tmp_path, inventory):
    inventory["evidence"].clear()
    with pytest.raises(ValueError, match="missing provenance evidence"):
        publish(tmp_path, inventory)
    assert p.read_current(tmp_path) is None


@pytest.mark.parametrize("field", ["dataset_id", "schema_version", "data_revision"])
def test_extra_dataset_cannot_override_core_fields(tmp_path, inventory, field):
    with pytest.raises(ValueError, match="cannot replace"):
        publish(tmp_path, inventory, extra_dataset={field: "false-identity"})
    assert p.read_current(tmp_path) is None


@pytest.mark.parametrize("path", ["../x.json", "/x.json", "C:/x", "a\\b.json", "a//b.json", "a/./b.json", "a\x00b.json"])
def test_relative_path_rejects_escapes_and_ambiguous_aliases(tmp_path, path):
    with pytest.raises(ValueError):
        p.safe_relative(tmp_path, path)


@pytest.mark.parametrize("case", ["bad_reference", "wrong_generation", "signed_url", "bad_scale"])
def test_bad_records_never_publish(tmp_path, inventory, case):
    if case == "bad_reference":
        inventory["source_links"][0]["source_id"] = "missing-source"
    elif case == "wrong_generation":
        inventory["source_links"][0]["generation_id"] = "gen-other"
    elif case == "signed_url":
        inventory["sources"][0]["href"] += "?token=secret"
    else:
        inventory["sources"][0]["declared_scale"] = float("nan")
    with pytest.raises(ValueError):
        publish(tmp_path, inventory)
    assert not list(tmp_path.iterdir())


def test_release_tampering_rejected(tmp_path, inventory):
    pointer = publish(tmp_path, inventory)
    evidence = p.safe_relative(tmp_path, "provenance/releases/" + pointer["metadata_release_id"] + "/evidence/runs/run1.json")
    evidence.write_text('{}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="contents differ"):
        p.read_snapshot(tmp_path)


def test_long_path_evidence_roundtrip_and_orphan_recovery(tmp_path, inventory, monkeypatch):
    root = tmp_path / ("long-" + "x" * 65)
    inventory = deepcopy(inventory)
    name = "runs/" + "r" * 80 + "/receipt.json"
    inventory["evidence"] = {name: {"verified": True}}
    for table in ("outputs", "sources", "source_links"):
        inventory[table][0]["evidence_ref"] = "evidence/" + name
    original = p.atomic_json
    def fail_pointer(path, value):
        if Path(path).name == "current.json":
            raise OSError("stop after release")
        return original(path, value)
    monkeypatch.setattr(p, "atomic_json", fail_pointer)
    with pytest.raises(OSError, match="stop after release"):
        publish(root, inventory)
    monkeypatch.setattr(p, "atomic_json", original)
    pointer = publish(root, inventory)
    physical = p.safe_relative(root, "provenance/releases/" + pointer["metadata_release_id"] + "/evidence/" + name)
    assert len(str(physical)) > 260
    assert json.loads(physical.read_text(encoding="utf-8")) == {"verified": True}
    assert p.read_snapshot(root)["evidence"] == inventory["evidence"]
    assert publish(root, inventory)["unchanged"]


def test_unknown_historical_outputs_remain_explicit(tmp_path, inventory):
    for field in ("generation_id", "run_id", "processing_id"):
        inventory["outputs"][0][field] = None
    inventory["outputs"][0]["evidence_status"] = "artifact_observed"
    inventory["outputs"][0]["unknown_reason"] = "Historical write event was not retained."
    inventory["source_links"][0]["generation_id"] = None
    inventory["source_links"][0]["role"] = "unknown"
    publish(tmp_path, inventory, execution_kind="historical_import")
    assert p.read_snapshot(tmp_path)["outputs"][0]["generation_id"] is None
