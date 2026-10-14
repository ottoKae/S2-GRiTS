"""Offline lifecycle/integrity tests. All COGs and Zarrs belong to tmp_path."""
from dataclasses import replace
import json
from pathlib import Path
import shutil

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
import zarr

from s2grits import quality_backfill as q
from s2grits.catalog import resync_global_catalog, upsert_records
from s2grits.locking import InterProcessLock
from s2grits.stac import feature_to_item
from s2grits.workflow import _catalog_records
from s2grits.zarr_writer import append_item_to_native_zarr


def tif(path, values, resolution):
    transform = from_origin(500000, 9600040, resolution, resolution)
    with rasterio.open(path, "w", driver="GTiff", count=1, dtype=values.dtype,
                       width=values.shape[1], height=values.shape[0], crs="EPSG:32717",
                       transform=transform, nodata=0) as ds:
        ds.write(values, 1)
    return {"href": f"https://e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com/{path.name}",
            "file:size": path.stat().st_size, "file:checksum": "1220"+q.digest(path),
            "raster:bands": [{"data_type": str(values.dtype), "nodata": 0}], "gsd": resolution,
            "proj:epsg": 32717, "proj:shape": list(values.shape), "proj:transform": list(transform)[:6]}


@pytest.fixture
def job(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Offline tests must not access the network")
    monkeypatch.setattr(q.requests, "get", no_network)
    source = tmp_path / "local_sources"
    source.mkdir()
    spectral = tmp_path / "spectral"
    store = q.store_path(spectral, "17MNR")
    features, flat = [], []
    for index in range(3):
        name = f"scene_{index}"
        assets = {}
        for band, res, size in (("red", 10, 12), ("swir16", 20, 6), ("scl", 20, 6)):
            values = ((np.arange(size*size).reshape(size, size)+index) % 12).astype("uint8") if band == "scl" else np.full((size, size), 100+index, dtype="uint16")
            path = source / (f"{name}_SCL.tif" if band == "scl" else f"{name}_{band}.tif")
            assets[band] = tif(path, values, res)
        feature = {"type": "Feature", "id": name, "collection": "sentinel-2-c1-l2a",
                   "properties": {"datetime": f"2023-0{index+1}-01T12:00:00.123000Z", "grid:code": "MGRS-17MNR"},
                   "assets": assets, "geometry": None}
        item = feature_to_item(feature)
        local_assets = {band: {**asset, "href": str(source / f"{name}_{band}.tif")} for band, asset in assets.items()}
        append_item_to_native_zarr(item=replace(item, assets=local_assets), bands=["red", "swir16"],
            zarr_path=store, chunk_size=512, compression=q.COMPRESSION, read_mode="remote_stream_first")
        upsert_records(spectral / "17MNR/catalog.parquet", _catalog_records(item=item,
            band_groups={"10m": ["red"], "20m": ["swir16"]}, zarr_path=store, preview_path=None,
            output_root=spectral, stac_api="test", provider="test"))
        features.append(feature)
        group = zarr.open_group(str(store), mode="r", use_consolidated=False)["20m"]
        flat.append({"collection": item.collection, "tile_id": item.tile_id, "item_id": item.id,
            "datetime_utc": item.datetime, "href": assets["scl"]["href"],
            "source_file_bytes": assets["scl"]["file:size"], "source_sha256": assets["scl"]["file:checksum"][4:],
            "shape": [6,6], "crs": "EPSG:32717", "transform": assets["scl"]["proj:transform"],
            "spectral_grid_id": group.attrs["grid_id"]})
    resync_global_catalog(spectral)
    # Deliberately give each optical group its own index order; scene_0's
    # 10m/20m/quality indices will be 2/1/0, respectively.
    root = zarr.open_group(str(store), mode="a", use_consolidated=False)
    for group, variable, order in (("10m", "red", [2,1,0]), ("20m", "swir_1", [2,0,1])):
        root[group][variable][:] = root[group][variable][:][order]
        root[group]["time"][:] = root[group]["time"][:][order]
    manifest, backfill, lock = tmp_path/"features.jsonl", tmp_path/"backfill.jsonl", tmp_path/"env.json"
    manifest.write_text("".join(json.dumps(row)+"\n" for row in features), encoding="utf-8")
    backfill.write_text("".join(json.dumps(row)+"\n" for row in flat), encoding="utf-8")
    q.atomic_json(lock, q.environment_versions())
    cfg = {"workflow": "quality_companion", "quality": {"spectral_root": str(spectral),
           "frozen_features": str(manifest), "frozen_features_sha256": q.digest(manifest),
           "backfill_manifest": str(backfill), "backfill_manifest_sha256": q.digest(backfill),
           "spectral_catalog_sha256": q.digest(spectral/"catalog.parquet"), "environment_lock": str(lock),
           "local_source_candidates": [str(source)]},
           "assets": {"bands": ["scl"], "resolution_mode": "native_groups", "resampling": "none"},
           "output": {"base_dir": str(tmp_path/"quality"), "cache_dir": str(tmp_path/"owned_cache"),
                      "formats": {"zarr": True, "cog": False, "preview": False, "raw_assets": False}},
           "cache": {"retention": "delete_after_verified", "max_scenes": 1, "max_bytes": 64*1024**2},
           "runtime": {"prefetch_scenes": 0, "chunk_size": 512, "max_download_attempts": 1,
                       "min_free_output_gib": 0, "max_output_gib": 1}}
    config = tmp_path/"config.json"
    q.atomic_json(config, cfg)
    return config, cfg, features


def test_dry_run_creates_no_products_or_cache(job):
    config, cfg, _ = job
    result = q.run_quality_backfill(config, dry_run=True)
    assert result["scenes"] == result["reusable_local_COGs"] == 3
    assert not Path(cfg["output"]["base_dir"]).exists()
    assert not Path(cfg["output"]["cache_dir"]).exists()


def test_per_tile_pilot_limit_stops_at_a_legal_prefix(job):
    config,_,_=job
    result=q.run_quality_backfill(config,max_new_scenes=8,max_new_scenes_per_tile=2)
    assert result["accepted_total"]==2 and not result["complete"]
    assert q.run_quality_backfill(config)["accepted_total"]==3


def test_full_lifecycle_three_group_indices_and_repeat_without_download(job, monkeypatch):
    config, cfg, _ = job
    result = q.run_quality_backfill(config)
    assert result["complete"] and result["accepted_total"] == 3 and not result["download_started"]
    root, cache = Path(cfg["output"]["base_dir"]), Path(cfg["output"]["cache_dir"])
    assert [p.name for p in cache.iterdir()] == [".owner.json"]
    marker = q.load_json(root/"companion.json")
    assert marker["state"] == "published" and marker["accepted_prefixes"] == {"17MNR": 3}
    receipt = q.load_json(root/"_task_state/accepted/17MNR_scene_0.json")
    assert receipt["verification"]["compared_pixels"] == 36
    assert receipt["verification"]["mismatched_pixels"] == 0
    link = receipt["link"]
    assert [link[p+"_time_index"] for p in ("spectral_10m", "spectral_20m", "quality_20m")] == [2,1,0]
    original_hash = q.digest(Path(cfg["quality"]["spectral_root"])/"catalog.parquet")
    monkeypatch.setattr(q, "download_source", lambda *a: pytest.fail("Accepted scene attempted download"))
    second = q.run_quality_backfill(config)
    assert second["accepted_this_run"] == 0 and second["complete"]
    assert q.digest(Path(cfg["quality"]["spectral_root"])/"catalog.parquet") == original_hash
    assert len(list(Path(cfg["quality"]["local_source_candidates"][0]).glob("*_SCL.tif"))) == 3


def test_committed_bad_unaccepted_tail_is_repaired_without_touching_accepted_prefix(job, monkeypatch):
    config, cfg, _ = job
    q.run_quality_backfill(config, max_new_scenes=1)
    root = Path(cfg["output"]["base_dir"])
    store = q.store_path(root, "17MNR")
    first = zarr.open_group(str(store), mode="r", use_consolidated=False)["20m/scl"][0].copy()
    actual_verify = q.verify_scene
    def interrupt(source, store, index, scene):
        if index == 1:
            raise RuntimeError("injected failure after commit")
        return actual_verify(source, store, index, scene)
    monkeypatch.setattr(q, "verify_scene", interrupt)
    with pytest.raises(RuntimeError, match="injected"):
        q.run_quality_backfill(config, max_new_scenes=1)
    assert q.load_json(root/"companion.json")["state"] == "updating"
    group = zarr.open_group(str(store), mode="a", use_consolidated=False)
    group["20m/scl"][1,2,3] = 11
    monkeypatch.setattr(q, "verify_scene", actual_verify)
    result = q.run_quality_backfill(config)
    assert result["complete"]
    assert q.load_json(root/"_task_state/recovery.json")["accepted"] == 1
    np.testing.assert_array_equal(group["20m/scl"][0], first)
    _, scenes, _ = q.load_job(config)
    with pytest.raises(RuntimeError, match="ACCEPTED"):
        q.rollback_tail(store, 0, scenes, root/"_task_state")


def test_cleanup_failure_is_resumed_without_append_or_download(job, monkeypatch):
    config, cfg, _ = job
    original = q.release_cache
    monkeypatch.setattr(q, "release_cache", lambda *a: (_ for _ in ()).throw(OSError("cleanup blocked")))
    with pytest.raises(OSError, match="cleanup"):
        q.run_quality_backfill(config, max_new_scenes=1)
    root = Path(cfg["output"]["base_dir"])
    assert q.load_json(root/"_task_state/pending.json")["state"] == "cache_release_pending"
    monkeypatch.setattr(q, "release_cache", original)
    monkeypatch.setattr(q, "append_item_to_native_zarr", lambda **kw: pytest.fail("Accepted scene appended again"))
    result = q.run_quality_backfill(config, max_new_scenes=0)
    assert result["accepted_total"] == 1
    assert list(Path(cfg["output"]["cache_dir"]).glob("*/*.tif")) == []


def test_low_disk_and_changed_source_stop_before_any_append(job, monkeypatch):
    config, cfg, _ = job
    monkeypatch.setattr(q.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(1,1,0))
    with pytest.raises(RuntimeError, match="free space"):
        q.run_quality_backfill(config)
    assert not q.store_path(cfg["output"]["base_dir"], "17MNR").exists()


def test_changed_source_fails_dry_run_and_environment_is_locked(job):
    config, cfg, _ = job
    lock = Path(cfg["quality"]["environment_lock"])
    versions = q.load_json(lock)
    versions["zarr"] = "0.0"
    q.atomic_json(lock, versions)
    with pytest.raises(RuntimeError, match="versions"):
        q.run_quality_backfill(config, dry_run=True)
    q.atomic_json(lock, q.environment_versions())
    source = Path(cfg["quality"]["local_source_candidates"][0])/"scene_0_SCL.tif"
    with source.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(RuntimeError, match="SHA256"):
        q.run_quality_backfill(config, dry_run=True)


def test_unknown_store_missing_sidecar_and_competing_writer_hold(job):
    config, cfg, _ = job
    q.run_quality_backfill(config, max_new_scenes=1)
    root = Path(cfg["output"]["base_dir"])
    with InterProcessLock(root.parent/("."+root.name+".quality.lock"), timeout=0):
        with pytest.raises(TimeoutError):
            q.run_quality_backfill(config)
    (q.store_path(root,"17MNR")/"s2grits_resume_state.json").unlink()
    with pytest.raises(RuntimeError, match="sidecar"):
        q.run_quality_backfill(config)


def test_published_artifact_mutation_and_consolidated_metadata_rejected(job):
    config, cfg, _ = job
    q.run_quality_backfill(config, max_new_scenes=1)
    root = Path(cfg["output"]["base_dir"])
    store = q.store_path(root,"17MNR")
    zarr.consolidate_metadata(str(store))
    with pytest.raises(RuntimeError, match="consolidated"):
        q.run_quality_backfill(config)


def test_interruption_during_tail_rollback_is_idempotent(job, monkeypatch):
    config, cfg, _ = job
    root = Path(cfg["output"]["base_dir"])
    actual_verify = q.verify_scene
    monkeypatch.setattr(q,"verify_scene",lambda *args: (_ for _ in ()).throw(RuntimeError("stop after commit")))
    with pytest.raises(RuntimeError,match="stop"):
        q.run_quality_backfill(config,max_new_scenes=1)
    store=q.store_path(root,"17MNR")
    zarr.open_group(str(store),mode="a",use_consolidated=False)["20m/scl"][0,0,1]=9
    monkeypatch.setattr(q,"verify_scene",actual_verify)
    original_json=q.atomic_json
    def interrupt(path,value):
        if Path(path).name=="s2grits_resume_state.json":
            raise OSError("stop between array rollback and sidecar")
        return original_json(path,value)
    monkeypatch.setattr(q,"atomic_json",interrupt)
    with pytest.raises(OSError,match="sidecar"):
        q.run_quality_backfill(config,max_new_scenes=1)
    assert zarr.open_group(str(store),mode="r",use_consolidated=False)["20m/scl"].shape[0]==0
    assert len(q.load_json(store/"s2grits_resume_state.json")["items"])==1
    monkeypatch.setattr(q,"atomic_json",original_json)
    assert q.run_quality_backfill(config)["complete"]


def test_publication_failure_never_exposes_new_generation_and_recovers(job, monkeypatch):
    config,cfg,_=job
    original=q.atomic_json
    def stop_publish(path,value):
        if Path(path).name=="companion.json" and value.get("state")=="published":
            raise OSError("publication interrupted")
        return original(path,value)
    monkeypatch.setattr(q,"atomic_json",stop_publish)
    with pytest.raises(OSError,match="publication"):
        q.run_quality_backfill(config,max_new_scenes=1)
    root=Path(cfg["output"]["base_dir"])
    assert q.load_json(root/"companion.json")["state"]=="updating"
    monkeypatch.setattr(q,"atomic_json",original)
    result=q.run_quality_backfill(config,max_new_scenes=0)
    assert result["accepted_total"]==1 and result["accepted_this_run"]==0


def test_part_files_count_toward_cache_limit_and_no_unknown_cleanup(job):
    config,cfg,_=job
    q.run_quality_backfill(config,max_new_scenes=1)
    cache=Path(cfg["output"]["cache_dir"])
    directory=cache/"scene_1"
    directory.mkdir()
    part=directory/"scl.tif.part"
    with part.open("wb") as stream:
        stream.truncate(64*1024**2)
    with pytest.raises(RuntimeError,match="cache exceeds"):
        q.run_quality_backfill(config)
    assert part.exists()


def test_no_per_scene_scan_of_historical_chunks(job, monkeypatch):
    config,cfg,_=job
    original=q._file_totals
    scans=[]
    def count(path):
        scans.append(Path(path))
        return original(path)
    monkeypatch.setattr(q,"_file_totals",count)
    q.run_quality_backfill(config)
    root=Path(cfg["output"]["base_dir"])
    assert scans.count(root)==2  # batch startup and publication only
    assert all(path==root or path.parent.name=="c" for path in scans)


def test_mock_http_download_verifies_checksum_without_real_network(job, monkeypatch):
    config,cfg,_=job
    loaded,scenes,_=q.load_job(config)
    source=Path(cfg["quality"]["local_source_candidates"][0])/"scene_0_SCL.tif"
    payload=source.read_bytes()
    class Response:
        status_code=200
        headers={"Content-Length":str(len(payload))}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,*args):yield payload
    monkeypatch.setattr(q.requests,"get",lambda *args,**kwargs:Response())
    target=config.parent/"downloaded.tif"
    q.download_source(scenes[0],target,loaded)
    assert q.digest(target)==scenes[0]["expected"]["source_sha256"]
    Response.headers={"Content-Length":str(len(payload)+1)}
    with pytest.raises(RuntimeError,match="Content-Length"):
        q.download_source(scenes[0],target.with_name("bad.tif"),loaded)


