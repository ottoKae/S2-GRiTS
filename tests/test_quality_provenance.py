"""Offline SCL write evidence and independent pairing generations."""
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import test_quality_backfill as quality_fixture
import zarr

from s2grits import provenance
from s2grits import quality_backfill as q
from s2grits.quality_rebind import rebind_quality

job = quality_fixture.job


def test_public_quality_evidence_removes_credentials_without_changing_receipts(job):
    config, cfg, features = job
    secret = "synthetic-test-credential"
    public_url = features[0]["assets"]["scl"]["href"] + "?versionId=public%2Fversion"
    private_url = public_url + "&token=" + secret + "&X-Amz-Signature=" + secret
    features[0]["assets"]["scl"]["href"] = private_url
    manifest = Path(cfg["quality"]["frozen_features"])
    manifest.write_text("".join(json.dumps(row) + "\n" for row in features), encoding="utf-8")
    backfill = Path(cfg["quality"]["backfill_manifest"])
    rows = [json.loads(line) for line in backfill.read_text(encoding="utf-8").splitlines()]
    rows[0]["href"] = private_url
    backfill.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    cfg["quality"]["frozen_features_sha256"] = q.digest(manifest)
    cfg["quality"]["backfill_manifest_sha256"] = q.digest(backfill)
    cfg["metadata"] = {"provenance": {"enabled": True, "dataset_id": "quality", "run_id": "privacy"}}
    _config(config, cfg)
    q.run_quality_backfill(config, max_new_scenes=1)
    root = Path(cfg["output"]["base_dir"])
    receipt_path = root / "_task_state/accepted/17MNR_scene_0.json"
    original = receipt_path.read_bytes()
    receipt = json.loads(original)
    assert receipt["write_intent"]["source_version"]["href"] == private_url
    snapshot = provenance.read_snapshot(root)
    assert snapshot["sources"][0]["href"] == public_url
    assert secret not in json.dumps(snapshot["evidence"])
    evidence = snapshot["evidence"]["quality/17MNR_scene_0.json"]
    assert evidence["write_intent"]["source_version"]["href"] == public_url
    assert q.load_json(root / "companion.json")["artifacts"]["_task_state/accepted/17MNR_scene_0.json"] == q.digest(receipt_path)
    assert rebind_quality(config, run_id="privacy-pair")["quality_available"] == 1
    for path in (root / "provenance/pairings/privacy-pair").rglob("*.json"):
        assert secret not in path.read_text(encoding="utf-8")
    assert receipt_path.read_bytes() == original


def _config(config, cfg):
    q.atomic_json(config, cfg)


def _new_binding(config, cfg):
    spectral = Path(cfg["quality"]["spectral_root"])
    rows = q.read_catalog(spectral / "catalog.parquet")
    rows[0]["provider"] = "updated provider description"
    pq.write_table(q.pa.Table.from_pylist(rows, schema=q.CATALOG_SCHEMA), spectral / "catalog.parquet")
    cfg["quality"]["spectral_catalog_sha256"] = q.digest(spectral / "catalog.parquet")
    _config(config, cfg)


def _protected(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and "pairings" not in p.parts}


def _published_rows(root):
    pointer = q.load_json(root / "provenance/pairings/current.json")
    return pq.read_table(root / "provenance/pairings" / pointer["run_id"] / "quality_links.parquet").to_pylist()


