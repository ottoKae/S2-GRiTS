from __future__ import annotations

from pathlib import Path
from typing import Any
import calendar
from datetime import date

import yaml

from .bands import NATIVE_BANDS, selected_native_groups


DEFAULT_BANDS = list(NATIVE_BANDS)
UNSUPPORTED_DATA_BANDS = {"visual", "preview", "thumbnail"}


def load_config(path: str | Path) -> dict[str, Any]:
    cfg_path = Path(path)
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["_config_path"] = str(cfg_path.resolve())
    apply_defaults(cfg)
    return cfg


def apply_defaults(cfg: dict[str, Any]) -> None:
    cfg.setdefault("workflow", "scenes")

    roi = cfg.setdefault("roi", {})
    roi.setdefault("manual_mgrs_tiles", [])

    time_cfg = cfg.setdefault("time", {})
    time_cfg.setdefault("allow_recent", True)
    time_cfg.setdefault("query_interval", "monthly")

    source = cfg.setdefault("source", {})
    source.setdefault("stac_api", "https://earth-search.aws.element84.com/v1")
    source.setdefault("collection", "sentinel-2-c1-l2a")
    source.setdefault("provider", "Element 84 Earth Search / AWS Open Data")
    source.setdefault("cloud_cover_max", 100)
    source.setdefault("page_limit", 100)
    source.setdefault("request_timeout_seconds", 90)
    source.setdefault("max_retries", 3)
    source.setdefault("retry_backoff_seconds", 5)
    source.setdefault("read_mode", "local_cache_first")
    source.setdefault("local_cache_on_read_failure", True)

    assets = cfg.setdefault("assets", {})
    assets.setdefault("bands", DEFAULT_BANDS)
    assets.setdefault("resolution_mode", "native_groups")
    assets.setdefault("resampling", "none")

    processing = cfg.setdefault("processing", {})
    processing.setdefault("monthly", {}).setdefault("enabled", False)
    processing.setdefault("cloud_mask", {}).setdefault("enabled", False)

    output = cfg.setdefault("output", {})
    output.setdefault("base_dir", "./outputs")
    output.setdefault("existing_store", "resume")
    formats = output.setdefault("formats", {})
    formats.setdefault("zarr", True)
    formats.setdefault("cog", True)
    formats.setdefault("preview", True)
    formats.setdefault("raw_assets", False)

    runtime = cfg.setdefault("runtime", {})
    runtime.setdefault("chunk_size", 512)
    runtime.setdefault("read_retries", 4)
    runtime.setdefault("download_scene_workers", 2)
    runtime.setdefault("download_band_workers", 2)
    runtime.setdefault("downloader", "auto")
    runtime.setdefault("max_tiles", None)
    runtime.setdefault("max_scenes_per_tile", None)
    runtime.setdefault("manifest_flush_seconds", 30)


