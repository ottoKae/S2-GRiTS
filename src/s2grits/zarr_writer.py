from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import rasterio
import requests
import zarr
from rasterio.windows import Window

from .bands import BAND_LABELS, resolution_metres, selected_native_groups, variable_name
from .locking import InterProcessLock
from .canonical_catalog_schema import (
    GRID_ID_VERSION,
    format_grid_name,
    make_grid_id,
    make_processing_signature,
)
from .monitor import RunProgress, format_bytes, format_duration, format_rate
from .stac import S2Item
from .zarr_cf import add_cf_grid_mapping


_thread_local = threading.local()
_logger = logging.getLogger("s2grits")


def _get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = requests.Session()
        retry = Retry(
            total=5,
            read=5,
            connect=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _thread_local.session = session
    return session


def _metadata_path(zarr_path: Path) -> Path:
    return zarr_path / "s2grits_resume_state.json"


def _load_metadata(zarr_path: Path) -> dict:
    path = _metadata_path(zarr_path)
    legacy_path = zarr_path / "s2grits_metadata.json"
    if not path.exists() and legacy_path.exists():
        path = legacy_path
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"items": []}


def _save_metadata(zarr_path: Path, metadata: dict) -> None:
    path = _metadata_path(zarr_path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _time64(value: str) -> np.datetime64:
    return np.datetime64(value.replace("Z", "").replace("+00:00", ""), "ns")


def _band_metadata(asset: dict) -> dict:
    raster_bands = asset.get("raster:bands") or []
    band_meta = dict(raster_bands[0]) if raster_bands else {}
    band_meta["gsd"] = asset.get("gsd") or band_meta.get("spatial_resolution")
    return {key: value for key, value in band_meta.items() if value is not None}


def _band_dtype(asset: dict) -> str:
    return str(_band_metadata(asset).get("data_type") or "uint16")


def _window_iter(width: int, height: int, chunk_size: int):
    for row in range(0, height, chunk_size):
        h = min(chunk_size, height - row)
        for col in range(0, width, chunk_size):
            w = min(chunk_size, width - col)
            yield Window(col, row, w, h)


def _read_window_with_retry(src, band: str, href: str, window: Window, retries: int = 4):
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return src.read(1, window=window)
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(
        f"Read failed for band={band}, window=({int(window.col_off)}, "
        f"{int(window.row_off)}, {int(window.width)}, {int(window.height)}), "
        f"source={href}. Last error: {last_exc}"
    ) from last_exc


def _safe_file_token(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def _aria2c_path() -> str | None:
    if str(os.environ.get("S2GRITS_DOWNLOADER", "auto")).lower() in {"requests", "python"}:
        return None
    return shutil.which("aria2c")


def _cache_asset_with_aria2c(href: str, tmp: Path, item_id: str, band: str) -> None:
    aria2c = _aria2c_path()
    if not aria2c:
        raise RuntimeError("aria2c is not available")

    connections = int(os.environ.get("S2GRITS_ARIA2_CONNECTIONS", "8"))
    split = int(os.environ.get("S2GRITS_ARIA2_SPLIT", str(connections)))
    min_split = os.environ.get("S2GRITS_ARIA2_MIN_SPLIT_SIZE", "8M")
    timeout = int(os.environ.get("S2GRITS_ARIA2_TIMEOUT", "60"))
    retries = int(os.environ.get("S2GRITS_ARIA2_RETRIES", "5"))
    tmp.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        aria2c,
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--continue=true",
        "--file-allocation=none",
        f"--dir={str(tmp.parent)}",
        f"--out={tmp.name}",
        f"--max-connection-per-server={connections}",
        f"--split={split}",
        f"--min-split-size={min_split}",
        f"--timeout={timeout}",
        f"--connect-timeout={timeout}",
        f"--max-tries={retries}",
        "--retry-wait=5",
        "--summary-interval=0",
        "--console-log-level=warn",
        href,
    ]
    _logger.info(
        "[download] band START scene=%s band=%s method=aria2c connections=%d split=%d",
        item_id,
        band,
        connections,
        split,
    )
    started = time.perf_counter()
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    last_report = started
    last_growth = started
    last_size = tmp.stat().st_size if tmp.exists() else 0
    stall_seconds = int(os.environ.get("S2GRITS_ARIA2_STALL_SECONDS", "45"))
    while process.poll() is None:
        time.sleep(1.0)
        now = time.perf_counter()
        current_size = tmp.stat().st_size if tmp.exists() else 0
        if current_size > last_size:
            last_size = current_size
            last_growth = now
        elif now - last_growth >= stall_seconds:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise RuntimeError(
                f"aria2c stalled at {format_bytes(current_size)} for {stall_seconds}s"
            )
        if now - last_report >= 30:
            elapsed = max(0.001, now - started)
            _logger.info(
                "[download] band PROGRESS scene=%s band=%s size=%s speed=%s elapsed=%s",
                item_id,
                band,
                format_bytes(current_size),
                format_rate(current_size / elapsed),
                format_duration(elapsed),
            )
            last_report = now
    output, _ = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"aria2c failed with exit code {process.returncode}: {(output or '').strip()}"
        )