def test_quality_receipt_generation_digest_and_optin_snapshot(job):
    config, cfg, _ = job
    cfg["metadata"] = {"provenance": {"enabled": True, "dataset_id": "test-quality", "run_id": "copy-one"}}
    _config(config, cfg)
    result = q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    receipt = q.load_json(root / "_task_state/accepted/17MNR_scene_0.json")
    intent = q.load_json(root / "_task_state/intents/17MNR_scene_0.json")
    assert receipt["generation_id"] == intent["generation_id"]
    assert receipt["verification"]["decoded_sha256"] == q.decoded_scene_digest(q.store_path(root, "17MNR"), 0)
    assert receipt["executed_processing"]["scale_applied"] is False
    snapshot = provenance.read_snapshot(root)
    assert len(snapshot["outputs"]) == len(snapshot["sources"]) == len(snapshot["source_links"]) == 3
    assert result["provenance"]["data_revision"] == snapshot["pointer"]["data_revision"]
    assert all(row["generation_id"] for row in snapshot["outputs"])
    before = snapshot["pointer"]
    q.run_quality_backfill(config)
    assert provenance.read_current(root) == before
    assert not list((root / "provenance/pending").glob("*.json"))
    assert rebind_quality(config, run_id="with-snapshot")["quality_available"] == 3


def test_committed_quality_restart_reuses_write_generation(job, monkeypatch):
    config, cfg, _ = job
    actual = q.verify_scene
    def fail(*args):
        raise OSError("after native commit")
    monkeypatch.setattr(q, "verify_scene", fail)
    with pytest.raises(OSError, match="native commit"):
        q.run_quality_backfill(config, max_new_scenes=1)
    root = Path(cfg["output"]["base_dir"])
    generation = q.load_json(root / "_task_state/intents/17MNR_scene_0.json")["generation_id"]
    monkeypatch.setattr(q, "verify_scene", actual)
    q.run_quality_backfill(config, max_new_scenes=1)
    assert q.load_json(root / "_task_state/accepted/17MNR_scene_0.json")["generation_id"] == generation


def test_quality_snapshot_failure_resumes_without_writing_pixels(job, monkeypatch):
    config, cfg, _ = job
    cfg["metadata"] = {"provenance": {"enabled": True, "dataset_id": "test-quality", "run_id": "copy-one"}}
    _config(config, cfg)
    actual = provenance.publish_snapshot
    def fail(*args, **kwargs):
        actual(*args, **kwargs)
        raise OSError("after provenance publish")
    monkeypatch.setattr(provenance, "publish_snapshot", fail)
    with pytest.raises(OSError, match="provenance publish"):
        q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    before = provenance.read_current(root)
    monkeypatch.setattr(provenance, "publish_snapshot", actual)
    monkeypatch.setattr(q, "append_item_to_native_zarr", lambda **kwargs: pytest.fail("recovery rewrote pixels"))
    monkeypatch.setattr(q, "download_source", lambda *args: pytest.fail("recovery downloaded source"))
    q.run_quality_backfill(config)
    assert provenance.read_current(root) == before
    assert not list((root / "provenance/pending").glob("*.json"))


def test_rebind_preserves_all_legacy_files_and_three_independent_indices(job, monkeypatch):
    config, cfg, _ = job
    q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    before = _protected(root)
    _new_binding(config, cfg)
    with pytest.raises(RuntimeError, match="changed nonempty"):
        q.run_quality_backfill(config)
    monkeypatch.setattr(q, "append_item_to_native_zarr", lambda **kwargs: pytest.fail("rebind wrote pixels"))
    monkeypatch.setattr(q, "download_source", lambda *args: pytest.fail("rebind downloaded source"))
    result = rebind_quality(config, run_id="pair-new")
    assert result["quality_available"] == 3 and not result["download_started"]
    row = _published_rows(root)[0]
    assert [row[p + "_time_index"] for p in ["spectral_10m", "spectral_20m", "quality_20m"]] == [2, 1, 0]
    assert _protected(root) == before
    assert rebind_quality(config, run_id="pair-new")["publication"]["unchanged"]


def test_rebind_unaccepted_scene_is_unavailable(job):
    config, cfg, _ = job
    q.run_quality_backfill(config, max_new_scenes=1)
    result = rebind_quality(config, run_id="partial")
    assert result["quality_available"] == 1 and result["quality_unavailable"] == 2
    rows = _published_rows(Path(cfg["output"]["base_dir"]))
    assert rows[1]["unavailable_reason"] == "no_accepted_quality_scene"