def validate_config(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if cfg.get("workflow") != "scenes":
        errors.append("Only workflow='scenes' is implemented in this S2-GRiTS build.")

    time_cfg = cfg.get("time") or {}
    has_date_range = bool(time_cfg.get("start_date") and time_cfg.get("end_date"))
    has_years = bool(time_cfg.get("years"))
    if not has_date_range and not has_years:
        errors.append("Use either time.years or time.start_date/time.end_date.")
    if has_years:
        try:
            years = [int(y) for y in time_cfg.get("years") or []]
            if not years:
                errors.append("time.years must contain at least one year.")
        except Exception:
            errors.append("time.years must be a list of integers.")
        try:
            months = [int(m) for m in time_cfg.get("months") or range(1, 13)]
            bad_months = [m for m in months if m < 1 or m > 12]
            if bad_months:
                errors.append(f"time.months contains invalid month(s): {bad_months}.")
        except Exception:
            errors.append("time.months must be a list of integers from 1 to 12.")
    if has_date_range:
        try:
            start = date.fromisoformat(str(time_cfg["start_date"]))
            end = date.fromisoformat(str(time_cfg["end_date"]))
            if end < start:
                errors.append("time.end_date must be on or after time.start_date.")
        except Exception:
            errors.append("time.start_date and time.end_date must use YYYY-MM-DD.")

    assets = cfg.get("assets") or {}
    if not assets.get("bands"):
        errors.append("assets.bands must contain at least one STAC asset key.")
    unsupported = sorted(set(assets.get("bands") or []) & UNSUPPORTED_DATA_BANDS)
    if unsupported:
        errors.append(
            "assets.bands contains preview/multiband assets that are not written "
            f"as scalar Zarr variables: {', '.join(unsupported)}."
        )
    resolution_mode = str(assets.get("resolution_mode", "native_groups")).lower()
    if resolution_mode != "native_groups":
        errors.append("This build requires assets.resolution_mode='native_groups'.")
    configured_bands = [str(band) for band in (assets.get("bands") or [])]
    grouped_bands = selected_native_groups(configured_bands)
    recognized = {band for bands in grouped_bands.values() for band in bands}
    unknown = sorted(set(configured_bands) - recognized)
    if unknown:
        errors.append(f"Unsupported native Sentinel-2 asset key(s): {', '.join(unknown)}.")
    if str(assets.get("resampling", "none")).lower() not in {"none", "native"}:
        errors.append("Native-group mode does not resample; use assets.resampling='none'.")

    cloud = (cfg.get("source") or {}).get("cloud_cover_max", 100)
    try:
        cloud_f = float(cloud)
        if cloud_f < 0 or cloud_f > 100:
            errors.append("source.cloud_cover_max must be between 0 and 100.")
    except Exception:
        errors.append("source.cloud_cover_max must be numeric.")

    read_mode = str((cfg.get("source") or {}).get("read_mode", "local_cache_first")).lower()
    if read_mode not in {"local_cache_first", "remote_stream_first"}:
        errors.append("source.read_mode must be 'local_cache_first' or 'remote_stream_first'.")

    downloader = str((cfg.get("runtime") or {}).get("downloader", "auto")).lower()
    if downloader not in {"auto", "aria2c", "requests"}:
        errors.append("runtime.downloader must be auto, aria2c, or requests.")

    out_dir = (cfg.get("output") or {}).get("base_dir")
    if not out_dir:
        errors.append("output.base_dir is required.")
    if resolution_mode == "native_groups" and bool(
        ((cfg.get("output") or {}).get("formats") or {}).get("cog", False)
    ):
        errors.append(
            "A single multiband COG cannot preserve mixed 10/20/60 m grids; "
            "set output.formats.cog=false."
        )

    return errors


def date_interval(cfg: dict[str, Any]) -> str:
    return date_intervals(cfg)[0]


def date_intervals(cfg: dict[str, Any]) -> list[str]:
    time_cfg = cfg["time"]
    if time_cfg.get("years"):
        years = [int(y) for y in time_cfg.get("years") or []]
        months = [int(m) for m in (time_cfg.get("months") or range(1, 13))]
        intervals: list[str] = []
        for year in years:
            for month in months:
                last_day = calendar.monthrange(year, month)[1]
                intervals.append(
                    f"{year:04d}-{month:02d}-01T00:00:00Z/"
                    f"{year:04d}-{month:02d}-{last_day:02d}T23:59:59Z"
                )
        return intervals
    start = date.fromisoformat(str(time_cfg["start_date"]))
    end = date.fromisoformat(str(time_cfg["end_date"]))
    if str(time_cfg.get("query_interval", "monthly")).lower() != "monthly":
        return [f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z"]

    intervals: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        interval_start = max(start, month_start)
        interval_end = min(end, month_end)
        intervals.append(
            f"{interval_start.isoformat()}T00:00:00Z/"
            f"{interval_end.isoformat()}T23:59:59Z"
        )
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return intervals
