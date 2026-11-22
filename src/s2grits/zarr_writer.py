from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import numpy as np
import rasterio
import requests
import zarr
from rasterio.windows import Window

from .bands import BAND_LABELS, resolution_metres, selected_native_groups, variable_name
from .locking import InterProcessLock
from .compression import compression_settings, make_compressor
from .canonical_catalog_schema import (
    GRID_ID_VERSION,
    format_grid_name,
    make_grid_id,
    make_processing_signature,
)
from .monitor import RunProgress, format_bytes, format_duration, format_rate
from .scene_identity import scene_identity
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
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or not isinstance(metadata.get("items"), list):
            raise ValueError("Invalid native Zarr commit record; restore it before resuming.")
        return metadata
    if zarr_path.exists() and any(zarr_path.iterdir()):
        root = zarr.open_group(str(zarr_path), mode="r", use_consolidated=False)
        if any(array.ndim and array.shape[0] for name, array in root.arrays() if name == "time") or any(
            array.ndim == 3 and array.shape[0]
            for _, group in root.groups() for _, array in group.arrays()
        ):
            raise ValueError("Native Zarr commit record is missing for nonempty arrays; restore it before resuming.")
    return {"items": []}


def _validate_resume_contract(zarr_path: Path, metadata: dict, band_groups: dict, item: S2Item) -> None:
    """Validate existing data read-only before downloads, attribute edits or rollback."""
    items = metadata["items"]
    for entry in items:
        if not isinstance(entry, dict) or not entry.get("item_id") or not entry.get("datetime"):
            raise ValueError("Invalid native Zarr committed scene record.")
        if entry.get("resolution_groups") != band_groups:
            raise ValueError("Native Zarr band/group contract changed; use a separate store.")
    if len({entry["item_id"] for entry in items}) != len(items):
        raise ValueError("Duplicate item IDs in native Zarr commit record.")
    if not zarr_path.exists() or not any(
        path.name not in {"s2grits_resume_state.json", "s2grits_metadata.json"}
        for path in zarr_path.iterdir()
    ):
        if items:
            raise ValueError("Native Zarr arrays are missing for existing commit records.")
        return
    root = zarr.open_group(str(zarr_path), mode="r", use_consolidated=False)
    for name, value in (("tile_id", item.tile_id), ("collection", item.collection)):
        if root.attrs.get(name) not in (None, value):
            raise ValueError(f"Native Zarr {name} differs from the incoming scene.")
    declared = root.attrs.get("band_groups")
    if declared is not None and declared != band_groups:
        raise ValueError("Native Zarr band/group contract changed; use a separate store.")
    expected = {name: {variable_name(band) for band in bands} for name, bands in band_groups.items()}
    actual = {
        name: {variable for variable, array in group.arrays() if array.ndim == 3}
        for name, group in root.groups()
    }
    if actual != expected:
        raise ValueError("Native Zarr stored band/group contract differs; restore the archive or use a separate store.")
    committed = len(items)
    times = np.asarray([_time64(entry["datetime"]) for entry in items], dtype="datetime64[ns]")
    for name, group in [("root", root), *list(root.groups())]:
        if "time" not in group or group["time"].shape[0] < committed:
            raise ValueError(f"Native Zarr {name}/time is shorter than its commit record.")
        if not np.array_equal(group["time"][:committed], times):
            raise ValueError(f"Native Zarr {name}/time disagrees with its commit record.")
        for variable, array in group.arrays():
            if array.ndim == 3 and array.shape[0] < committed:
                raise ValueError(f"Native Zarr {name}/{variable} is shorter than its commit record.")


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


def _window_iter(width: int, height: int, chunk_size: int, chunk_height: int | None = None):
    chunk_height = chunk_height or chunk_size
    for row in range(0, height, chunk_height):
        h = min(chunk_height, height - row)
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