@pytest.mark.parametrize("zero", [False, True])
def test_all_pixels_including_missing_chunk_and_legal_implicit_zeros(tmp_path, zero):
    values = np.full((600,600), 0 if zero else 4, dtype="uint8")
    path = tmp_path/"scl.tif"
    asset = tif(path, values, 20)
    feature = {"id":"scene", "collection":"sentinel-2-c1-l2a", "properties":{"datetime":"2023-01-01T00:00:00Z", "grid:code":"MGRS-17MNR"}, "assets":{"scl":{**asset,"href":str(path)}}}
    item = feature_to_item(feature)
    store = tmp_path/"store.zarr"
    append_item_to_native_zarr(item=item, bands=["scl"], zarr_path=store, chunk_size=512,
                              read_mode="remote_stream_first", compression=q.COMPRESSION)
    group = zarr.open_group(str(store),mode="r",use_consolidated=False)["20m"]
    scene = {"item":item,"expected":{"shape":[600,600],"crs":"EPSG:32717","transform":asset["proj:transform"],"spectral_grid_id":group.attrs["grid_id"]}}
    chunk = store/"20m/scl/c/0/1/1"
    if chunk.exists():
        chunk.unlink()
    if zero:
        assert q.verify_scene(path,store,0,scene)["compared_pixels"] == 360000
    else:
        with pytest.raises(q.PixelMismatchError) as failure:
            q.verify_scene(path,store,0,scene)
        assert failure.value.report["compared_pixels"] == 360000
        assert failure.value.report["mismatched_pixels"] == 88*88


