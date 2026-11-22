#!/usr/bin/env python3
"""Ordered frozen-manifest archive runner; no STAC search or compositing.

One task lock, bounded per-scene cache, verified COGs, native writer, durable
acceptance receipts. Unknown stores and incomplete array tails are held for
inspection, never reset. This is deliberately separate from the legacy CLI.
Optional bounded lookahead downloads one future scene while the sole scene
committer writes the current scene. Runtime tuning does not alter the binding.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import time
from urllib.parse import urlparse

import numpy as np
import rasterio
from rasterio.windows import Window
import requests
import zarr

from s2grits.bands import selected_native_groups, variable_name
from s2grits.catalog import read_catalog, resync_global_catalog, upsert_records
from s2grits.compression import make_compressor
from s2grits.locking import InterProcessLock
from s2grits.stac import feature_to_item
from s2grits.workflow import _catalog_records
from s2grits.zarr_writer import append_item_to_native_zarr

ASSETS = ['red', 'blue', 'green', 'nir', 'nir08', 'rededge1', 'rededge2', 'rededge3', 'swir16', 'swir22']
COMPRESSION = {'cname': 'zstd', 'clevel': 5, 'shuffle': 'shuffle'}
COORDS = {'time', 'x', 'y', 'spatial_ref'}
TRANSIENT_RETRY_DELAYS = (30, 60, 120, 240, 300, 300)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def safe_child(path, parent):
    path, parent = Path(path), Path(parent)
    require(not path.is_symlink() and not parent.is_symlink(), 'Symlink is not allowed: ' + str(path))
    require(path.resolve() != parent.resolve() and path.resolve().is_relative_to(parent.resolve()), 'Path escaped task root: ' + str(path))
    current = path
    while current != parent and current != current.parent:
        require(not current.is_symlink(), 'Symlink ancestor: ' + str(current))
        current = current.parent
    return path


def load_inputs(manifest, config):
    text = Path(config).read_text(encoding='utf8')
    if Path(config).suffix.lower() == '.json':
        cfg = json.loads(text)
    else:
        import yaml
        cfg = yaml.safe_load(text)
    require(cfg.get('assets', cfg.get('bands')) == ASSETS, 'This reviewed job requires the exact ten spectral assets in model order')
    cfg['assets'] = ASSETS
    require(cfg.get('compression') == COMPRESSION, 'Expected Blosc/Zstd5/byte shuffle')
    require(cfg.get('chunk_size', 512) == 512, 'Expected 512-pixel chunks')
    require(1 <= int(cfg.get('download_workers', 4)) <= 4, 'At most four asset download workers')
    require(1 <= int(cfg.get('max_download_attempts', 3)) <= 3, 'At most three download attempts')
    require(float(cfg.get('request_timeout_seconds', 120)) > 0, 'Positive request timeout required')
    features = [json.loads(line) for line in Path(manifest).read_text(encoding='utf8').splitlines() if line.strip()]
    require(features, 'Empty selected manifest')
    seen, last, per_tile = set(), {}, defaultdict(list)
    for feature in features:
        item = feature_to_item(feature)
        require(item is not None and feature.get('type') == 'Feature', 'Expected complete STAC Feature')
        require(item.collection == 'sentinel-2-c1-l2a', 'Unexpected source collection')
        require(re.fullmatch(r'[A-Za-z0-9_-]+', item.id) and re.fullmatch(r'\d{2}[A-Z]{3}', item.tile_id), 'Unsafe identity')
        key = (item.collection, item.tile_id, item.id)
        require(key not in seen, 'Duplicate scene identity')
        seen.add(key)
        require(item.dt.tzinfo is not None, 'Acquisition time must include timezone')
        require(item.tile_id not in last or item.dt > last[item.tile_id], 'Per-tile acquisition dates must strictly increase')
        last[item.tile_id] = item.dt
        require(isinstance(feature.get('selection_quality'), dict), 'Missing frozen selection_quality provenance')
        for band in ASSETS:
            asset = item.assets[band]
            url = urlparse(asset['href'])
            require(url.scheme == 'https' and url.hostname == 'e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com', 'Unapproved public source host')
            require(type(asset.get('file:size')) is int and asset['file:size'] > 0, 'Missing positive frozen asset size')
            require(re.fullmatch(r'1220[0-9a-fA-F]{64}', asset.get('file:checksum', '')), 'Missing SHA256 multihash')
            meta = (asset.get('raster:bands') or [{}])[0]
            require(meta.get('data_type') == 'uint16' and 'nodata' in meta and 'scale' in meta and 'offset' in meta, 'Incomplete source radiometry metadata')
        per_tile[item.tile_id].append(feature)
    return cfg, features, dict(per_tile)


def store_path(output, tile):
    return Path(output) / tile / 'scenes_L2A_native' / 'zarr' / f's2grits_scenes_{tile}_L2A_native.zarr'


def _time_ns(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
    return np.datetime64(stamp, 'ns')


def verify_store(output, tile, expected_features, *, check_catalog=True):
    """Read-only strict prefix audit; never invoke writer on an unknown tail."""
    store = store_path(output, tile)
    if not store.exists():
        require(not (Path(output) / tile / 'catalog.parquet').exists(), 'Catalog without store')
        return []
    require(not store.is_symlink(), 'Linked store is not allowed')
    sidecar = store / 's2grits_resume_state.json'
    require(sidecar.is_file(), 'Nonempty store missing resume sidecar; HOLD')
    entries = json.loads(sidecar.read_text(encoding='utf8')).get('items')
    require(isinstance(entries, list), 'Invalid resume sidecar')
    require(len(entries) <= len(expected_features), 'More committed scenes than frozen manifest')
    root = zarr.open_group(str(store), mode='r', use_consolidated=False)
    require(root.attrs.get('tile_id') == tile and root.attrs.get('collection') == 'sentinel-2-c1-l2a', 'Store identity differs')
    groups = selected_native_groups(ASSETS)
    require(set(root.group_keys()) == set(groups) and set(root.array_keys()) == {'time'}, 'Unexpected native groups/root arrays')
    require(root.attrs.get('resolution_mode') == 'native_groups', 'Store resolution mode differs')
    times = []
    for index, entry in enumerate(entries):
        item = feature_to_item(expected_features[index])
        require(entry.get('item_id') == item.id and entry.get('tile_id') == tile and _time_ns(entry['datetime']) == _time_ns(item.datetime), 'Committed identities are not a frozen chronological prefix')
        require(entry.get('resolution_groups') == groups, 'Committed bands differ')
        times.append(_time_ns(item.datetime))
    expected_times = np.asarray(times, dtype='datetime64[ns]')
    require(np.array_equal(root['time'][:], expected_times), 'Uncommitted root time tail or mismatch; HOLD')
    first = feature_to_item(expected_features[0])
    for group_name, bands in groups.items():
        group = root[group_name]
        require(set(group.array_keys()) == COORDS | {variable_name(band) for band in bands}, 'Unexpected native arrays')
        require(np.array_equal(group['time'][:], expected_times), 'Uncommitted group time tail; HOLD')
        require(group.attrs.get('resampling') == 'none', 'Resampling contract differs')
        reference_asset = first.assets[bands[0]]
        frozen_shape = reference_asset.get('proj:shape', first.properties.get('proj:shape'))
        frozen_transform = reference_asset.get('proj:transform', first.properties.get('proj:transform'))
        frozen_crs = reference_asset.get('proj:code', first.properties.get('proj:code'))
        if frozen_crs is None:
            frozen_crs = 'EPSG:' + str(reference_asset.get('proj:epsg', first.properties.get('proj:epsg')))
        require(group.attrs['crs'] == frozen_crs, 'Group CRS differs from frozen source')
        require(list(frozen_shape) == [group.attrs['height'], group.attrs['width']], 'Group dimensions differ from frozen source')
        require(np.allclose(list(frozen_transform)[:6], group.attrs['transform'], rtol=0, atol=1e-9), 'Group transform differs from frozen source')
        tr = list(frozen_transform)
        require(np.array_equal(group['x'][:], tr[2] + (np.arange(group.attrs['width']) + .5) * tr[0]), 'Native x coordinates differ')
        require(np.array_equal(group['y'][:], tr[5] + (np.arange(group.attrs['height']) + .5) * tr[4]), 'Native y coordinates differ')
        for band in bands:
            array = group[variable_name(band)]
            require(array.shape == (len(entries), int(group.attrs['height']), int(group.attrs['width'])), 'Uncommitted raster tail/shape mismatch; HOLD')
            require(array.dtype == np.dtype('uint16'), 'Lossy source dtype')
            require(array.chunks == (1, min(512, array.shape[1]), min(512, array.shape[2])), 'Unexpected chunks')
            require([codec.to_dict() for codec in array.compressors] == [make_compressor(array.dtype, COMPRESSION).to_dict()], 'Codec mismatch')
            meta = first.assets[band]['raster:bands'][0]
            require(all(array.attrs.get(key) == meta.get(key) for key in ('scale', 'offset', 'nodata', 'data_type')), 'Initial array radiometry differs')
    if check_catalog and entries:
        rows = read_catalog(Path(output) / tile / 'catalog.parquet')
        require(len(rows) == 2 * len(entries), 'Tile catalog count differs from committed prefix')
        keyed = {(row['item_id'], row['resolution_group']): row for row in rows}
        require(len(keyed) == len(rows), 'Duplicate catalog identity')
        for feature in expected_features[:len(entries)]:
            item = feature_to_item(feature)
            for group_name, bands in groups.items():
                row = keyed.get((item.id, group_name))
                require(row is not None and row['source_collection'] == item.collection and _time_ns(row['datetime']) == _time_ns(item.datetime), 'Catalog source/time mismatch')
                require(row['tile_id'] == tile and row['status'] == 'complete' and row['zarr_path'] == store.relative_to(output).as_posix() and row['zarr_group'] == group_name, 'Catalog identity/path/status mismatch')
                require(set(json.loads(row['bands']) if isinstance(row['bands'], str) else row['bands']) == {variable_name(band) for band in bands}, 'Catalog bands mismatch')
                assets = json.loads(row['source_assets_json'])
                for band in bands:
                    require(all(assets[band].get(key) == item.assets[band].get(key) for key in ('href', 'file:size', 'raster:bands')), 'Catalog source asset metadata mismatch')
    return entries


def verify_asset_file(path, asset):
    require(path.is_file() and not path.is_symlink(), 'Missing/nonregular cached asset')
    require(path.stat().st_size == asset['file:size'], 'Cached size differs from frozen STAC')
    require(digest(path) == asset['file:checksum'][4:].lower(), 'Cached SHA256 differs from frozen STAC')


def is_transient_network_error(error):
    """Allow broken connections/timeouts, but never relax TLS verification.

    requests/urllib3 can wrap an SSL EOF in MaxRetryError.reason and args,
    without setting __cause__; inspect those links as well as Python chains.
    """
    pending, seen, errors = [error], set(), []
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        errors.append(current)
        pending.extend((current.__cause__, current.__context__, getattr(current, 'reason', None)))
        pending.extend(current.args)
    messages = [str(current).upper() for current in errors]
    if any(isinstance(current, ssl.SSLCertVerificationError) for current in errors) or any('CERTIFICATE_VERIFY_FAILED' in message or 'CERTIFICATE VERIFY FAILED' in message for message in messages):
        return False
    has_ssl = any(isinstance(current, (requests.exceptions.SSLError, ssl.SSLError)) or type(current).__name__ == 'SSLError' for current in errors)
    if has_ssl:
        return any(isinstance(current, ssl.SSLEOFError) for current in errors) or any('UNEXPECTED_EOF_WHILE_READING' in message or 'EOF OCCURRED IN VIOLATION OF PROTOCOL' in message for message in messages)
    return isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))


def download_asset(asset, path, cfg):
    """Bound every attempt by frozen size; optionally retry transient failures."""
    attempts = int(cfg.get('max_download_attempts', 3))
    transient_retries = int(cfg.get('transient_network_retries', 0))
    require(1 <= attempts <= 3 and 0 <= transient_retries <= len(TRANSIENT_RETRY_DELAYS), 'Invalid download retry limits')
    if path.exists():
        verify_asset_file(path, asset)
        return {'downloaded_bytes': 0, 'cache_hit': True}
    temporary = path.with_suffix('.tif.part')
    require(not temporary.is_symlink(), 'Linked partial asset')
    observed = 0
    for attempt in range(attempts + transient_retries):
        try:
            with requests.get(asset['href'], stream=True, timeout=(20, float(cfg.get('request_timeout_seconds', 120))), allow_redirects=False) as response:
                require(response.status_code == 200, f'Unexpected source HTTP {response.status_code}')
                if response.headers.get('Content-Length'):
                    require(int(response.headers['Content-Length']) == asset['file:size'], 'HTTP size differs from frozen STAC')
                count, sha = 0, hashlib.sha256()
                with temporary.open('wb') as stream:
                    for block in response.iter_content(1024 * 1024):
                        if not block:
                            continue
                        count += len(block)
                        observed += len(block)
                        require(count <= asset['file:size'], 'Source exceeded declared byte bound')
                        sha.update(block)
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                require(count == asset['file:size'], 'Truncated source asset')
                require(sha.hexdigest() == asset['file:checksum'][4:].lower(), 'Source SHA256 differs from frozen STAC')
                os.replace(temporary, path)
                return {'downloaded_bytes': observed, 'cache_hit': False}
        except Exception as exc:
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 4))
                continue
            extra = attempt + 1 - attempts
            if extra >= transient_retries or not is_transient_network_error(exc):
                raise
            delay = TRANSIENT_RETRY_DELAYS[extra]
            print(json.dumps({'utc': utc(), 'status': 'transient_network_retry', 'item_id': path.parent.name, 'asset': path.name, 'failed_attempt': attempt + 1, 'next_attempt': attempt + 2, 'additional_retry': extra + 1, 'delay_seconds': delay, 'error_class': type(exc).__name__}), flush=True)
            time.sleep(delay)
    raise AssertionError('unreachable')


def check_budget(output, cache, scene_bytes, args):
    cache_bytes = sum(path.stat().st_size for path in cache.rglob('*') if path.is_file())
    require(cache_bytes + scene_bytes <= args.max_cache_gb * 1e9, 'Cache bound would be exceeded')
    require(shutil.disk_usage(cache).free >= args.min_free_cache_gb * 1e9 + scene_bytes, 'Insufficient cache reserve')
    require(shutil.disk_usage(output).free >= args.min_free_output_gb * 1e9 + 1326164400, 'Insufficient output reserve')


def check_reserved_budget(output, cache, reservations, args):
    """Count each reserved scene once, including its already cached bytes.

    Only the main thread adds reservations. Downloads can rename .part files
    during this snapshot; full frozen scene reservations cover either name.
    Unreserved cache directories retain their actual byte cost.
    """
    other_bytes, reserved_present = 0, defaultdict(int)
    reserved_assets = {}
    for path in cache.rglob('*'):
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        scene_id = path.relative_to(cache).parts[0]
        if scene_id in reservations:
            # A directory walk can observe the .part name before an atomic
            # rename and the final name afterward. They are one reservation.
            asset = path.relative_to(cache).as_posix().removesuffix('.part')
            reserved_assets[(scene_id, asset)] = max(size, reserved_assets.get((scene_id, asset), 0))
        else:
            other_bytes += size
    for (scene_id, _), size in reserved_assets.items():
        reserved_present[scene_id] += size
    require(all(reserved_present[key] <= value for key, value in reservations.items()), 'Cached scene exceeds frozen reservation')
    require(other_bytes + sum(reservations.values()) <= args.max_cache_gb * 1e9, 'Cache bound would be exceeded')
    remaining = sum(max(0, value - reserved_present[key]) for key, value in reservations.items())
    require(shutil.disk_usage(cache).free >= args.min_free_cache_gb * 1e9 + remaining, 'Insufficient cache reserve')
    require(shutil.disk_usage(output).free >= args.min_free_output_gb * 1e9 + 1326164400, 'Insufficient output reserve')


def prepare_scene_cache(cache, item):
    scene_cache = safe_child(cache / item.id, cache)
    scene_cache.mkdir(exist_ok=True)
    names = {f'{band}.tif' for band in ASSETS} | {f'{band}.tif.part' for band in ASSETS}
    require(all(path.name in names and path.is_file() and not path.is_symlink() for path in scene_cache.iterdir()), 'Unexpected scene cache contents')
    for band in ASSETS:
        complete, partial = scene_cache / f'{band}.tif', scene_cache / f'{band}.tif.part'
        require(not (complete.exists() and partial.exists()), 'Duplicate complete/partial cached asset; HOLD')
        if complete.exists():
            require(complete.stat().st_size == item.assets[band]['file:size'], 'Cached size differs from frozen STAC')
        if partial.exists():
            require(partial.stat().st_size <= item.assets[band]['file:size'], 'Partial cache exceeds frozen STAC size')
    return scene_cache


def verify_pixels(item, cache, store, index):
    root = zarr.open_group(str(store), mode='r', use_consolidated=False)
    reports = []
    for group_name, bands in selected_native_groups(ASSETS).items():
        group = root[group_name]
        for band in bands:
            with rasterio.open(cache / f'{band}.tif') as source:
                target = group[variable_name(band)]
                require(source.crs.to_string() == group.attrs['crs'], 'Native CRS mismatch')
                require(np.allclose(list(source.transform)[:6], group.attrs['transform'], rtol=0, atol=1e-9), 'Native transform mismatch')
                require(source.dtypes[0] == 'uint16' and (source.height, source.width) == target.shape[1:], 'Native shape/dtype mismatch')
                size = min(64, source.height, source.width)
                positions = [(0, 0), ((source.height-size)//2, (source.width-size)//2), (source.height-size, source.width-size)]
                for row, col in positions:
                    expected = source.read(1, window=Window(col, row, size, size))
                    actual = np.asarray(target[index, row:row+size, col:col+size])
                    require(actual.dtype == expected.dtype and np.array_equal(actual, expected), 'Source/Zarr pixel mismatch')
                    reports.append({'asset': band, 'row': row, 'col': col, 'size': size, 'sha256': hashlib.sha256(expected.tobytes()).hexdigest()})
    return reports


def release_cache(scene_cache, cache_root, *, remaining_subset=False):
    safe_child(scene_cache, cache_root)
    expected = {f'{band}.tif' for band in ASSETS}
    paths = list(scene_cache.iterdir())
    names = {path.name for path in paths}
    require(names <= expected if remaining_subset else names == expected, 'Unexpected cache contents; keep for inspection')
    for path in paths:
        safe_child(path, cache_root)
        require(path.is_file() and not path.is_symlink(), 'Nonregular cache entry')
    freed = 0
    for path in paths:
        freed += path.stat().st_size
        path.unlink()
    scene_cache.rmdir()
    return freed


def run(args):
    cfg, features, per_tile = load_inputs(args.manifest, args.config)
    output, cache, state = Path(cfg['output_root']), Path(cfg['cache_root']), Path(args.state_dir)
    require(output.is_absolute() and cache.is_absolute() and state.is_absolute(), 'Absolute roots required')
    for root in (output, cache, state):
        require(not root.is_symlink(), 'Root symlink forbidden')
    require(len({root.resolve() for root in (output, cache, state)}) == 3, 'Roots must differ')
    require(not cache.resolve().is_relative_to(output.resolve()) and not output.resolve().is_relative_to(cache.resolve()), 'Output/cache nesting forbidden')
    state.mkdir(parents=True, exist_ok=True)
    binding = {'manifest_sha256': digest(args.manifest), 'config_sha256': digest(args.config), 'output_root': str(output.resolve()), 'cache_root': str(cache.resolve())}
    require(output.parent.is_dir(), 'Output parent must already exist')
    output_lock = output.parent / ('.' + output.name + '.selected.lock')
    with InterProcessLock(output_lock, timeout=1), InterProcessLock(state / 'task.lock', timeout=1):
        owner = state / 'binding.json'
        if owner.exists():
            require(json.loads(owner.read_text(encoding='utf8')) == binding, 'Frozen manifest/config/root binding changed')
        else:
            require(not output.exists() or not any(output.iterdir()), 'Unbound nonempty output; HOLD')
            require(not cache.exists() or not any(cache.iterdir()), 'Unbound nonempty cache; HOLD')
            atomic_json(owner, binding)
        output.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        # Validate every existing tile before any HTTP request or append.
        committed = {tile: verify_store(output, tile, expected, check_catalog=False) for tile, expected in per_tile.items()}
        allowed_tiles = set(per_tile)
        require(not any(path.is_dir() and path.name not in allowed_tiles for path in output.iterdir()), 'Unknown tile/output directory')
        # A crash can occur after the writer's atomic commit but before catalog
        # update. Only the final scheduled committed scene may lack acceptance.
        committed_ids = {entry['item_id'] for entries in committed.values() for entry in entries}
        committed_features = [feature for feature in features if feature['id'] in committed_ids]
        incomplete = [feature for feature in committed_features if not (state / 'accepted' / (feature['id'] + '.json')).exists()]
        require(len(incomplete) <= 1 and (not incomplete or incomplete[0]['id'] == committed_features[-1]['id']), 'Multiple/non-final committed scenes lack acceptance; HOLD')
        total_new, total_skip, downloaded_bytes = 0, 0, 0
        current_id = None
        started = utc()
        prefetch = int(getattr(args, 'prefetch_scenes', 0))
        write_workers = int(getattr(args, 'write_workers', 1))
        download_override = getattr(args, 'download_workers', None)
        download_workers = int(cfg.get('download_workers', 4) if download_override is None else download_override)
        transient_retries = int(getattr(args, 'transient_network_retries', 0))
        stop_file = getattr(args, 'stop_file', None)
        require(prefetch in (0, 1) and write_workers in (1, 2, 3, 4) and 1 <= download_workers <= 8, 'Invalid runtime concurrency')
        require(0 <= transient_retries <= len(TRANSIENT_RETRY_DELAYS), 'Invalid runtime transient network retry count')
        download_cfg = {**cfg, 'transient_network_retries': transient_retries}
        runtime = {'prefetch_scenes': prefetch, 'download_workers': download_workers, 'write_workers': write_workers, 'transient_network_retries': transient_retries, 'runner_sha256': digest(__file__), 'stop_file': str(stop_file) if stop_file else None}
        atomic_json(state / 'runtime.json', {'started_utc': started, **runtime})
        pending = [feature for feature in features if not (state / 'accepted' / (feature['id'] + '.json')).exists()]
        if args.max_new_scenes is not None:
            pending = pending[:args.max_new_scenes]
        next_pending = {feature['id']: pending[i + 1] for i, feature in enumerate(pending[:-1])}
        downloads, reservations = {}, {}
        pool = ThreadPoolExecutor(max_workers=runtime['download_workers'])
        stopped = False

        def stopped_requested():
            return stop_file is not None and Path(stop_file).exists()

        def stage(status, order, **extra):
            atomic_json(state / 'progress.json', {'started_utc': started, 'updated_utc': utc(), 'status': status, 'current_id': current_id, 'schedule_index': order, 'accepted_this_run': total_new, 'skipped_this_run': total_skip, 'manifest_count': len(features), 'runtime': runtime, 'prefetch_ids': [key for key in downloads if key != current_id], **extra})

        def submit_download(feature):
            item = feature_to_item(feature)
            require(item.id not in downloads, 'Duplicate concurrent scene download')
            scene_cache = prepare_scene_cache(cache, item)
            reservation = sum(item.assets[band]['file:size'] for band in ASSETS)
            proposed = {**reservations, item.id: reservation}
            check_reserved_budget(output, cache, proposed, args)
            reservations[item.id] = reservation
            downloads[item.id] = [pool.submit(download_asset, item.assets[band], scene_cache / f'{band}.tif', download_cfg) for band in ASSETS]

        def finish_download_pool(*, cancel):
            # Joining is mandatory before returning, including exceptions. No
            # background download may outlive the task/output ownership locks.
            nonlocal downloaded_bytes
            pool.shutdown(wait=True, cancel_futures=cancel)
            errors = []
            for scene_id, futures in downloads.items():
                for future in futures:
                    if future.cancelled():
                        continue
                    try:
                        downloaded_bytes += future.result()['downloaded_bytes']
                    except Exception as exc:
                        errors.append({'item_id': scene_id, 'error': f'{type(exc).__name__}: {exc}'})
            downloads.clear()
            return errors

        try:
            for order, feature in enumerate(features):
                item = feature_to_item(feature)
                current_id = item.id
                receipt_path = state / 'accepted' / f'{item.id}.json'
                feature_hash = canonical_digest(feature)
                entries = committed[item.tile_id]
                index = next((i for i, f in enumerate(per_tile[item.tile_id]) if f['id'] == item.id), None)
                if receipt_path.exists():
                    receipt = json.loads(receipt_path.read_text(encoding='utf8'))
                    require(receipt.get('binding') == binding and receipt.get('feature_sha256') == feature_hash and receipt.get('accepted') is True, 'Invalid acceptance receipt')
                    require(index < len(entries), 'Receipt has no committed scene')
                    verify_store(output, item.tile_id, per_tile[item.tile_id], check_catalog=True)
                    global_rows = read_catalog(output / 'catalog.parquet')
                    tile_rows = read_catalog(output / item.tile_id / 'catalog.parquet')
                    require([row for row in global_rows if row['tile_id'] == item.tile_id] == tile_rows, 'Global/tile Catalog mismatch before skip')
                    scene_cache = safe_child(cache / item.id, cache)
                    if scene_cache.exists():
                        leftovers = list(scene_cache.iterdir())
                        require({path.name for path in leftovers} <= {f'{band}.tif' for band in ASSETS}, 'Unexpected accepted scene cache contents')
                        for path in leftovers:
                            safe_child(path, cache)
                            verify_asset_file(path, item.assets[path.stem])
                        if not getattr(args, 'verify_only', False):
                            freed = release_cache(scene_cache, cache, remaining_subset=True)
                            receipt['released_cache_bytes'] = receipt.get('released_cache_bytes', 0) + freed
                            receipt['cache_released'] = True
                            receipt['cache_reconciled_utc'] = utc()
                            atomic_json(receipt_path, receipt)
                    elif not receipt.get('cache_released') and not getattr(args, 'verify_only', False):
                        receipt['cache_released'] = True
                        receipt['cache_reconciled_utc'] = utc()
                        atomic_json(receipt_path, receipt)
                    total_skip += 1
                    continue
                if getattr(args, 'verify_only', False):
                    require(index >= len(entries), 'Committed scene lacks acceptance receipt during verification')
                    continue
                if args.max_new_scenes is not None and total_new >= args.max_new_scenes:
                    break
                if stopped_requested():
                    stopped = True
                    break
                scene_started = time.perf_counter()
                require(index <= len(entries), 'Non-prefix pending scene; HOLD')
                scene_cache = safe_child(cache / item.id, cache)
                wait_started = time.perf_counter()
                if item.id not in downloads:
                    submit_download(feature)
                stage('downloading', order)
                results = [future.result() for future in downloads[item.id]]
                downloaded_bytes += sum(result['downloaded_bytes'] for result in results)
                del downloads[item.id]
                timings = {'download_wait_seconds': time.perf_counter() - wait_started}
                # Every cached COG is verified before the writer can read it.
                stage('verifying_sources', order)
                verify_started = time.perf_counter()
                for band in ASSETS:
                    verify_asset_file(scene_cache / f'{band}.tif', item.assets[band])
                timings['source_verify_seconds'] = time.perf_counter() - verify_started
                # Only downloads look ahead. Append, catalog, receipt and cache
                # release remain in frozen schedule order under one committer.
                if prefetch and item.id in next_pending and not stopped_requested():
                    submit_download(next_pending[item.id])
                store = store_path(output, item.tile_id)
                was_committed = index < len(entries)
                stage('writing', order)
                write_started = time.perf_counter()
                if not was_committed:
                    # This runner already verified each local COG against its frozen SHA-256.
                    local_item = replace(item, assets={band: {**item.assets[band], 'href': str(scene_cache / f'{band}.tif')} for band in ASSETS})
                    written = append_item_to_native_zarr(item=local_item, bands=ASSETS, zarr_path=store, chunk_size=512, compression=COMPRESSION, read_mode='remote_stream_first', lock_dir=state / 'locks', write_workers=write_workers)
                    require(written, 'New scene unexpectedly skipped by writer')
                timings['write_seconds'] = time.perf_counter() - write_started
                stage('verifying_catalog', order)
                verify_started = time.perf_counter()
                verify_store(output, item.tile_id, per_tile[item.tile_id], check_catalog=False)
                pixels = verify_pixels(item, scene_cache, store, index)
                records = _catalog_records(item=item, band_groups=selected_native_groups(ASSETS), zarr_path=store, preview_path=None, output_root=output, stac_api='https://earth-search.aws.element84.com/v1', provider='Element 84 Earth Search / AWS Open Data')
                upsert_records(output / item.tile_id / 'catalog.parquet', records, lock_path=state / 'locks/catalog' / f'{item.tile_id}.lock')
                resync_global_catalog(output, lock_path=state / 'locks/catalog/global.lock')
                committed[item.tile_id] = verify_store(output, item.tile_id, per_tile[item.tile_id], check_catalog=True)
                global_rows = read_catalog(output / 'catalog.parquet')
                tile_rows = read_catalog(output / item.tile_id / 'catalog.parquet')
                require([row for row in global_rows if row['tile_id'] == item.tile_id] == tile_rows, 'Global/tile Catalog mismatch')
                timings['verify_catalog_seconds'] = time.perf_counter() - verify_started
                receipt = {'accepted': True, 'accepted_utc': utc(), 'binding': binding, 'feature_sha256': feature_hash, 'item_id': item.id, 'tile_id': item.tile_id, 'datetime': item.datetime, 'index': index, 'recovered_committed_scene': was_committed, 'source_assets_verified': ASSETS, 'pixel_windows': pixels, 'selection_quality': feature['selection_quality'], 'scl_reference': feature['assets'].get('scl'), 'source_feature': feature, 'cache_released': False, 'runtime': runtime, 'timings': timings}
                atomic_json(receipt_path, receipt)
                receipt['released_cache_bytes'] = release_cache(scene_cache, cache)
                receipt['cache_released'] = True
                reservations.pop(item.id)
                timings['total_seconds'] = time.perf_counter() - scene_started
                atomic_json(receipt_path, receipt)
                total_new += 1
                stage('accepted', order, downloaded_bytes_this_run=downloaded_bytes, timings=timings)
                print(json.dumps({'utc': utc(), 'status': 'accepted', 'item_id': item.id, 'accepted_this_run': total_new, 'skipped_this_run': total_skip, 'downloaded_bytes_this_run': downloaded_bytes, 'runtime': runtime, 'timings': timings}), flush=True)
            pending_errors = finish_download_pool(cancel=False)
            require(not pending_errors or stopped, 'Prefetch failed: ' + str(pending_errors))
            summary = {'started_utc': started, 'finished_utc': utc(), 'status': 'verification_complete' if getattr(args, 'verify_only', False) else ('stopped_at_boundary' if stopped else 'batch_complete'), 'accepted_this_run': total_new, 'skipped_this_run': total_skip, 'manifest_count': len(features), 'downloaded_bytes_this_run': downloaded_bytes, 'all_manifest_accepted': total_new + total_skip == len(features), 'binding': binding, 'runtime': runtime, 'pending_download_errors': pending_errors}
            atomic_json(state / 'last_run.json', summary)
            if stopped:
                atomic_json(state / 'progress.json', summary)
            print(json.dumps(summary, ensure_ascii=False))
            return summary
        except Exception as exc:
            pending_errors = finish_download_pool(cancel=True)
            failure = {'started_utc': started, 'failed_utc': utc(), 'status': 'held', 'current_id': current_id, 'accepted_this_run': total_new, 'skipped_this_run': total_skip, 'error': f'{type(exc).__name__}: {exc}', 'binding': binding, 'runtime': runtime, 'pending_download_errors': pending_errors}
            atomic_json(state / 'failures' / f'{time.time_ns()}.json', failure)
            atomic_json(state / 'progress.json', failure)
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--max-new-scenes', type=int)
    parser.add_argument('--transient-network-retries', type=int, choices=range(7), default=0, help='Extra bounded retries for SSL EOF, connection failures or timeouts after ordinary attempts')
    parser.add_argument('--verify-only', action='store_true', help='Validate existing committed prefix and receipts; zero downloads and no new scenes')
    parser.add_argument('--prefetch-scenes', type=int, choices=(0, 1), default=0, help='Download one next scene while the current scene is written')
    parser.add_argument('--download-workers', type=int, choices=range(1, 9), default=None, help='Runtime asset download threads; default preserves the frozen config value')
    parser.add_argument('--write-workers', type=int, choices=(1, 2, 3, 4), default=1, help='Distinct band writers within the sole scene commit')
    parser.add_argument('--stop-file', type=Path, help='Stop before starting another scene when this file exists; finish in-flight downloads')
    parser.add_argument('--min-free-output-gb', type=float, default=500)
    parser.add_argument('--min-free-cache-gb', type=float, default=50)
    parser.add_argument('--max-cache-gb', type=float, default=8)
    args = parser.parse_args()
    require(args.max_new_scenes is None or args.max_new_scenes >= 0, 'Nonnegative max-new-scenes required')
    require(args.min_free_output_gb >= 0 and args.min_free_cache_gb >= 0 and args.max_cache_gb > 0, 'Invalid capacity limits')
    try:
        run(args)
    except Exception as exc:
        print(f'ARCHIVE HELD: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