def _cache_asset_with_requests(href: str, tmp: Path, item_id: str, band: str) -> None:
    session = _get_session()
    downloaded = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
    with session.get(href, stream=True, timeout=(20, 90), headers=headers) as response:
        response.raise_for_status()
        mode = "ab" if downloaded and response.status_code == 206 else "wb"
        if mode == "wb":
            downloaded = 0
        started = time.perf_counter()
        last_report = started
        with tmp.open(mode) as stream:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                stream.write(chunk)
                downloaded += len(chunk)
                now = time.perf_counter()
                if now - last_report >= 30:
                    _logger.info(
                        "[download] band PROGRESS scene=%s band=%s size=%s speed=%s elapsed=%s",
                        item_id,
                        band,
                        format_bytes(downloaded),
                        format_rate(downloaded / max(0.001, now - started)),
                        format_duration(now - started),
                    )
                    last_report = now


def _cache_asset_unlocked(href: str, cache_dir: Path, item_id: str, band: str) -> Path:
    scheme = urlparse(str(href)).scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported asset URL scheme for band={band}: {href}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(href.split("?")[0]).suffix or ".tif"
    out = cache_dir / _safe_file_token(item_id) / f"{_safe_file_token(band)}{ext}"
    if out.exists() and out.stat().st_size > 0:
        try:
            _assert_raster_opens(out, band)
            _logger.info(
                "[download] cache HIT scene=%s band=%s size=%s",
                item_id,
                band,
                format_bytes(out.stat().st_size),
            )
            return out
        except Exception:
            out.unlink(missing_ok=True)

    tmp = out.with_suffix(out.suffix + ".part")
    out.parent.mkdir(parents=True, exist_ok=True)
    wait_seconds = 2.0
    last_exc: Exception | None = None
    use_aria2 = _aria2c_path() is not None
    for attempt in range(5):
        started = time.perf_counter()
        try:
            method = "aria2c" if use_aria2 else "requests"
            _logger.info(
                "[download] band attempt scene=%s band=%s method=%s attempt=%d/5",
                item_id,
                band,
                method,
                attempt + 1,
            )
            if use_aria2:
                _cache_asset_with_aria2c(href, tmp, item_id, band)
            else:
                _cache_asset_with_requests(href, tmp, item_id, band)
            if not tmp.exists() or tmp.stat().st_size <= 0:
                raise RuntimeError("downloaded file is empty")
            size = tmp.stat().st_size
            os.replace(tmp, out)
            _assert_raster_opens(out, band)
            control_path = Path(str(tmp) + ".aria2")
            control_path.unlink(missing_ok=True)
            elapsed = time.perf_counter() - started
            _logger.info(
                "[download] band END scene=%s band=%s size=%s speed=%s elapsed=%s",
                item_id,
                band,
                format_bytes(size),
                format_rate(size / max(0.001, elapsed)),
                format_duration(elapsed),
            )
            return out
        except Exception as exc:
            last_exc = exc
            _logger.warning(
                "[download] band RETRY scene=%s band=%s attempt=%d/5 elapsed=%s error=%s",
                item_id,
                band,
                attempt + 1,
                format_duration(time.perf_counter() - started),
                exc,
            )
            if use_aria2:
                _logger.warning(
                    "[download] switching scene=%s band=%s from aria2c to requests",
                    item_id,
                    band,
                )
                use_aria2 = False
            if attempt < 4:
                time.sleep(wait_seconds)
                wait_seconds *= 2
    raise RuntimeError(f"Failed to cache band={band}, url={href}. Last error: {last_exc}") from last_exc


def cache_asset(
    href: str, cache_dir: Path, item_id: str, band: str, lock_dir: Path | None = None
) -> Path:
    if lock_dir is None:
        return _cache_asset_unlocked(href, cache_dir, item_id, band)
    lock = lock_dir / "cache" / _safe_file_token(item_id) / f"{_safe_file_token(band)}.lock"
    with InterProcessLock(lock):
        return _cache_asset_unlocked(href, cache_dir, item_id, band)