def test_rebind_rejects_changed_decoded_quality_content(job):
    config, cfg, _ = job
    q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    store = zarr.open_group(str(q.store_path(root, "17MNR")), mode="a", use_consolidated=False)
    store["20m/scl"][0, 0, 0] = 7
    result = rebind_quality(config, run_id="corrupt")
    assert result["quality_unavailable"] == 1
    assert _published_rows(root)[0]["unavailable_reason"] == "quality_pixels_changed"


def _legacy(root):
    receipt_path = root / "_task_state/accepted/17MNR_scene_0.json"
    receipt = q.load_json(receipt_path)
    receipt.pop("generation_id")
    receipt.pop("write_intent")
    receipt["verification"].pop("decoded_sha256")
    receipt["verification"].pop("decoded_hash_scheme")
    q.atomic_json(receipt_path, receipt)
    marker = q.load_json(root / "companion.json")
    marker["artifacts"]["_task_state/accepted/17MNR_scene_0.json"] = q.digest(receipt_path)
    q.atomic_json(root / "companion.json", marker)


@pytest.mark.parametrize("available", [True, False])
def test_legacy_without_pixel_digest_requires_original_cog(job, available):
    config, cfg, _ = job
    q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    _legacy(root)
    if not available:
        cfg["quality"]["local_source_candidates"] = []
        _config(config, cfg)
    before = _protected(root)
    result = rebind_quality(config, run_id="legacy")
    assert result["quality_available"] == (3 if available else 2)
    assert _published_rows(root)[0]["quality_generation_id"] is None
    assert _protected(root) == before
    assert rebind_quality(config, run_id="legacy")["publication"]["unchanged"]


def test_pairing_publish_failure_recovers_without_legacy_mutation(job, monkeypatch):
    from s2grits import quality_rebind as rebound
    config, cfg, _ = job
    q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    before = _protected(root)
    actual = rebound.atomic_json
    def fail(path, value):
        if Path(path).name == "current.json":
            raise OSError("pairing final pointer")
        return actual(path, value)
    monkeypatch.setattr(rebound, "atomic_json", fail)
    with pytest.raises(OSError, match="final pointer"):
        rebind_quality(config, run_id="interrupted")
    assert not (root / "provenance/pairings/current.json").exists()
    monkeypatch.setattr(rebound, "atomic_json", actual)
    assert rebind_quality(config, run_id="interrupted")["quality_available"] == 3
    assert _protected(root) == before


def test_pairing_blocks_pending_spectral_write_and_dry_run_does_not_publish(job):
    config, cfg, _ = job
    q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    before = _protected(root)
    assert rebind_quality(config, run_id="dry", dry_run=True)["quality_available"] == 3
    assert not (root / "provenance/pairings").exists()
    assert _protected(root) == before
    pending = Path(cfg["quality"]["spectral_root"]) / "provenance/pending/pending.json"
    q.atomic_json(pending, {"state": "writing"})
    with pytest.raises(RuntimeError, match="pending"):
        rebind_quality(config, run_id="blocked")


@pytest.mark.parametrize("change", ["checksum", "product_version"])
def test_new_source_version_is_unavailable_without_changing_quality(job, change):
    import json
    config, cfg, features = job
    q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    manifest = Path(cfg["quality"]["frozen_features"])
    flat_path = Path(cfg["quality"]["backfill_manifest"])
    flat = [json.loads(line) for line in flat_path.read_text(encoding="utf-8").splitlines()]
    if change == "checksum":
        features[0]["assets"]["scl"]["file:checksum"] = "1220" + "f"*64
        flat[0]["source_sha256"] = "f"*64
    else:
        features[0]["properties"]["s2:processing_baseline"] = "05.11"
    manifest.write_text("".join(json.dumps(row)+"\n" for row in features), encoding="utf-8")
    flat_path.write_text("".join(json.dumps(row)+"\n" for row in flat), encoding="utf-8")
    cfg["quality"]["frozen_features_sha256"] = q.digest(manifest)
    cfg["quality"]["backfill_manifest_sha256"] = q.digest(flat_path)
    _config(config, cfg)
    before = _protected(root)
    assert rebind_quality(config, run_id="new-source")["quality_unavailable"] == 1
    assert _published_rows(root)[0]["unavailable_reason"] in {
        "source_scl_content_changed", "source_product_or_asset_version_changed"}
    assert _protected(root) == before


