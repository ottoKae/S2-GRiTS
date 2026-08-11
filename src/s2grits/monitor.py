from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


CONSOLE = Console(legacy_windows=True, no_color=False)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}h{m:02d}m{s:02d}s"
    if m:
        return f"{m:d}m{s:02d}s"
    return f"{s:d}s"


def format_bytes(size: int | float | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def format_rate(bytes_per_second: float | None) -> str:
    if bytes_per_second is None or bytes_per_second <= 0:
        return "--"
    return f"{format_bytes(bytes_per_second)}/s"


def asset_declared_size(asset: dict) -> int | None:
    for key in ("file:size", "filesize", "size"):
        value = asset.get(key)
        if value is not None:
            try:
                return int(value)
            except Exception:
                return None
    return None


def scene_declared_size(item, bands: list[str]) -> int | None:
    total = 0
    known = 0
    for band in bands:
        size = asset_declared_size(item.assets.get(band, {}))
        if size is not None:
            known += 1
            total += size
    return total if known == len(bands) and known else None


class RunProgress:
    def __init__(self, tile_count: int) -> None:
        self.tile_count = tile_count
        self._lock = threading.RLock()
        self._download_tasks: dict[str, int] = {}
        self._download_started: dict[str, float] = {}
        self._query_task: int | None = None
        self._tile_scene_task: int | None = None
        self._write_task: int | None = None
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=24),
            TaskProgressColumn(),
            TextColumn("{task.fields[detail]}", justify="left"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=CONSOLE,
            refresh_per_second=4,
            expand=True,
        )
        self.tiles_task = self.progress.add_task(
            f"MGRS tiles 0/{tile_count}", total=tile_count, detail="waiting"
        )

    def __enter__(self) -> "RunProgress":
        self.progress.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.progress.stop()

    def start_query(self, tile: str, tile_index: int, intervals: int) -> None:
        with self._lock:
            if self._query_task is not None:
                self.progress.remove_task(self._query_task)
            self._query_task = self.progress.add_task(
                f"[{tile_index}/{self.tile_count}] {tile} query",
                total=max(1, intervals),
                detail="Earth Search",
            )

    def advance_query(self, found: int, interval_index: int, intervals: int) -> None:
        with self._lock:
            if self._query_task is not None:
                self.progress.update(
                    self._query_task,
                    completed=interval_index,
                    detail=f"interval {interval_index}/{intervals}, found {found}",
                )

    def finish_query(self, scene_count: int) -> None:
        with self._lock:
            if self._query_task is not None:
                self.progress.update(self._query_task, detail=f"found {scene_count} scenes")
                self.progress.remove_task(self._query_task)
                self._query_task = None

    def start_tile_scenes(self, tile: str, tile_index: int, scene_count: int) -> None:
        with self._lock:
            if self._tile_scene_task is not None:
                self.progress.remove_task(self._tile_scene_task)
            self._tile_scene_task = self.progress.add_task(
                f"[{tile_index}/{self.tile_count}] {tile} scenes",
                total=max(1, scene_count),
                detail=f"0/{scene_count} complete",
            )

    def start_download(
        self,
        *,
        item_id: str,
        tile: str,
        scene_index: int,
        scene_count: int,
        total_bytes: int | None,
        band_count: int,
    ) -> None:
        with self._lock:
            task_id = self.progress.add_task(
                f"{tile} scene {scene_index}/{scene_count} download",
                total=total_bytes,
                detail=f"0/{band_count} bands | {item_id}",
            )
            self._download_tasks[item_id] = task_id
            self._download_started[item_id] = time.perf_counter()

    def update_download(
        self,
        *,
        item_id: str,
        downloaded_bytes: int,
        total_bytes: int | None,
        completed_bands: int,
        band_count: int,
        active_bands: list[str],
    ) -> None:
        with self._lock:
            task_id = self._download_tasks.get(item_id)
            if task_id is None:
                return
            elapsed = max(0.001, time.perf_counter() - self._download_started[item_id])
            rate = downloaded_bytes / elapsed
            size_text = format_bytes(downloaded_bytes)
            if total_bytes:
                size_text += f"/{format_bytes(total_bytes)}"
            active = ",".join(active_bands[:4]) or "finalizing"
            self.progress.update(
                task_id,
                completed=downloaded_bytes if total_bytes else completed_bands,
                total=total_bytes if total_bytes else band_count,
                detail=(
                    f"{completed_bands}/{band_count} bands | {size_text} | "
                    f"{format_rate(rate)} | {active}"
                ),
            )

    def finish_download(self, item_id: str, *, status: str, size: int) -> None:
        with self._lock:
            task_id = self._download_tasks.pop(item_id, None)
            self._download_started.pop(item_id, None)
            if task_id is not None:
                self.progress.update(task_id, detail=f"{status} | {format_bytes(size)}")
                self.progress.remove_task(task_id)

    def start_scene_write(
        self,
        *,
        tile: str,
        scene_index: int,
        scene_count: int,
        item_id: str,
        band_count: int,
    ) -> None:
        with self._lock:
            if self._write_task is not None:
                self.progress.remove_task(self._write_task)
            self._write_task = self.progress.add_task(
                f"{tile} scene {scene_index}/{scene_count} process",
                total=max(1, band_count),
                detail=f"Zarr prepare | {item_id}",
            )

    def update_scene_write(self, *, completed_bands: int, band_count: int, detail: str) -> None:
        with self._lock:
            if self._write_task is not None:
                self.progress.update(
                    self._write_task,
                    completed=completed_bands,
                    total=max(1, band_count),
                    detail=detail,
                )

    def finish_scene(self, *, completed_scenes: int, scene_count: int, detail: str) -> None:
        with self._lock:
            if self._write_task is not None:
                self.progress.update(self._write_task, detail=detail)
                self.progress.remove_task(self._write_task)
                self._write_task = None
            if self._tile_scene_task is not None:
                self.progress.update(
                    self._tile_scene_task,
                    completed=completed_scenes,
                    total=max(1, scene_count),
                    detail=f"{completed_scenes}/{scene_count} complete",
                )

    def finish_tile(self, *, tile: str, tile_index: int, ok: int, failed: int) -> None:
        with self._lock:
            if self._tile_scene_task is not None:
                self.progress.remove_task(self._tile_scene_task)
                self._tile_scene_task = None
            self.progress.update(
                self.tiles_task,
                completed=tile_index,
                description=f"MGRS tiles {tile_index}/{self.tile_count}",
                detail=f"{ok} ok, {failed} partial/failed | last {tile}",
            )

    def set_final_stage(self, detail: str) -> None:
        with self._lock:
            self.progress.update(self.tiles_task, detail=detail)