def _assert_raster_opens(path: str | Path, band: str) -> None:
    try:
        with rasterio.open(str(path)) as ds:
            if ds.count < 1 or ds.width <= 0 or ds.height <= 0:
                raise RuntimeError(
                    f"invalid raster dimensions: {ds.width}x{ds.height}, count={ds.count}"
                )
    except Exception as exc:
        raise RuntimeError(f"Cached asset for band={band} is not readable: {path}. {exc}") from exc


def _resolve_asset_paths(
    *,
    item: S2Item,
    bands: list[str],
    cache_dir: Path | None,
    read_mode: str,
) -> dict[str, str | Path]:
    mode = read_mode.lower()
    if mode == "remote_stream_first":
        return {band: item.assets[band]["href"] for band in bands}
    if mode != "local_cache_first":
        raise ValueError(f"Unsupported read_mode: {read_mode}")
    if cache_dir is None:
        raise ValueError("cache_dir is required when read_mode='local_cache_first'.")
    return {
        band: cache_asset(item.assets[band]["href"], cache_dir, item.id, band)
        for band in bands
    }


def _profile_from_dataset(dataset) -> dict:
    return {
        "crs": dataset.crs,
        "transform": dataset.transform,
        "height": int(dataset.height),
        "width": int(dataset.width),
    }


def _profile_signature(profile: dict) -> tuple:
    crs = profile["crs"].to_string() if profile["crs"] else None
    transform = tuple(round(float(value), 10) for value in list(profile["transform"])[:6])
    return crs, transform, int(profile["height"]), int(profile["width"])


def _validate_native_grid(dataset, reference: dict, band: str, resolution: int) -> None:
    profile = _profile_from_dataset(dataset)
    if _profile_signature(profile) != _profile_signature(reference):
        raise ValueError(
            f"Native grid mismatch for {band}: {_profile_signature(profile)} != "
            f"{_profile_signature(reference)}"
        )
    pixel_x = abs(float(dataset.transform.a))
    pixel_y = abs(float(dataset.transform.e))
    if abs(pixel_x - resolution) > 0.01 or abs(pixel_y - resolution) > 0.01:
        raise ValueError(
            f"Band {band} is not on its declared {resolution} m grid: "
            f"pixel size is {pixel_x} x {pixel_y}."
        )


def _init_resolution_group(
    *,
    root,
    group_name: str,
    item: S2Item,
    bands: list[str],
    reference: dict,
    chunk_size: int,
):
    group = root.require_group(group_name)
    height = int(reference["height"])
    width = int(reference["width"])
    transform = reference["transform"]
    crs = reference["crs"]
    crs_string = crs.to_string() if crs else None
    if crs_string is None:
        raise ValueError(f"Native group {group_name} has no CRS.")
    transform_values = [float(value) for value in list(transform)[:6]]
    resolution = [abs(transform_values[0]), abs(transform_values[4])]
    grid_id = make_grid_id(
        item.tile_id,
        crs_string,
        transform_values,
        width,
        height,
        resolution=resolution,
    )
    processing_values = {
        "assets.resolution_mode": "native_groups",
        "assets.resampling": "none",
        "processing.cloud_mask.enabled": False,
        "resolution_group": group_name,
        "bands": [variable_name(band) for band in bands],
    }
    processing_signature = make_processing_signature(processing_values)
    expected_signature = _profile_signature(reference)
    expected_signature_json = json.dumps(expected_signature, ensure_ascii=True)

    existing_signature = group.attrs.get("grid_signature")
    if existing_signature and existing_signature != expected_signature_json:
        raise ValueError(
            f"Existing {group_name} Zarr grid does not match source grid: "
            f"{existing_signature} != {expected_signature_json}"
        )
    if "x" not in group:
        xs = transform.c + (np.arange(width, dtype="float64") + 0.5) * transform.a
        x_array = group.create_array(
            "x", data=xs, chunks=(min(width, 4096),), dimension_names=["x"]
        )
        x_array.attrs["_ARRAY_DIMENSIONS"] = ["x"]
    if "y" not in group:
        ys = transform.f + (np.arange(height, dtype="float64") + 0.5) * transform.e
        y_array = group.create_array(
            "y", data=ys, chunks=(min(height, 4096),), dimension_names=["y"]
        )
        y_array.attrs["_ARRAY_DIMENSIONS"] = ["y"]
    if "time" not in group:
        time_array = group.create_array(
            "time",
            shape=(0,),
            chunks=(1024,),
            dtype="datetime64[ns]",
            dimension_names=["time"],
        )
        time_array.attrs["_ARRAY_DIMENSIONS"] = ["time"]

    group.attrs.update(
        {
            "resolution_metres": resolution_metres(group_name),
            "crs": crs_string,
            "transform": transform_values,
            "height": height,
            "width": width,
            "grid_signature": expected_signature_json,
            "grid_id": grid_id,
            "grid_name": format_grid_name(item.tile_id, transform_values, crs_string),
            "grid_version": GRID_ID_VERSION,
            "resolution": resolution,
            "grid_source": "sentinel-2-c1-l2a-native",
            "product_type": "scenes",
            "product_label": f"scenes_L2A_native_{group_name}",
            "processing_level": "L2A",
            "processing_signature": processing_signature,
            "processing_variant_json": json.dumps(
                processing_values, sort_keys=True, ensure_ascii=True
            ),
            "time_varying": True,
            "array_dims": ["time", "y", "x"],
            "resampling": "none",
        }
    )
    for band in bands:
        variable = variable_name(band)
        if variable not in group:
            array = group.create_array(
                variable,
                shape=(0, height, width),
                chunks=(1, min(chunk_size, height), min(chunk_size, width)),
                dtype=_band_dtype(item.assets[band]),
                fill_value=0,
                dimension_names=["time", "y", "x"],
            )
            array.attrs.update(
                {
                    "_ARRAY_DIMENSIONS": ["time", "y", "x"],
                    "source_band": BAND_LABELS.get(band, band),
                    "source_asset": band,
                    "grid_mapping": "spatial_ref",
                    **_band_metadata(item.assets[band]),
                }
            )
        elif tuple(group[variable].shape[1:]) != (height, width):
            raise ValueError(
                f"Existing Zarr {group_name}/{variable} shape "
                f"{group[variable].shape[1:]} "
                f"does not match source grid {(height, width)}."
            )
    if "spatial_ref" not in group:
        add_cf_grid_mapping(group, crs_string)
    return group


