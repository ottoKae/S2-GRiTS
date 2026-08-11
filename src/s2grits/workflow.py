from __future__ import annotations

import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .asset_cache import SceneCacheResult, iter_cached_scenes
from .audit import audit_outputs
from .bands import display_bands, selected_native_groups
from .catalog import resync_global_catalog, upsert_records
from .config import date_intervals, load_config, validate_config
from .grid import enumerate_mgrs_tiles, roi_bbox
from .io import download_file, save_thumbnail
from .manifest import DownloadManifest
from .monitor import (
    CONSOLE,
    RunProgress,
    format_bytes,
    format_duration,
    phase_timer,
    scene_declared_size,
    setup_logger,
)
from .stac import S2Item, asset_summary_json, search_items
from .zarr_writer import append_item_to_native_zarr


def _date_token(item: S2Item) -> str:
    return item.dt.strftime("%Y%m%dT%H%M%S")


def _scene_name(item: S2Item, suffix: str) -> str:
    return f"s2grits_scenes_{item.tile_id}_{_date_token(item)}_{item.id}{suffix}"


def _raw_asset_name(item: S2Item, band: str, href: str) -> str:
    ext = Path(href.split("?")[0]).suffix or ".tif"
    return f"{item.id}_{band}{ext}"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _catalog_record(
    *,
    item: S2Item,
    bands: list[str],
    band_groups: dict[str, list[str]],
    zarr_path: Path | None,
    preview_path: Path | None,
    stac_api: str,
    provider: str,
) -> dict:
    return {
        "collection_id": "s2grits-scenes-native",
        "product_type": "scenes",
        "tile_id": item.tile_id,
        "item_id": item.id,
        "datetime": item.datetime,
        "date": item.dt.date().isoformat(),
        "cloud_cover": item.cloud_cover,
        "platform": item.platform,
        "source_collection": item.collection,
        "provider": provider,
        "stac_api": stac_api,
        "stac_item_url": item.self_href,
        "bands": json.dumps(bands, ensure_ascii=False),
        "resolution_groups_json": json.dumps(band_groups, ensure_ascii=False),
        "declared_size_bytes": scene_declared_size(item, bands),
        "zarr_path": str(zarr_path) if zarr_path else None,
        "cog_path": None,
        "preview_path": str(preview_path) if preview_path else None,
        "source_assets_json": asset_summary_json(item, bands),
        "bbox": json.dumps(item.bbox) if item.bbox else None,
        "geometry_json": json.dumps(item.geometry, ensure_ascii=False) if item.geometry else None,
    }