def test_zero_new_scene_budget_leaves_no_false_provenance_pending(job):
    config, cfg, _ = job
    cfg["metadata"] = {"provenance": {"enabled": True, "dataset_id": "empty-quality", "run_id": "zero"}}
    _config(config, cfg)
    result = q.run_quality_backfill(config, max_new_scenes=0)
    root = Path(cfg["output"]["base_dir"])
    assert result["accepted_total"] == 0 and result["provenance"] is None
    assert not list((root / "provenance/pending").glob("*.json"))
    assert provenance.read_current(root) is None


def test_optin_quality_uses_spectral_lock_before_writing(job):
    from s2grits.locking import InterProcessLock
    config, cfg, _ = job
    cfg["metadata"] = {"provenance": {"enabled": True, "dataset_id": "quality", "run_id": "locked"}}
    _config(config, cfg)
    spectral = Path(cfg["quality"]["spectral_root"])
    with InterProcessLock(spectral.parent / ("." + spectral.name + ".selected.lock"), timeout=0), pytest.raises(TimeoutError):
        q.run_quality_backfill(config)
    assert not Path(cfg["output"]["base_dir"]).exists()



def test_pairing_run_id_and_published_artifact_cannot_be_silently_changed(job):
    config, cfg, _ = job
    q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    rebind_quality(config, run_id="fixed")
    _new_binding(config, cfg)
    with pytest.raises(RuntimeError, match="run_id"):
        rebind_quality(config, run_id="fixed")
    path = root / "provenance/pairings/fixed/evidence.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="artifact changed"):
        rebind_quality(config, run_id="new")



def test_rebind_validates_quality_provenance_release_before_publishing(job):
    config, cfg, _ = job
    cfg["metadata"] = {"provenance": {"enabled": True, "dataset_id": "quality", "run_id": "tracked"}}
    _config(config, cfg)
    q.run_quality_backfill(config)
    root = Path(cfg["output"]["base_dir"])
    pointer = provenance.read_current(root)
    damaged = root / "provenance/releases" / pointer["metadata_release_id"] / "variables.json"
    damaged.write_bytes(damaged.read_bytes() + b" ")
    before = _protected(root)
    with pytest.raises(ValueError, match="manifest|digest|checksum|integrity"):
        rebind_quality(config, run_id="blocked")
    assert not (root / "provenance/pairings").exists()
    assert _protected(root) == before


def test_quality_pending_rejects_unrelated_current_before_mutation(job, monkeypatch):
    from s2grits.quality_provenance import quality_inventory
    config, cfg, _ = job
    cfg["metadata"] = {"provenance": {"enabled": True, "dataset_id": "quality", "run_id": "tracked"}}
    _config(config, cfg)
    actual = provenance.publish_snapshot
    def fail(*args, **kwargs):
        raise OSError("before snapshot publish")
    monkeypatch.setattr(provenance, "publish_snapshot", fail)
    with pytest.raises(OSError, match="snapshot publish"):
        q.run_quality_backfill(config, max_new_scenes=1)
    root = Path(cfg["output"]["base_dir"])
    inventory = quality_inventory(root, q.load_json(root / "companion.json"))
    actual(root, dataset_id="quality", inventory=inventory, execution_kind="metadata_recheck")
    before = _protected(root)
    monkeypatch.setattr(provenance, "publish_snapshot", actual)
    monkeypatch.setattr(q, "append_item_to_native_zarr", lambda **kw: pytest.fail("changed current permitted pixels"))
    with pytest.raises(RuntimeError, match="publication changed"):
        q.run_quality_backfill(config)
    assert _protected(root) == before


