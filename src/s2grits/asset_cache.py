from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
import logging
from pathlib import Path
import time
from typing import Iterator

from .monitor import RunProgress, format_bytes, format_duration, format_rate, scene_declared_size
from .stac import S2Item
from .zarr_writer import cache_asset


_logger = logging.getLogger("s2grits")


@dataclass
class SceneCacheResult:
    item_id: str
    tile_id: str
    status: str
    assets: dict[str, Path] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    bytes_written: int = 0
    elapsed_seconds: float = 0.0


def _scene_cache_bytes(cache_dir: Path, item_id: str, bands: list[str]) -> int:
    scene_dir = cache_dir / item_id
    if not scene_dir.exists():
        return 0
    total = 0
    for band in bands:
        sizes = []
        for path in scene_dir.glob(f"{band}.tif*"):
            if path.name.endswith(".aria2") or not path.is_file():
                continue
            try:
                sizes.append(path.stat().st_size)
            except OSError:
                continue
        total += max(sizes, default=0)
    return total


def cache_scene_assets(
    *,
    item: S2Item,
    bands: list[str],
    cache_dir: Path,
    max_band_workers: int,
    scene_index: int = 1,
    scene_count: int = 1,
    progress: RunProgress | None = None,
) -> SceneCacheResult:
    started = time.perf_counter()
    assets: dict[str, Path] = {}
    errors: list[str] = []
    expected = scene_declared_size(item, bands)
    _logger.info(
        "[PHASE] download.scene START tile=%s scene=%d/%d item=%s datetime=%s "
        "cloud=%s bands=%d declared_size=%s",
        item.tile_id,
        scene_index,
        scene_count,
        item.id,
        item.datetime,
        item.cloud_cover,
        len(bands),
        format_bytes(expected),
    )
    if progress:
        progress.start_download(
            item_id=item.id,
            tile=item.tile_id,
            scene_index=scene_index,
            scene_count=scene_count,
            total_bytes=expected,
            band_count=len(bands),
        )

    workers = max(1, min(int(max_band_workers), len(bands)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"s2-{item.tile_id}") as pool:
        futures = {
            pool.submit(cache_asset, item.assets[band]["href"], cache_dir, item.id, band): band
            for band in bands
        }
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for future in done:
                band = futures[future]
                try:
                    assets[band] = future.result()
                except Exception as exc:
                    errors.append(f"{band}: {exc}")
            downloaded = _scene_cache_bytes(cache_dir, item.id, bands)
            if progress:
                progress.update_download(
                    item_id=item.id,
                    downloaded_bytes=downloaded,
                    total_bytes=expected,
                    completed_bands=len(assets) + len(errors),
                    band_count=len(bands),
                    active_bands=[futures[future] for future in pending],
                )

    status = "ok" if not errors else ("partial" if assets else "failed")
    bytes_written = sum(path.stat().st_size for path in assets.values() if path.exists())
    elapsed = time.perf_counter() - started
    rate = bytes_written / elapsed if elapsed > 0 else None
    if progress:
        progress.finish_download(item.id, status=status, size=bytes_written)
    _logger.info(
        "[PHASE] download.scene END tile=%s scene=%d/%d item=%s status=%s "
        "cached_bands=%d/%d size=%s speed=%s elapsed=%s",
        item.tile_id,
        scene_index,
        scene_count,
        item.id,
        status,
        len(assets),
        len(bands),
        format_bytes(bytes_written),
        format_rate(rate),
        format_duration(elapsed),
    )
    return SceneCacheResult(
        item_id=item.id,
        tile_id=item.tile_id,
        status=status,
        assets=assets,
        errors=errors,
        bytes_written=bytes_written,
        elapsed_seconds=elapsed,
    )


def iter_cached_scenes(
    *,
    items: list[S2Item],
    bands: list[str],
    cache_dir: Path,
    max_scene_workers: int,
    max_band_workers: int,
    progress: RunProgress | None = None,
) -> Iterator[tuple[S2Item, SceneCacheResult, int]]:
    if not items:
        return

    workers = max(1, min(int(max_scene_workers), len(items)))
    batch_started = time.perf_counter()
    _logger.info(
        "[PHASE] download.tile START tile=%s scenes=%d scene_workers=%d band_workers=%d",
        items[0].tile_id,
        len(items),
        workers,
        max_band_workers,
    )
    scene_order = {item.id: index for index, item in enumerate(items, start=1)}

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="s2-scenes") as pool:
        futures = {
            pool.submit(
                cache_scene_assets,
                item=item,
                bands=bands,
                cache_dir=cache_dir,
                max_band_workers=max_band_workers,
                scene_index=scene_order[item.id],
                scene_count=len(items),
                progress=progress,
            ): item
            for item in items
        }

        for done_count, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = SceneCacheResult(
                    item_id=item.id,
                    tile_id=item.tile_id,
                    status="failed",
                    errors=[str(exc)],
                )
            total_bytes = sum(
                result_path.stat().st_size
                for result_path in result.assets.values()
                if result_path.exists()
            )
            _logger.info(
                "[download] tile=%s completed_scenes=%d/%d last=%s status=%s "
                "last_size=%s elapsed=%s",
                item.tile_id,
                done_count,
                len(items),
                item.id,
                result.status,
                format_bytes(total_bytes),
                format_duration(time.perf_counter() - batch_started),
            )
            yield item, result, done_count

    _logger.info(
        "[PHASE] download.tile END tile=%s scenes=%d elapsed=%s",
        items[0].tile_id,
        len(items),
        format_duration(time.perf_counter() - batch_started),
    )