@contextmanager
def phase_timer(logger: logging.Logger, name: str, **fields):
    started = time.perf_counter()
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("[PHASE] %s START%s", name, f" {suffix}" if suffix else "")
    try:
        yield
    except Exception:
        logger.exception(
            "[PHASE] %s FAIL elapsed=%s%s",
            name,
            format_duration(time.perf_counter() - started),
            f" {suffix}" if suffix else "",
        )
        raise
    else:
        logger.info(
            "[PHASE] %s END elapsed=%s%s",
            name,
            format_duration(time.perf_counter() - started),
            f" {suffix}" if suffix else "",
        )


def setup_logger(log_dir: Path, dataset_name: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_dataset = "".join(c if c.isalnum() or c in "-_" else "-" for c in dataset_name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / (
        f"S2-GRiTS__dataset-{safe_dataset}__workflow-scenes-L2A-native"
        f"__started-{stamp}__pid-{os.getpid()}.log"
    )

    logger = logging.getLogger("s2grits")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    stream_handler = RichHandler(
        level=logging.INFO,
        console=CONSOLE,
        show_time=False,
        show_path=False,
        show_level=True,
        markup=False,
        rich_tracebacks=True,
    )

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.log_path = str(log_path)  # type: ignore[attr-defined]
    logger.info("Logging system configured")
    logger.info("  - Log file: %s", log_path)
    logger.info("  - File level: DEBUG")
    logger.info("  - Console level: INFO")
    return logger