def run_scenes_workflow(config_path: str | Path) -> dict:
    run_started = time.perf_counter()
    cfg = load_config(config_path)
    errors = validate_config(cfg)
    if errors:
        raise ValueError("\n".join(errors))

    bands = [str(band) for band in cfg["assets"]["bands"]]
    band_groups = selected_native_groups(bands)
    output_root = Path(cfg["output"]["base_dir"])
    output_root.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(output_root, config_path)
    log_path = getattr(logger, "log_path", None)
    chunk_size = int(cfg["runtime"]["chunk_size"])
    configured_downloader = str(cfg["runtime"].get("downloader", "auto")).lower()
    if configured_downloader not in {"auto", "aria2c", "requests"}:
        raise ValueError("runtime.downloader must be auto, aria2c, or requests.")
    os.environ["S2GRITS_DOWNLOADER"] = configured_downloader
    provider = str(cfg["source"].get("provider", "Element 84 Earth Search / AWS Open Data"))
    stac_api = str(cfg["source"]["stac_api"])

    tiles = enumerate_mgrs_tiles(cfg)
    max_tiles = cfg["runtime"].get("max_tiles")
    if max_tiles:
        tiles = tiles[: int(max_tiles)]
    formats = cfg["output"]["formats"]
    max_scenes = cfg["runtime"].get("max_scenes_per_tile")
    bbox = roi_bbox(cfg)
    intervals = date_intervals(cfg)
    status_path = output_root / "run_status.json"
    summary_path = output_root / "run_summary.json"
    expected_by_tile: dict[str, list[S2Item]] = {}
    results: dict[str, dict] = {}
    manifest = DownloadManifest(
        output_root,
        flush_interval_seconds=float(cfg["runtime"].get("manifest_flush_seconds", 30)),
    )

    CONSOLE.rule("S2-GRiTS Native: Scenes Workflow")
    CONSOLE.print(f"Config:              {Path(config_path).resolve()}")
    CONSOLE.print(f"Output:              {output_root}")
    CONSOLE.print(f"Collection:          {cfg['source']['collection']}")
    CONSOLE.print(f"Date interval:       {intervals[0].split('/')[0]} / {intervals[-1].split('/')[1]}")
    CONSOLE.print(f"Cloud threshold:     <= {cfg['source']['cloud_cover_max']}%")
    CONSOLE.print(f"MGRS tiles:          {len(tiles)}")
    for group_name, group_bands in band_groups.items():
        CONSOLE.print(f"Bands {group_name}:          {display_bands(group_bands)}")
    CONSOLE.print("Resolution mode:     native 10m/20m/60m (no resampling)")
    CONSOLE.print(
        "Workers:             "
        f"scene={cfg['runtime'].get('download_scene_workers', 2)}, "
        f"band={cfg['runtime'].get('download_band_workers', 4)}"
    )
    CONSOLE.print(f"Downloader:          {configured_downloader}")
    if log_path:
        CONSOLE.print(f"Log:                 {log_path}")
    CONSOLE.print(f"Manifest:            {manifest.csv_path}")
    CONSOLE.print()

    def write_status(
        stage: str,
        current_tile: str | None = None,
        current_scene: str | None = None,
        extra: dict | None = None,
    ) -> None:
        value = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "current_tile": current_tile,
            "current_scene": current_scene,
            "config": str(Path(config_path).resolve()),
            "output_root": str(output_root),
            "tiles": tiles,
            "tile_count": len(tiles),
            "elapsed_seconds": round(time.perf_counter() - run_started, 3),
            "results": results,
        }
        if extra:
            value.update(extra)
        _atomic_json(status_path, value)

    logger.info(
        "Starting native scenes workflow: config=%s collection=%s output=%s",
        config_path,
        cfg["source"]["collection"],
        output_root,
    )
    logger.info(
        "Config loaded: tiles=%d intervals=%d bands=%d groups=%s cloud_cover<=%s",
        len(tiles),
        len(intervals),
        len(bands),
        band_groups,
        cfg["source"]["cloud_cover_max"],
    )
    logger.info("Output policies: existing_store=%s", cfg["output"].get("existing_store", "resume"))
    write_status("started")

    with RunProgress(len(tiles)) as run_progress:
        for tile_index, tile in enumerate(tiles, start=1):
            tile_started = time.perf_counter()
            tile_dir = output_root / tile
            product_dir = tile_dir / "scenes_L2A_native"
            zarr_path = product_dir / "zarr" / f"s2grits_scenes_{tile}_L2A_native.zarr"
            tile_dir.mkdir(parents=True, exist_ok=True)
            written = 0
            skipped = 0
            failed: list[str] = []
            processed_count = 0
            cache_size = 0

            logger.info("[tile] START tile=%s index=%d/%d", tile, tile_index, len(tiles))
            logger.info("[1/6] Querying Earth Search tile=%s intervals=%d", tile, len(intervals))
            run_progress.start_query(tile, tile_index, len(intervals))
            write_status("querying", tile, extra={"tile_index": tile_index})
            tile_items_by_id: dict[str, S2Item] = {}
            query_failed = False
            for interval_index, interval in enumerate(intervals, start=1):
                interval_started = time.perf_counter()
                try:
                    with phase_timer(
                        logger,
                        "query.interval",
                        tile=tile,
                        interval=f"{interval_index}/{len(intervals)}",
                        datetime=interval,
                    ):
                        found_interval = search_items(
                            stac_api=stac_api,
                            collection=cfg["source"]["collection"],
                            datetime_interval=interval,
                            tiles=[tile],
                            bands=bands,
                            bbox=bbox,
                            cloud_cover_max=float(cfg["source"]["cloud_cover_max"]),
                            page_limit=int(cfg["source"]["page_limit"]),
                            timeout=float(cfg["source"]["request_timeout_seconds"]),
                            max_retries=int(cfg["source"]["max_retries"]),
                            backoff_seconds=float(cfg["source"]["retry_backoff_seconds"]),
                            grid_code=tile,
                        )
                    for item in found_interval:
                        tile_items_by_id[item.id] = item
                    run_progress.advance_query(
                        len(tile_items_by_id), interval_index, len(intervals)
                    )
                    logger.info(
                        "[query] tile=%s interval=%d/%d found_now=%d unique_total=%d elapsed=%s",
                        tile,
                        interval_index,
                        len(intervals),
                        len(found_interval),
                        len(tile_items_by_id),
                        format_duration(time.perf_counter() - interval_started),
                    )
                except Exception as exc:
                    query_failed = True
                    failed.append(f"query {interval}: {exc}")
                    logger.exception("[query] FAILED tile=%s interval=%s", tile, interval)
                    break

            tile_items = sorted(tile_items_by_id.values(), key=lambda item: (item.datetime, item.id))
            if max_scenes:
                tile_items = tile_items[: int(max_scenes)]
            expected_by_tile[tile] = tile_items
            run_progress.finish_query(len(tile_items))

            if query_failed:
                results[tile] = {
                    "status": "failed",
                    "items_found": len(tile_items),
                    "written": 0,
                    "skipped": 0,
                    "failed": failed,
                    "tile_dir": str(tile_dir),
                }
                ok_tiles = sum(1 for result in results.values() if result.get("status") == "success")
                failed_tiles = len(results) - ok_tiles
                run_progress.finish_tile(
                    tile=tile,
                    tile_index=tile_index,
                    ok=ok_tiles,
                    failed=failed_tiles,
                )
                write_status("tile_query_failed", tile)
                continue

            declared_total = sum(scene_declared_size(item, bands) or 0 for item in tile_items)
            logger.info(
                "[1/6] Query complete tile=%s scenes=%d declared_total=%s",
                tile,
                len(tile_items),
                format_bytes(declared_total if declared_total else None),
            )
            logger.info("[2/6] Writing download manifest tile=%s scenes=%d", tile, len(tile_items))
            for scene_index, item in enumerate(tile_items, start=1):
                manifest.discover(
                    item=item,
                    bands=bands,
                    tile_index=tile_index,
                    tile_count=len(tiles),
                    scene_index=scene_index,
                    scene_count=len(tile_items),
                    stac_api=stac_api,
                    provider=provider,
                )
                logger.info(
                    "[scene-list] tile=%s scene=%d/%d item=%s datetime=%s cloud=%s "
                    "source=%s declared_size=%s",
                    tile,
                    scene_index,
                    len(tile_items),
                    item.id,
                    item.datetime,
                    item.cloud_cover,
                    item.collection,
                    format_bytes(scene_declared_size(item, bands)),
                )
            manifest.flush_if_due(force=True)
            run_progress.start_tile_scenes(tile, tile_index, len(tile_items))
            write_status(
                "manifest_ready",
                tile,
                extra={"tile_index": tile_index, "scene_count": len(tile_items)},
            )

            cache_dir = output_root / "_asset_cache"
            scene_order = {item.id: index for index, item in enumerate(tile_items, start=1)}

            def mark_cache_failure(item: S2Item, cache_result: SceneCacheResult) -> None:
                nonlocal processed_count
                processed_count += 1
                message = "; ".join(cache_result.errors) or "asset cache failed"
                failed.append(f"{item.id}: {message}")
                manifest.update(
                    item.id,
                    event="cache_failed",
                    status="failed",
                    current_stage="download_failed",
                    downloaded_size_bytes=cache_result.bytes_written,
                    download_seconds=cache_result.elapsed_seconds,
                    total_seconds=cache_result.elapsed_seconds,
                    error=message,
                )
                manifest.flush_if_due()
                run_progress.finish_scene(
                    completed_scenes=processed_count,
                    scene_count=len(tile_items),
                    detail=f"FAILED download | {item.id}",
                )
                logger.error(
                    "[scene] FAILED tile=%s scene=%d/%d item=%s stage=download error=%s",
                    tile,
                    scene_order[item.id],
                    len(tile_items),
                    item.id,
                    message,
                )

            def process_scene(item: S2Item, cache_result: SceneCacheResult) -> None:
                nonlocal written, skipped, processed_count, cache_size
                scene_index = scene_order[item.id]
                scene_started = time.perf_counter()
                zarr_seconds = 0.0
                preview_seconds = 0.0
                preview_path: Path | None = None
                cache_size += cache_result.bytes_written
                processed_count += 1
                run_progress.start_scene_write(
                    tile=tile,
                    scene_index=scene_index,
                    scene_count=len(tile_items),
                    item_id=item.id,
                    band_count=len(bands),
                )
                manifest.update(
                    item.id,
                    event="cache_complete",
                    status="processing",
                    current_stage="zarr",
                    downloaded_size_bytes=cache_result.bytes_written,
                    download_seconds=cache_result.elapsed_seconds,
                    error=None,
                )
                write_status(
                    "scene_processing",
                    tile,
                    item.id,
                    extra={
                        "tile_index": tile_index,
                        "scene_index": scene_index,
                        "scene_count": len(tile_items),
                        "processed_scenes": processed_count,
                    },
                )
                logger.info(
                    "[scene] START tile=%s tile_index=%d/%d scene=%d/%d item=%s "
                    "datetime=%s cloud=%s cached_size=%s cache_elapsed=%s",
                    tile,
                    tile_index,
                    len(tiles),
                    scene_index,
                    len(tile_items),
                    item.id,
                    item.datetime,
                    item.cloud_cover,
                    format_bytes(cache_result.bytes_written),
                    format_duration(cache_result.elapsed_seconds),
                )
                try:
                    wrote_zarr = False
                    if formats.get("zarr", True):
                        logger.info("[3/6] Download complete item=%s", item.id)
                        logger.info("[4/6] Zarr START item=%s path=%s", item.id, zarr_path)
                        zarr_started = time.perf_counter()
                        wrote_zarr = append_item_to_native_zarr(
                            item=item,
                            bands=bands,
                            zarr_path=zarr_path,
                            chunk_size=chunk_size,
                            read_retries=int(cfg["runtime"].get("read_retries", 4)),
                            cache_dir=cache_dir,
                            read_mode=str(cfg["source"].get("read_mode", "local_cache_first")),
                            progress=run_progress,
                        )
                        zarr_seconds = time.perf_counter() - zarr_started
                        logger.info(
                            "[4/6] Zarr END item=%s wrote=%s elapsed=%s",
                            item.id,
                            wrote_zarr,
                            format_duration(zarr_seconds),
                        )
                    if wrote_zarr:
                        written += 1
                    else:
                        skipped += 1

                    run_progress.update_scene_write(
                        completed_bands=len(bands),
                        band_count=len(bands),
                        detail="Preview and catalog",
                    )
                    logger.info("[5/6] Preview/catalog START item=%s", item.id)
                    if formats.get("preview", True):
                        preview_started = time.perf_counter()
                        preview_path = product_dir / "preview" / _scene_name(item, ".jpg")
                        if not preview_path.exists():
                            saved = save_thumbnail(
                                item,
                                preview_path,
                                timeout=float(cfg["source"]["request_timeout_seconds"]),
                            )
                            if saved is None:
                                preview_path = None
                        preview_seconds = time.perf_counter() - preview_started

                    if formats.get("raw_assets", False):
                        raw_dir = (
                            tile_dir
                            / "raw"
                            / item.dt.strftime("%Y")
                            / item.dt.strftime("%m")
                            / item.id
                        )
                        for band in bands:
                            href = item.assets[band]["href"]
                            out = raw_dir / _raw_asset_name(item, band, href)
                            if not out.exists():
                                download_file(
                                    href,
                                    out,
                                    timeout=float(cfg["source"]["request_timeout_seconds"]),
                                )

                    record = _catalog_record(
                        item=item,
                        bands=bands,
                        band_groups=band_groups,
                        zarr_path=zarr_path if formats.get("zarr", True) else None,
                        preview_path=preview_path,
                        stac_api=stac_api,
                        provider=provider,
                    )
                    upsert_records(tile_dir / "catalog.parquet", [record])
                    scene_elapsed = time.perf_counter() - scene_started
                    manifest.update(
                        item.id,
                        event="scene_complete",
                        status="complete",
                        current_stage="complete",
                        zarr_seconds=zarr_seconds,
                        preview_seconds=preview_seconds,
                        total_seconds=cache_result.elapsed_seconds + scene_elapsed,
                        zarr_path=str(zarr_path) if formats.get("zarr", True) else None,
                        preview_path=str(preview_path) if preview_path else None,
                        error=None,
                    )
                    manifest.flush_if_due()
                    run_progress.finish_scene(
                        completed_scenes=processed_count,
                        scene_count=len(tile_items),
                        detail=f"complete | {item.id}",
                    )
                    logger.info(
                        "[5/6] Preview/catalog END item=%s preview=%s elapsed=%s",
                        item.id,
                        preview_path,
                        format_duration(preview_seconds),
                    )
                    logger.info(
                        "[scene] END tile=%s scene=%d/%d item=%s written=%d skipped=%d "
                        "download=%s zarr=%s preview=%s scene_total=%s run_total=%s",
                        tile,
                        scene_index,
                        len(tile_items),
                        item.id,
                        written,
                        skipped,
                        format_duration(cache_result.elapsed_seconds),
                        format_duration(zarr_seconds),
                        format_duration(preview_seconds),
                        format_duration(cache_result.elapsed_seconds + scene_elapsed),
                        format_duration(time.perf_counter() - run_started),
                    )
                except Exception as exc:
                    error_text = f"{exc}\n{traceback.format_exc()}"
                    failed.append(f"{item.id}: {error_text}")
                    manifest.update(
                        item.id,
                        event="scene_failed",
                        status="failed",
                        current_stage="processing_failed",
                        zarr_seconds=zarr_seconds,
                        preview_seconds=preview_seconds,
                        total_seconds=cache_result.elapsed_seconds
                        + (time.perf_counter() - scene_started),
                        zarr_path=str(zarr_path),
                        preview_path=str(preview_path) if preview_path else None,
                        error=str(exc),
                    )
                    manifest.flush_if_due(force=True)
                    run_progress.finish_scene(
                        completed_scenes=processed_count,
                        scene_count=len(tile_items),
                        detail=f"FAILED processing | {item.id}",
                    )
                    logger.exception(
                        "[scene] FAILED tile=%s scene=%d/%d item=%s",
                        tile,
                        scene_index,
                        len(tile_items),
                        item.id,
                    )

            logger.info(
                "[3/6] Downloading tile=%s scenes=%d scene_workers=%s band_workers=%s",
                tile,
                len(tile_items),
                cfg["runtime"].get("download_scene_workers", 2),
                cfg["runtime"].get("download_band_workers", 4),
            )
            write_status(
                "downloading",
                tile,
                extra={"tile_index": tile_index, "scene_count": len(tile_items)},
            )

            ready: dict[int, tuple[S2Item, SceneCacheResult]] = {}
            next_scene_index = 1
            for cached_item, cache_result, cached_count in iter_cached_scenes(
                items=tile_items,
                bands=bands,
                cache_dir=cache_dir,
                max_scene_workers=int(cfg["runtime"].get("download_scene_workers", 2)),
                max_band_workers=int(cfg["runtime"].get("download_band_workers", 4)),
                progress=run_progress,
            ):
                ready[scene_order[cached_item.id]] = (cached_item, cache_result)
                while next_scene_index in ready:
                    ordered_item, ordered_cache = ready.pop(next_scene_index)
                    if ordered_cache.status == "ok":
                        process_scene(ordered_item, ordered_cache)
                    else:
                        mark_cache_failure(ordered_item, ordered_cache)
                    next_scene_index += 1

            results[tile] = {
                "status": "success" if not failed else "partial",
                "items_found": len(tile_items),
                "written": written,
                "skipped": skipped,
                "failed": failed,
                "cached_size_bytes": cache_size,
                "tile_dir": str(tile_dir),
                "zarr_path": str(zarr_path),
            }
            manifest.flush_if_due(force=True)
            ok_tiles = sum(1 for result in results.values() if result.get("status") == "success")
            failed_tiles = len(results) - ok_tiles
            run_progress.finish_tile(
                tile=tile,
                tile_index=tile_index,
                ok=ok_tiles,
                failed=failed_tiles,
            )
            logger.info(
                "[tile] END tile=%s index=%d/%d found=%d written=%d skipped=%d failed=%d "
                "cache=%s elapsed=%s run_total=%s",
                tile,
                tile_index,
                len(tiles),
                len(tile_items),
                written,
                skipped,
                len(failed),
                format_bytes(cache_size),
                format_duration(time.perf_counter() - tile_started),
                format_duration(time.perf_counter() - run_started),
            )
            write_status("tile_done", tile)

        run_progress.set_final_stage("[6/6] catalog resync and audit")
        logger.info("[6/6] Global catalog resync START")
        global_catalog = resync_global_catalog(output_root)
        logger.info("[6/6] Global catalog resync END path=%s", global_catalog)
        logger.info("[6/6] Output audit START")
        audit_report = audit_outputs(
            output_root=output_root,
            expected_by_tile=expected_by_tile,
            bands=bands,
            band_groups=band_groups,
            formats=formats,
        )
        failed_workflow_tiles = [
            tile for tile, result in results.items() if result.get("status") == "failed"
        ]
        if failed_workflow_tiles:
            audit_report["warnings"].append(
                f"Workflow failed before complete inventory for tiles: {', '.join(failed_workflow_tiles)}"
            )
            audit_report["ok"] = False
            _atomic_json(Path(audit_report["report_path"]), audit_report)
        manifest.flush_if_due(force=True)
        run_progress.set_final_stage(
            f"complete | missing {len(audit_report.get('missing', []))} | "
            f"warnings {len(audit_report.get('warnings', []))}"
        )

    summary = {
        "config": str(Path(config_path).resolve()),
        "output_root": str(output_root),
        "tiles": tiles,
        "n_items": sum(int(value.get("items_found", 0)) for value in results.values()),
        "global_catalog": str(global_catalog) if global_catalog else None,
        "download_manifest_csv": str(manifest.csv_path),
        "download_manifest_parquet": str(manifest.parquet_path),
        "audit_report": audit_report,
        "elapsed_seconds": round(time.perf_counter() - run_started, 3),
        "results": results,
    }
    _atomic_json(summary_path, summary)
    write_status("complete", extra={"audit_report": audit_report, "summary_path": str(summary_path)})
    logger.info(
        "S2 native scenes workflow complete scenes=%d elapsed=%s missing=%d warnings=%d",
        summary["n_items"],
        format_duration(time.perf_counter() - run_started),
        len(audit_report.get("missing", [])),
        len(audit_report.get("warnings", [])),
    )
    return summary
