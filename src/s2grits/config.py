from __future__ import annotations

from pathlib import Path
from typing import Any
import calendar
from datetime import date

import yaml

from .bands import NATIVE_BANDS, selected_native_groups
from .compression import DEFAULT_COMPRESSION, compression_settings


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
    output.setdefault("logs_dir", "../logs")
    output.setdefault("cache_dir", "../_cache")
    output.setdefault("existing_store", "resume")
    if output.get("zarr_compression") is None:
        output["zarr_compression"] = dict(DEFAULT_COMPRESSION)
    elif isinstance(output["zarr_compression"], dict):
        for key, value in DEFAULT_COMPRESSION.items():
            output["zarr_compression"].setdefault(key, value)
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


def output_path(cfg: dict[str, Any], key: str) -> Path:
    """Resolve auxiliary output paths relative to the dataset root."""
    value = Path(str(cfg["output"][key]))
    if value.is_absolute() or key == "base_dir":
        return value
    return Path(str(cfg["output"]["base_dir"])) / value


def validate_config(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "companions" in cfg:
        errors.append("companions is only supported by process_pipeline; use a separate pipeline YAML.")
    try:
        compression_settings((cfg.get("output") or {}).get("zarr_compression"))
    except ValueError as exc:
        errors.append(str(exc))
    chunk_size = (cfg.get("runtime") or {}).get("chunk_size", 512)
    if type(chunk_size) is not int or chunk_size <= 0:
        errors.append("runtime.chunk_size must be a positive integer.")
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
    for key in ("logs_dir", "cache_dir"):
        if not (cfg.get("output") or {}).get(key):
            errors.append(f"output.{key} is required.")
    if resolution_mode == "native_groups" and bool(
        ((cfg.get("output") or {}).get("formats") or {}).get("cog", False)
    ):
        errors.append(
            "A single multiband COG cannot preserve mixed 10/20/60 m grids; "
            "set output.formats.cog=false."
        )

    return errors


def load_pipeline_config(path: str | Path) -> dict[str, Any]:
    """Parse orchestration only; never open a disabled companion's resources."""
    path = Path(path).resolve()
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or cfg.get("workflow") != "pipeline":
        raise ValueError("process_pipeline requires workflow: pipeline")
    if set(cfg) - {"workflow", "spectral", "companions"}:
        raise ValueError("Unknown pipeline fields; scene settings belong in spectral.job_config")
    spectral = cfg.get("spectral")
    if not isinstance(spectral, dict) or set(spectral) - {"root", "job_config"}:
        raise ValueError("spectral must contain root and optional job_config")

    def resolve(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty path string")
        candidate = Path(value)
        return (candidate if candidate.is_absolute() else path.parent / candidate).resolve()

    root = resolve(spectral.get("root"), "spectral.root")
    scene_job = spectral.get("job_config")
    scene_job = resolve(scene_job, "spectral.job_config") if scene_job is not None else None
    companions = cfg.get("companions", {})
    if not isinstance(companions, dict) or set(companions) - {"scl"}:
        raise ValueError("companions supports only scl")
    scl = companions.get("scl", {})
    if not isinstance(scl, dict) or set(scl) - {"enabled", "job_config"}:
        raise ValueError("companions.scl supports enabled and job_config")
    enabled = scl.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("companions.scl.enabled must be YAML true or false, not a string")
    # Do not resolve, read or require the quality path while disabled.
    quality_job = resolve(scl.get("job_config"), "companions.scl.job_config") if enabled else None
    if path in (scene_job, quality_job):
        raise ValueError("A pipeline cannot use itself as a child job")
    return {"spectral_root": root, "scene_job": scene_job,
            "scl_enabled": enabled, "quality_job": quality_job}


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