def _resize_native_arrays(root, band_groups: dict[str, list[str]], length: int) -> None:
    if "time" in root:
        root["time"].resize((length,))
    for group_name, bands in band_groups.items():
        if group_name not in root:
            continue
        group = root[group_name]
        if "time" in group:
            group["time"].resize((length,))
        for band in bands:
            variable = variable_name(band)
            if variable in group:
                array = group[variable]
                array.resize((length, array.shape[1], array.shape[2]))


def _write_native_band(
    *,
    source: str | Path,
    band: str,
    group_name: str,
    group,
    reference: dict,
    t_index: int,
    chunk_size: int,
    read_retries: int,
    progress: RunProgress | None,
    completed_bands: int,
    total_bands: int,
) -> None:
    source_str = str(source)
    started = time.perf_counter()
    resolution = resolution_metres(group_name)
    with rasterio.open(source_str) as src:
        _validate_native_grid(src, reference, band, resolution)
        windows = list(_window_iter(src.width, src.height, chunk_size))
        variable = variable_name(band)
        target = group[variable]
        next_log_fraction = 0.25
        for window_index, window in enumerate(windows, start=1):
            data = _read_window_with_retry(src, band, source_str, window, retries=read_retries)
            row = int(window.row_off)
            col = int(window.col_off)
            target[
                t_index,
                row : row + int(window.height),
                col : col + int(window.width),
            ] = data
            fraction = window_index / max(1, len(windows))
            if progress:
                progress.update_scene_write(
                    completed_bands=completed_bands + fraction,
                    band_count=total_bands,
                    detail=(
                        f"Zarr {group_name}/{variable} "
                        f"chunks {window_index}/{len(windows)}"
                    ),
                )
            if fraction >= next_log_fraction or window_index == len(windows):
                _logger.debug(
                    "[zarr] band progress group=%s band=%s chunks=%d/%d",
                    group_name,
                    band,
                    window_index,
                    len(windows),
                )
                next_log_fraction += 0.25
    _logger.info(
        "[zarr] band END group=%s band=%s source=%s elapsed=%s",
        group_name,
        band,
        source_str,
        format_duration(time.perf_counter() - started),
    )