def test_resolver_end_to_end_same_scene_three_indices(job, tmp_path):
    resolver_module = pytest.importorskip("grits_resolver")
    from grits_resolver.spatial import TargetWindow
    from rasterio.enums import Resampling
    config, cfg, _ = job
    q.run_quality_backfill(config, max_new_scenes=1)
    s1 = tmp_path/"s1"
    s1.mkdir()
    # Target-only Catalog is enough; no S1 raster is read in this S2-window test.
    row = q.read_catalog(Path(cfg["quality"]["spectral_root"])/"catalog.parquet")[1]
    row.update({"sensor":"sentinel-1", "product_type":"smonthly", "zarr_path":"target.zarr"})
    upsert_records(s1/"catalog.parquet",[row])
    resolver = resolver_module.MultiSensorResolver(s1,cfg["quality"]["spectral_root"],
        s2_quality_root=cfg["output"]["base_dir"],validate=False)
    pair = resolver.s2_scene_quality("17MNR","scene_0")
    assert (pair.spectral_10m_index,pair.spectral_20m_index,pair.quality_20m_index)==(2,1,0)
    window=TargetWindow(0,0,6,6)
    with pytest.raises(resolver_module.CatalogConsistencyError,match="pair differs"):
        resolver.read_s2_quality_on_s1(replace(pair,quality_20m_index=2),window=window)
    result=resolver.read_s2_quality_on_s1(pair,window=window)
    assert result["scl"].dtype==np.uint8 and result["scl"][0,0]==0
    assert not result["quality_valid"][0,0] and result["quality_valid"][1,3]  # cloud class 9 is observed
    mixed=resolver.read_s2_variables_on_s1("17MNR",["swir_1","scl"],s2_time_index=1,window=window)
    np.testing.assert_array_equal(mixed["scl"],result["scl"])
    np.testing.assert_array_equal(resolver.read_s2_on_s1("17MNR","scl",s2_time_index=2,
        resolution_group="10m",window=window),result["scl"])
    with pytest.raises(ValueError,match="nearest"):
        resolver.read_s2_on_s1("17MNR","scl",s2_time_index=1,window=window,resampling=Resampling.bilinear)
    missing=resolver.s2_scene_quality("17MNR","scene_1",allow_missing=True)
    assert not resolver.read_s2_quality_on_s1(missing,window=window)["quality_available"]
    outside=resolver.read_s2_quality_on_s1(pair,window=TargetWindow(1000,1000,3,3))
    assert outside["scl"].dtype==np.uint8 and not outside["quality_valid"].any()
    # Cached window reads never rehash any Catalog or rebuild all identities.
    import grits_resolver.s2_quality as quality_reader
    original=quality_reader.digest
    quality_reader.digest=lambda *a: pytest.fail("Window read rehashed a catalog")
    try:
        resolver.read_s2_quality_on_s1(pair,window=window)
    finally:
        quality_reader.digest=original
    marker=Path(cfg["output"]["base_dir"])/"companion.json"
    value=q.load_json(marker)
    value["state"]="updating"
    q.atomic_json(marker,value)
    with pytest.raises(resolver_module.CatalogConsistencyError,match="changed"):
        resolver.read_s2_quality_on_s1(pair,window=window)
    with pytest.raises(resolver_module.CatalogConsistencyError,match="unpublished"):
        resolver_module.MultiSensorResolver(s1,cfg["quality"]["spectral_root"],
            s2_quality_root=cfg["output"]["base_dir"],validate=False)


def test_reader_rejects_changed_publication_artifact(job):
    pytest.importorskip("grits_resolver")
    from grits_resolver.s2_quality import QualityCompanion
    from grits_resolver.catalog import CatalogConsistencyError,load_catalog
    config,cfg,_=job
    q.run_quality_backfill(config,max_new_scenes=1)
    root=Path(cfg["output"]["base_dir"])
    with (root/"quality_links.parquet").open("ab") as stream:
        stream.write(b"unpublished change")
    with pytest.raises(CatalogConsistencyError,match="artifact differs"):
        QualityCompanion(cfg["quality"]["spectral_root"],root,load_catalog(cfg["quality"]["spectral_root"],"sentinel-2"))