def _stable_asset_href(href: str) -> str:
    parsed = urlsplit(href)
    transient = set((
        "sig se st sp sv sr spr sip si srt ss skoid sktid skt ske sks skv "
        "saoid suoid scid ses rscc rscd rsce rscl rsct "
        "x-amz-algorithm x-amz-credential x-amz-date x-amz-expires x-amz-signedheaders "
        "x-amz-signature x-amz-security-token x-goog-algorithm x-goog-credential "
        "x-goog-date x-goog-expires x-goog-signedheaders x-goog-signature "
        "awsaccesskeyid signature expires access_token sas_token"
    ).split())
    query = sorted((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                   if key.lower() not in transient)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_cache_receipt(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_cache_receipt(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _validate_cached_bytes(path: Path, asset: dict) -> str:
    size = asset.get("file:size")
    if size is not None and (type(size) is not int or size != path.stat().st_size):
        raise ValueError("Cached asset differs from its declared file:size.")
    digest = _file_sha256(path)
    checksum = asset.get("file:checksum", asset.get("checksum:multihash"))
    # STAC file:checksum encodes SHA-256 as multihash 0x12 + 0x20 + digest.
    if isinstance(checksum, str) and checksum.lower().startswith("1220"):
        if checksum[4:].lower() != digest:
            raise ValueError("Cached asset differs from its declared SHA-256.")
    return digest


def _cache_asset_unlocked(
    href: str, cache_dir: Path, item_id: str, band: str, *, collection: str | None = None,
    asset: dict | None = None,
) -> Path:
    scheme = urlparse(str(href)).scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported asset URL scheme for band={band}: {href}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(href.split("?")[0]).suffix or ".tif"
    out = cache_dir / _safe_file_token(item_id) / f"{_safe_file_token(band)}{ext}"
    asset = asset or {}
    binding = {"collection": collection, "item_id": item_id, "asset": band,
               "href": _stable_asset_href(href),
               "file:size": asset.get("file:size"),
               "checksum": asset.get("file:checksum", asset.get("checksum:multihash"))}
    receipt_path = out.with_suffix(out.suffix + ".source.json")
    receipt = _read_cache_receipt(receipt_path)
    if out.exists() and out.stat().st_size > 0 and receipt.get("binding") == binding:
        try:
            _assert_raster_opens(out, band)
            if (receipt.get("size") != out.stat().st_size or
                    receipt.get("sha256") != _validate_cached_bytes(out, asset)):
                raise ValueError("Cached file no longer matches its receipt.")
            _logger.info(
                "[download] cache HIT scene=%s band=%s size=%s",
                item_id,
                band,
                format_bytes(out.stat().st_size),
            )
            return out
        except Exception as exc:
            _logger.warning("[download] invalid cache scene=%s band=%s: %s", item_id, band, exc)

    tmp = out.with_suffix(out.suffix + ".part")
    out.parent.mkdir(parents=True, exist_ok=True)
    partial_receipt = tmp.with_suffix(tmp.suffix + ".source.json")
    if _read_cache_receipt(partial_receipt) != binding:
        # Unknown/revised sources must never share a resumed byte range.
        tmp.unlink(missing_ok=True)
        Path(str(tmp) + ".aria2").unlink(missing_ok=True)
    _write_cache_receipt(partial_receipt, binding)
    wait_seconds = 2.0
    last_exc: Exception | None = None
    use_aria2 = _aria2c_path() is not None
    if not use_aria2 and Path(str(tmp) + ".aria2").exists():
        # aria2 pieces are not necessarily a contiguous HTTP Range prefix.
        tmp.unlink(missing_ok=True)
        Path(str(tmp) + ".aria2").unlink()
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
            try:
                _assert_raster_opens(tmp, band)
                digest = _validate_cached_bytes(tmp, asset)
            except Exception:
                tmp.unlink(missing_ok=True)
                Path(str(tmp) + ".aria2").unlink(missing_ok=True)
                raise
            os.replace(tmp, out)
            _write_cache_receipt(receipt_path, {"binding": binding, "size": size, "sha256": digest})
            partial_receipt.unlink(missing_ok=True)
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
                tmp.unlink(missing_ok=True)
                Path(str(tmp) + ".aria2").unlink(missing_ok=True)
                use_aria2 = False
            if attempt < 4:
                time.sleep(wait_seconds)
                wait_seconds *= 2
    raise RuntimeError(f"Failed to cache band={band}, url={href}. Last error: {last_exc}") from last_exc


def cache_asset(
    href: str, cache_dir: Path, item_id: str, band: str, lock_dir: Path | None = None,
    *, collection: str | None = None, asset: dict | None = None,
) -> Path:
    if lock_dir is None:
        return _cache_asset_unlocked(href, cache_dir, item_id, band, collection=collection, asset=asset)
    lock = lock_dir / "cache" / _safe_file_token(item_id) / f"{_safe_file_token(band)}.lock"
    with InterProcessLock(lock):
        return _cache_asset_unlocked(href, cache_dir, item_id, band, collection=collection, asset=asset)


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
        band: cache_asset(item.assets[band]["href"], cache_dir, item.id, band,
                          collection=item.collection, asset=item.assets[band])
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
    compression: dict | None = None,
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
                compressors=[make_compressor(_band_dtype(item.assets[band]), compression)],
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
        else:
            # Existing chunk bytes must continue to use their recorded codec.
            # A setting change applies only to new arrays; conversion is a copy.
            expected = make_compressor(group[variable].dtype, compression).to_dict()
            actual = [codec.to_dict() for codec in group[variable].compressors]
            if actual != [expected]:
                _logger.warning(
                    "[zarr] preserving existing codec for %s/%s: %s; requested=%s. "
                    "Use tools/recompress_zarr.py for a verified separate copy.",
                    group_name, variable, actual, expected,
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
        variable = variable_name(band)
        target = group[variable]
        if np.dtype(src.dtypes[0]) != target.dtype:
            raise ValueError(
                f"Refusing lossy dtype conversion for {group_name}/{variable}: "
                f"source={src.dtypes[0]}, stored={target.dtype}."
            )
        # On resume, use the stored chunk grid even if runtime.chunk_size changed.
        windows = list(_window_iter(src.width, src.height, target.chunks[2], target.chunks[1]))
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
    compression: dict | None = None,
    write_workers: int = 1,
    _provenance_tracker=None,
    on_item_conflict: str = "skip",
) -> bool:
    identity = scene_identity(item)
    compression = compression_settings(compression)
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer.")
    if type(write_workers) is not int or not 1 <= write_workers <= 4:
        raise ValueError("write_workers must be an integer from 1 to 4.")
    if on_item_conflict not in {"skip", "overwrite"}:
        raise ValueError("on_item_conflict must be skip or overwrite.")
    if on_item_conflict == "overwrite" and _provenance_tracker is None:
        raise ValueError("Replacing a committed scene requires a provenance tracker.")
    metadata = _load_metadata(zarr_path)
    band_groups = selected_native_groups(bands)
    if not bands or set(bands) != {band for group in band_groups.values() for band in group}:
        raise ValueError("Select at least one supported native band.")
    _validate_resume_contract(zarr_path, metadata, band_groups, item)
    committed_count = len(metadata["items"])
    existing_index = next((index for index, entry in enumerate(metadata["items"])
                           if entry["item_id"] == item.id), None)
    if existing_index is not None and on_item_conflict == "skip":
        _logger.info("[zarr] scene SKIP already committed item=%s", item.id)
        return False
    replacing = on_item_conflict == "overwrite"
    if replacing and existing_index is None:
        raise ValueError("Replacement requires the same committed item_id, not a matching date.")
    stored = (zarr.open_group(str(zarr_path), mode="r", use_consolidated=False)
              if _provenance_tracker is not None and committed_count else None)
    if replacing:
        prior = metadata["items"][existing_index]
        if (prior.get("tile_id") != item.tile_id
                or stored.attrs.get("tile_id") != item.tile_id
                or stored.attrs.get("collection") != item.collection
                or prior.get("collection", item.collection) != item.collection
                or _time64(prior["datetime"]) != _time64(item.datetime)):
            raise ValueError("Replacement must preserve collection, tile, item and acquisition datetime.")
        for group in [stored, *[stored[name] for name in band_groups]]:
            if any(array.shape[0] != committed_count for name, array in group.arrays()
                   if name == "time" or array.ndim == 3):
                raise ValueError("Cannot replace a scene while an uncommitted native tail exists.")
    t_index = existing_index if replacing else committed_count

    asset_paths = _resolve_asset_paths(
        item=item,
        bands=bands,
        cache_dir=cache_dir,
        read_mode=read_mode,
    )
    references: dict[str, dict] = {}
    if _provenance_tracker is not None:
        for group_name, group_bands in band_groups.items():
            with rasterio.open(str(asset_paths[group_bands[0]])) as source:
                references[group_name] = _profile_from_dataset(source)
            reference = references[group_name]
            if stored is not None:
                attrs = stored[group_name].attrs
                signature = json.dumps(_profile_signature(reference), ensure_ascii=True)
                if attrs.get("grid_signature") != signature:
                    raise ValueError("Tracked source grid differs from the committed native group.")
            for band in group_bands:
                with rasterio.open(str(asset_paths[band])) as source:
                    _validate_native_grid(source, reference, band, resolution_metres(group_name))
                    target_dtype = (stored[group_name][variable_name(band)].dtype if stored is not None
                                    else np.dtype(_band_dtype(item.assets[band])))
                    if np.dtype(source.dtypes[0]) != target_dtype:
                        raise ValueError("Tracked source dtype differs from the native output dtype.")
                    if stored is not None and stored[group_name][variable_name(band)].shape[1:] != (
                            reference["height"], reference["width"]):
                        raise ValueError("Tracked source shape differs from the native output shape.")
        _provenance_tracker.prepare(
            item=item, bands=bands, zarr_path=zarr_path, asset_paths=asset_paths,
            band_groups=band_groups, index=t_index, operation="replace" if replacing else "append",
            parameters={"chunk_size": chunk_size, "read_retries": read_retries,
                        "compression": compression, "write_workers": write_workers, "read_mode": read_mode},
        )
    zarr_path.parent.mkdir(parents=True, exist_ok=True)
    root = zarr.open_group(str(zarr_path), mode="a", zarr_format=3, use_consolidated=False)
    if not replacing:
        root.attrs.update(
            {
                "s2grits_version": "0.2.0-native",
                "collection": item.collection,
                "tile_id": item.tile_id,
                "resolution_mode": "native_groups",
                "resolution_groups": list(band_groups),
                "band_groups": band_groups,
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
                compression=compression,
            )

        _resize_native_arrays(root, band_groups, committed_count)
        for group_name, group_bands in band_groups.items():
            group = groups[group_name]
            reference = references[group_name]
            for band in group_bands:
                array = group[variable_name(band)]
                array.resize((t_index + 1, reference["height"], reference["width"]))

    else:
        groups = {name: root[name] for name in band_groups}

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
            def write_band(band: str) -> None:
                _write_native_band(
                    source=asset_paths[band], band=band, group_name=group_name,
                    group=groups[group_name], reference=references[group_name],
                    t_index=t_index, chunk_size=chunk_size, read_retries=read_retries,
                    # RunProgress is shared state; only the calling thread updates it.
                    progress=progress if write_workers == 1 else None,
                    completed_bands=completed_bands, total_bands=len(bands),
                )
                if _provenance_tracker is not None:
                    _provenance_tracker.used(band)

            if write_workers == 1:
                for band in group_bands:
                    write_band(band)
                    completed_bands += 1
            else:
                # Each task owns a distinct band array and rasterio reader. Group
                # metadata, array resizing and time/sidecar commits stay serial,
                # under the single tile lock held by the public wrapper.
                with ThreadPoolExecutor(max_workers=write_workers) as executor:
                    futures = {}
                    try:
                        for band in group_bands:
                            futures[executor.submit(write_band, band)] = band
                        for future in as_completed(futures):
                            future.result()
                            completed_bands += 1
                            if progress:
                                progress.update_scene_write(
                                    completed_bands=completed_bands,
                                    band_count=len(bands),
                                    detail=f"Zarr {group_name}/{variable_name(futures[future])} complete",
                                )
                    except BaseException:
                        for future in futures:
                            future.cancel()
                        # Exiting the executor waits for every active writer before
                        # the enclosing handler can resize arrays for rollback.
                        raise
            _logger.info(
                "[PHASE] zarr.%s END tile=%s item=%s elapsed=%s",
                group_name,
                item.tile_id,
                item.id,
                format_duration(time.perf_counter() - group_started),
            )

        if not replacing:
            root["time"].resize((t_index + 1,))
            root["time"][t_index] = _time64(item.datetime)
            for group_name in band_groups:
                group_time = groups[group_name]["time"]
                group_time.resize((t_index + 1,))
                group_time[t_index] = _time64(item.datetime)
        entry = {
            "item_id": item.id, "datetime": item.datetime, "tile_id": item.tile_id,
            "cloud_cover": item.cloud_cover, "resolution_groups": band_groups, **identity,
        }
        if replacing:
            metadata["items"][t_index] = {**metadata["items"][t_index], **entry}
        else:
            metadata["items"].append(entry)
        _save_metadata(zarr_path, metadata)
    except BaseException:
        # A replacement may already have changed committed pixels; keep its pending guard.
        if not replacing:
            _resize_native_arrays(root, band_groups, committed_count)
        _logger.exception(
            "[zarr] scene FAILED tile=%s item=%s committed_index=%d",
            item.tile_id,
            item.id,
            committed_count,
        )
        raise
    # A durable native commit must survive failure of companion publication.
    if _provenance_tracker is not None:
        _provenance_tracker.commit(item=item, zarr_path=zarr_path, index=t_index, band_groups=band_groups)
    return True


def append_item_to_native_zarr(
    *, item: S2Item, bands: list[str], zarr_path: Path, chunk_size: int,
    read_retries: int = 4, cache_dir: Path | None = None,
    read_mode: str = "local_cache_first", progress: RunProgress | None = None,
    lock_dir: Path | None = None,
    compression: dict | None = None,
    write_workers: int = 1,
    _provenance_tracker=None,
    on_item_conflict: str = "skip",
) -> bool:
    lock = nullcontext()
    if lock_dir is not None:
        lock = InterProcessLock(lock_dir / "zarr" / f"{_safe_file_token(item.tile_id)}.lock")
    with lock:
        if _provenance_tracker is None:
            for ancestor in Path(zarr_path).resolve().parents:
                if next((ancestor / "provenance" / "pending").glob("*.json"), None) is not None:
                    raise ValueError("Unfinished provenance write blocks an untracked native writer.")
        tracking = ({"_provenance_tracker": _provenance_tracker, "on_item_conflict": on_item_conflict}
                    if _provenance_tracker is not None or on_item_conflict != "skip" else {})
        return _append_item_to_native_zarr_unlocked(
            item=item, bands=bands, zarr_path=zarr_path, chunk_size=chunk_size,
            read_retries=read_retries, cache_dir=cache_dir, read_mode=read_mode,
            progress=progress,
            compression=compression,
            write_workers=write_workers,
            **tracking,
        )


# Compatibility alias inside this isolated fork.
append_item_to_zarr = append_item_to_native_zarr