def _append_item_to_native_zarr_unlocked(
    *,
    item: S2Item,
    bands: list[str],
    zarr_path: Path,
    chunk_size: int,
    read_retries: int = 4,
    cache_dir: Path | None = None,
    read_mode: str = "local_cache_first",
    progress: RunProgress | None = None,
) -> bool:
    metadata = _load_metadata(zarr_path)
    if any(entry.get("item_id") == item.id for entry in metadata.get("items", [])):
        _logger.info("[zarr] scene SKIP already committed item=%s", item.id)
        return False

    band_groups = selected_native_groups(bands)
    asset_paths = _resolve_asset_paths(
        item=item,
        bands=bands,
        cache_dir=cache_dir,
        read_mode=read_mode,
    )
    zarr_path.parent.mkdir(parents=True, exist_ok=True)
    root = zarr.open_group(str(zarr_path), mode="a")
    root.attrs.update(
        {
            "s2grits_version": "0.2.0-native",
            "collection": item.collection,
            "tile_id": item.tile_id,
            "resolution_mode": "native_groups",
            "resolution_groups": list(band_groups),
            "product_type": "scenes",
            "time_varying": True,
        }
    )
    if "time" not in root:
        time_array = root.create_array(
            "time",
            shape=(0,),
            chunks=(1024,),
            dtype="datetime64[ns]",
            dimension_names=["time"],
        )
        time_array.attrs["_ARRAY_DIMENSIONS"] = ["time"]

    references: dict[str, dict] = {}
    groups = {}
    for group_name, group_bands in band_groups.items():
        with rasterio.open(str(asset_paths[group_bands[0]])) as reference_ds:
            reference = _profile_from_dataset(reference_ds)
        references[group_name] = reference
        groups[group_name] = _init_resolution_group(
            root=root,
            group_name=group_name,
            item=item,
            bands=group_bands,
            reference=reference,
            chunk_size=chunk_size,
        )

    committed_count = len(metadata.get("items") or [])
    _resize_native_arrays(root, band_groups, committed_count)
    t_index = committed_count
    for group_name, group_bands in band_groups.items():
        group = groups[group_name]
        reference = references[group_name]
        for band in group_bands:
            array = group[variable_name(band)]
            array.resize((t_index + 1, reference["height"], reference["width"]))

    completed_bands = 0
    try:
        for group_name, group_bands in band_groups.items():
            group_started = time.perf_counter()
            _logger.info(
                "[PHASE] zarr.%s START tile=%s item=%s bands=%d grid=%dx%d",
                group_name,
                item.tile_id,
                item.id,
                len(group_bands),
                references[group_name]["width"],
                references[group_name]["height"],
            )
            for band in group_bands:
                _write_native_band(
                    source=asset_paths[band],
                    band=band,
                    group_name=group_name,
                    group=groups[group_name],
                    reference=references[group_name],
                    t_index=t_index,
                    chunk_size=chunk_size,
                    read_retries=read_retries,
                    progress=progress,
                    completed_bands=completed_bands,
                    total_bands=len(bands),
                )
                completed_bands += 1
            _logger.info(
                "[PHASE] zarr.%s END tile=%s item=%s elapsed=%s",
                group_name,
                item.tile_id,
                item.id,
                format_duration(time.perf_counter() - group_started),
            )

        root["time"].resize((t_index + 1,))
        root["time"][t_index] = _time64(item.datetime)
        for group_name in band_groups:
            group_time = groups[group_name]["time"]
            group_time.resize((t_index + 1,))
            group_time[t_index] = _time64(item.datetime)
        metadata.setdefault("items", []).append(
            {
                "item_id": item.id,
                "datetime": item.datetime,
                "tile_id": item.tile_id,
                "cloud_cover": item.cloud_cover,
                "resolution_groups": band_groups,
            }
        )
        _save_metadata(zarr_path, metadata)
    except Exception:
        _resize_native_arrays(root, band_groups, committed_count)
        _logger.exception(
            "[zarr] scene ROLLBACK tile=%s item=%s committed_index=%d",
            item.tile_id,
            item.id,
            committed_count,
        )
        raise
    return True


def append_item_to_native_zarr(
    *, item: S2Item, bands: list[str], zarr_path: Path, chunk_size: int,
    read_retries: int = 4, cache_dir: Path | None = None,
    read_mode: str = "local_cache_first", progress: RunProgress | None = None,
    lock_dir: Path | None = None,
) -> bool:
    if lock_dir is None:
        return _append_item_to_native_zarr_unlocked(
            item=item, bands=bands, zarr_path=zarr_path, chunk_size=chunk_size,
            read_retries=read_retries, cache_dir=cache_dir, read_mode=read_mode,
            progress=progress,
        )
    lock = lock_dir / "zarr" / f"{_safe_file_token(item.tile_id)}.lock"
    with InterProcessLock(lock):
        return _append_item_to_native_zarr_unlocked(
            item=item, bands=bands, zarr_path=zarr_path, chunk_size=chunk_size,
            read_retries=read_retries, cache_dir=cache_dir, read_mode=read_mode,
            progress=progress,
        )


# Compatibility alias inside this isolated fork.
append_item_to_zarr = append_item_to_native_zarr