def test_partial_quality_publication_recovery_rebases_before_next_scene(job, monkeypatch):
    config, cfg, _ = job
    cfg["metadata"] = {"provenance": {"enabled": True, "dataset_id": "quality", "run_id": "tracked"}}
    _config(config, cfg)
    actual = provenance.publish_snapshot
    def fail(*args, **kwargs):
        actual(*args, **kwargs)
        raise OSError("after partial publish")
    monkeypatch.setattr(provenance, "publish_snapshot", fail)
    with pytest.raises(OSError, match="partial publish"):
        q.run_quality_backfill(config, max_new_scenes=1)
    root = Path(cfg["output"]["base_dir"])
    first = provenance.read_snapshot(root)["outputs"][0]
    monkeypatch.setattr(provenance, "publish_snapshot", actual)
    assert q.run_quality_backfill(config)["accepted_total"] == 3
    outputs = provenance.read_snapshot(root)["outputs"]
    assert next(row for row in outputs if row["output_id"] == first["output_id"])["generation_id"] == first["generation_id"]
    assert not list((root / "provenance/pending").glob("*.json"))


def test_rebind_rejects_unrecorded_spectral_append_outside_selected_subset(job):
    from copy import deepcopy
    from dataclasses import replace

    from s2grits.provenance_production import observe_stores

    config, cfg, features = job
    spectral = Path(cfg["quality"]["spectral_root"])
    store = q.store_path(spectral, "17MNR")
    native = zarr.open_group(str(store), mode="a", use_consolidated=False)
    # Tracked native snapshots require the writer's shared committed scene order.
    for group, variable in (("10m", "red"), ("20m", "swir_1")):
        order = q.np.argsort(native[group]["time"][:])
        native[group][variable][:] = native[group][variable][:][order]
        native[group]["time"][:] = native[group]["time"][:][order]
    q.run_quality_backfill(config)
    observed = observe_stores(spectral, [store.relative_to(spectral).as_posix()])
    provenance.publish_snapshot(spectral, dataset_id="test-spectral", inventory=observed,
                                execution_kind="historical_import")
    assert rebind_quality(config, run_id="native-stable")["quality_available"] == 3
    root = Path(cfg["output"]["base_dir"])
    before = _protected(root)
    # A legacy writer can append without changing any of the selected 3 indices.
    feature = deepcopy(features[-1])
    feature["id"] = "scene_3"
    feature["properties"]["datetime"] = "2023-04-01T12:00:00.123000Z"
    item = quality_fixture.feature_to_item(feature)
    source = Path(cfg["quality"]["local_source_candidates"][0])
    local_assets = {band: {**asset, "href": str(source / f"scene_2_{band}.tif")}
                    for band, asset in item.assets.items()}
    quality_fixture.append_item_to_native_zarr(item=replace(item, assets=local_assets),
        bands=["red", "swir16"], zarr_path=store, chunk_size=512, compression=q.COMPRESSION,
        read_mode="remote_stream_first")
    quality_fixture.upsert_records(spectral / "17MNR/catalog.parquet",
        quality_fixture._catalog_records(item=item, band_groups={"10m": ["red"], "20m": ["swir16"]},
            zarr_path=store, preview_path=None, output_root=spectral, stac_api="test", provider="test"))
    quality_fixture.resync_global_catalog(spectral)
    cfg["quality"]["spectral_catalog_sha256"] = q.digest(spectral / "catalog.parquet")
    _config(config, cfg)
    with pytest.raises(ValueError, match="current spectral data changed"):
        rebind_quality(config, run_id="stale-subset")
    assert _protected(root) == before
    assert not (root / "provenance/pairings/stale-subset").exists()
