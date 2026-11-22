from __future__ import annotations

from argparse import Namespace
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import ssl
import sys
import threading

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
import zarr
from urllib3.exceptions import MaxRetryError, SSLError as Urllib3SSLError

SPEC = importlib.util.spec_from_file_location('selected_archive_runner', Path(__file__).parents[1] / 'tools/run_selected_archive.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture
def job(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    features = []
    for scene in range(2):
        assets = {}
        for n, band in enumerate(runner.ASSETS):
            resolution = 10 if n < 4 else 20
            size = 120 // resolution
            path = source / f'{scene}_{band}.tif'
            values = np.arange(size * size, dtype='uint16').reshape(size, size) + n + scene
            transform = from_origin(500000, 9600040, resolution, resolution)
            with rasterio.open(path, 'w', driver='GTiff', width=size, height=size, count=1, dtype='uint16', crs='EPSG:32717', transform=transform, nodata=0) as dataset:
                dataset.write(values, 1)
            assets[band] = {'href': f'https://e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com/{scene}_{band}.tif', 'file:size': path.stat().st_size, 'file:checksum': '1220' + runner.digest(path), 'raster:bands': [{'data_type': 'uint16', 'nodata': 0, 'scale': .0001, 'offset': -.1}], 'gsd': resolution, 'proj:shape': [size, size], 'proj:transform': list(transform)[:6], 'proj:epsg': 32717}
        features.append({'type': 'Feature', 'id': f'scene_{scene}', 'collection': 'sentinel-2-c1-l2a', 'properties': {'datetime': f'2018-0{scene + 1}-01T12:00:00.123Z', 'grid:code': 'MGRS-17MNR', 'proj:epsg': 32717}, 'assets': assets, 'geometry': None, 'selection_quality': {'valid_fraction': 1, 'clear_fraction_total': .9}})
    manifest = tmp_path / 'features.jsonl'
    manifest.write_text(''.join(json.dumps(feature) + '\n' for feature in features), encoding='utf8')
    cfg = {'output_root': str(tmp_path / 'output'), 'cache_root': str(tmp_path / 'cache'), 'assets': runner.ASSETS, 'compression': runner.COMPRESSION, 'download_workers': 4, 'max_download_attempts': 1}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(cfg), encoding='utf8')
    args = Namespace(manifest=manifest, config=config, state_dir=tmp_path / 'state', max_new_scenes=None, verify_only=False, min_free_output_gb=0, min_free_cache_gb=0, max_cache_gb=8)
    calls = []

    def copy_download(asset, path, cfg):
        calls.append(asset['href'])
        shutil.copyfile(source / asset['href'].rsplit('/', 1)[-1], path)
        return {'downloaded_bytes': path.stat().st_size, 'cache_hit': False}

    monkeypatch.setattr(runner, 'download_asset', copy_download)
    return args, features, cfg, calls


def test_archive_and_repeat_skips_without_http(job, monkeypatch):
    args, features, cfg, calls = job
    from s2grits import zarr_writer
    def no_second_cache(*args, **kwargs):
        pytest.fail('Frozen source already verified; writer must read that local COG directly')
    monkeypatch.setattr(zarr_writer, 'cache_asset', no_second_cache)
    first = runner.run(args)
    assert first['accepted_this_run'] == 2 and len(calls) == 20
    assert list(Path(cfg['cache_root']).iterdir()) == []

    def forbidden(*args, **kwargs):
        pytest.fail('Committed scene attempted HTTP')

    monkeypatch.setattr(runner, 'download_asset', forbidden)
    second = runner.run(args)
    assert second['skipped_this_run'] == 2
    assert second['accepted_this_run'] == second['downloaded_bytes_this_run'] == 0
    assert second['all_manifest_accepted']


def test_first_scene_failure_prevents_later_append(job, monkeypatch):
    args, features, cfg, calls = job

    def failure(asset, path, cfg):
        calls.append(asset['href'])
        raise RuntimeError('injected download failure')

    monkeypatch.setattr(runner, 'download_asset', failure)
    with pytest.raises(RuntimeError, match='injected'):
        runner.run(args)
    assert all('/0_' in href for href in calls)
    assert not runner.store_path(cfg['output_root'], '17MNR').exists()
    assert len(list((args.state_dir / 'failures').glob('*.json'))) == 1


def test_pilot_then_verify_only_does_not_start_second_scene(job, monkeypatch):
    args, features, cfg, calls = job
    args.max_new_scenes = 1
    runner.run(args)
    calls.clear()
    args.verify_only = True
    summary = runner.run(args)
    assert summary['status'] == 'verification_complete'
    assert summary['skipped_this_run'] == 1 and not calls
    assert summary['downloaded_bytes_this_run'] == 0


@pytest.mark.parametrize('corruption', ['missing_sidecar', 'array_tail', 'missing_catalog'])
def test_unknown_existing_state_held_before_http(job, monkeypatch, corruption):
    args, features, cfg, calls = job
    args.max_new_scenes = 1
    runner.run(args)
    store = runner.store_path(cfg['output_root'], '17MNR')
    if corruption == 'missing_sidecar':
        (store / 's2grits_resume_state.json').unlink()
    elif corruption == 'array_tail':
        root = zarr.open_group(str(store), mode='a')
        array = root['10m']['red']
        array.resize((2, *array.shape[1:]))
    else:
        (Path(cfg['output_root']) / '17MNR/catalog.parquet').unlink()
    calls.clear()
    with pytest.raises(RuntimeError):
        runner.run(args)
    assert not calls


def test_budget_stops_before_http(job):
    args, features, cfg, calls = job
    args.max_cache_gb = .000000001
    with pytest.raises(RuntimeError, match='Cache bound'):
        runner.run(args)
    assert not calls


def test_committed_scene_missing_receipt_recovers_without_append(job, monkeypatch):
    args, features, cfg, calls = job
    args.max_new_scenes = 1
    runner.run(args)
    receipt = args.state_dir / 'accepted/scene_0.json'
    receipt.unlink()

    def forbidden(**kwargs):
        pytest.fail('Recovery attempted duplicate append')

    monkeypatch.setattr(runner, 'append_item_to_native_zarr', forbidden)
    summary = runner.run(args)
    assert summary['accepted_this_run'] == 1
    assert json.loads(receipt.read_text())['recovered_committed_scene']


def test_manifest_reorder_rejected(job):
    args, features, cfg, calls = job
    args.manifest.write_text(''.join(json.dumps(feature) + '\n' for feature in reversed(features)), encoding='utf8')
    with pytest.raises(RuntimeError, match='strictly increase'):
        runner.run(args)
    assert not calls


def test_config_binding_change_rejected(job):
    args, features, cfg, calls = job
    args.max_new_scenes = 1
    runner.run(args)
    cfg['download_workers'] = 3
    args.config.write_text(json.dumps(cfg), encoding='utf8')
    calls.clear()
    with pytest.raises(RuntimeError, match='binding changed'):
        runner.run(args)
    assert not calls


def test_download_rejects_digest_mismatch(tmp_path, monkeypatch):
    asset = {'href': 'https://example.test/a.tif', 'file:size': 3, 'file:checksum': '1220' + hashlib.sha256(b'abc').hexdigest()}

    class Response:
        status_code = 200
        headers = {'Content-Length': '3'}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def iter_content(self, _):
            yield b'bad'

    monkeypatch.setattr(runner.requests, 'get', lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match='SHA256'):
        runner.download_asset(asset, tmp_path / 'red.tif', {'max_download_attempts': 1})
    assert not (tmp_path / 'red.tif').exists()


def test_different_state_directory_uses_same_output_lock(job, monkeypatch):
    args, features, cfg, calls = job
    lock = Path(cfg['output_root']).parent / ('.' + Path(cfg['output_root']).name + '.selected.lock')
    with runner.InterProcessLock(lock, timeout=.1):
        with pytest.raises(TimeoutError):
            runner.run(args)
    assert not calls
    assert not (args.state_dir / 'binding.json').exists()


def test_cleanup_interruption_resumes_without_http(job, monkeypatch):
    args, features, cfg, calls = job
    args.max_new_scenes = 1
    release = runner.release_cache

    def interrupted(scene_cache, cache_root, **kwargs):
        (scene_cache / 'red.tif').unlink()
        raise OSError('injected cleanup interruption')

    monkeypatch.setattr(runner, 'release_cache', interrupted)
    with pytest.raises(OSError, match='cleanup interruption'):
        runner.run(args)
    cache = Path(cfg['cache_root']) / 'scene_0'
    assert len(list(cache.iterdir())) == 9
    monkeypatch.setattr(runner, 'release_cache', release)
    calls.clear()
    args.verify_only = True
    runner.run(args)
    assert len(list(cache.iterdir())) == 9 and not calls
    args.verify_only = False
    args.max_new_scenes = 0
    summary = runner.run(args)
    assert summary['skipped_this_run'] == 1 and not calls
    assert not cache.exists()
    assert json.loads((args.state_dir / 'accepted/scene_0.json').read_text())['cache_released']


def test_prefetch_overlaps_write_with_only_one_future_scene(job, monkeypatch):
    args, features, cfg, calls = job
    args.prefetch_scenes = 1
    third = json.loads(json.dumps(features[1]))
    third['id'] = 'scene_2'
    third['properties']['datetime'] = '2018-03-01T12:00:00.123Z'
    features.append(third)
    args.manifest.write_text(''.join(json.dumps(feature) + '\n' for feature in features), encoding='utf8')
    download, append = runner.download_asset, runner.append_item_to_native_zarr
    next_started = threading.Event()
    downloaded_scenes, appended = set(), []

    def observed_download(asset, path, cfg):
        downloaded_scenes.add(path.parent.name)
        if path.parent.name == 'scene_1':
            next_started.set()
        return download(asset, path, cfg)

    def observed_append(**kwargs):
        scene_id = kwargs['item'].id
        if scene_id == 'scene_0':
            assert next_started.wait(5), 'Next scene did not download during first writer'
            assert 'scene_2' not in downloaded_scenes
        if scene_id != 'scene_0':
            previous = f'scene_{int(scene_id[-1]) - 1}'
            assert json.loads((args.state_dir / 'accepted' / (previous + '.json')).read_text())['accepted']
        appended.append(scene_id)
        return append(**kwargs)

    monkeypatch.setattr(runner, 'download_asset', observed_download)
    monkeypatch.setattr(runner, 'append_item_to_native_zarr', observed_append)
    result = runner.run(args)
    assert appended == ['scene_0', 'scene_1', 'scene_2']
    assert result['accepted_this_run'] == 3 and result['all_manifest_accepted']
    assert not list(Path(cfg['cache_root']).iterdir())
    receipt = json.loads((args.state_dir / 'accepted/scene_0.json').read_text())
    assert receipt['runtime']['prefetch_scenes'] == 1
    assert set(receipt['timings']) == {'download_wait_seconds', 'source_verify_seconds', 'write_seconds', 'verify_catalog_seconds', 'total_seconds'}
    assert all(value >= 0 for value in receipt['timings'].values())


def test_failed_prefetch_keeps_current_acceptance_and_holds_next(job, monkeypatch):
    args, features, cfg, calls = job
    args.prefetch_scenes = 1
    download = runner.download_asset

    def fail_next(asset, path, cfg):
        if path.parent.name == 'scene_1':
            raise RuntimeError('injected next-scene download failure')
        return download(asset, path, cfg)

    monkeypatch.setattr(runner, 'download_asset', fail_next)
    with pytest.raises(RuntimeError, match='next-scene'):
        runner.run(args)
    assert json.loads((args.state_dir / 'accepted/scene_0.json').read_text())['accepted']
    assert not (args.state_dir / 'accepted/scene_1.json').exists()
    entries = runner.verify_store(cfg['output_root'], '17MNR', features)
    assert [entry['item_id'] for entry in entries] == ['scene_0']


def test_write_failure_joins_prefetch_before_return(job, monkeypatch):
    args, features, cfg, calls = job
    args.prefetch_scenes = 1
    download = runner.download_asset
    started, release = threading.Event(), threading.Event()
    active = []
    task_errors = []

    def blocked_prefetch(asset, path, cfg):
        if path.parent.name == 'scene_1':
            active.append(path.name)
            started.set()
            assert release.wait(10)
            try:
                return download(asset, path, cfg)
            finally:
                active.remove(path.name)
        return download(asset, path, cfg)

    def fail_write(**kwargs):
        assert started.wait(5)
        raise RuntimeError('injected writer failure')

    def execute():
        try:
            runner.run(args)
        except Exception as exc:
            task_errors.append(exc)

    monkeypatch.setattr(runner, 'download_asset', blocked_prefetch)
    monkeypatch.setattr(runner, 'append_item_to_native_zarr', fail_write)
    task = threading.Thread(target=execute)
    task.start()
    try:
        assert started.wait(5)
        task.join(.1)
        assert task.is_alive(), 'Runner returned while prefetch workers still active'
    finally:
        release.set()
        task.join(15)
    assert not task.is_alive() and not active
    assert len(task_errors) == 1 and 'writer failure' in str(task_errors[0])
    assert not (args.state_dir / 'accepted').exists()


def test_stop_at_boundary_retains_prefetch_and_resumes_binding(job, monkeypatch):
    args, features, cfg, calls = job
    args.prefetch_scenes = 1
    args.stop_file = args.state_dir / 'stop.request'
    append = runner.append_item_to_native_zarr
    download = runner.download_asset

    def request_stop(**kwargs):
        result = append(**kwargs)
        args.stop_file.touch()
        return result

    monkeypatch.setattr(runner, 'append_item_to_native_zarr', request_stop)
    first = runner.run(args)
    assert first['status'] == 'stopped_at_boundary' and first['accepted_this_run'] == 1
    binding = (args.state_dir / 'binding.json').read_bytes()
    assert len(list((Path(cfg['cache_root']) / 'scene_1').glob('*.tif'))) == 10
    args.stop_file.unlink()
    args.prefetch_scenes = 0
    monkeypatch.setattr(runner, 'append_item_to_native_zarr', append)

    def require_cache(asset, path, cfg):
        assert path.is_file(), 'Verified prefetched cache was lost'
        runner.verify_asset_file(path, asset)
        return {'downloaded_bytes': 0, 'cache_hit': True}

    monkeypatch.setattr(runner, 'download_asset', require_cache)
    second = runner.run(args)
    assert second['accepted_this_run'] == 1 and second['skipped_this_run'] == 1
    assert second['downloaded_bytes_this_run'] == 0
    assert (args.state_dir / 'binding.json').read_bytes() == binding


def test_prefetch_respects_pilot_and_verification_scope(job):
    args, features, cfg, calls = job
    args.prefetch_scenes = 1
    args.max_new_scenes = 1
    runner.run(args)
    assert len(calls) == 10 and all('/0_' in href for href in calls)
    assert not (Path(cfg['cache_root']) / 'scene_1').exists()
    calls.clear()
    args.verify_only = True
    runner.run(args)
    assert not calls


def test_reserved_budget_does_not_double_count_cached_bytes(tmp_path):
    cache, output = tmp_path / 'cache', tmp_path / 'output'
    (cache / 'scene_0').mkdir(parents=True)
    output.mkdir()
    (cache / 'scene_0' / 'red.tif').write_bytes(b'a' * 80)
    args = Namespace(max_cache_gb=200 / 1e9, min_free_cache_gb=0, min_free_output_gb=0)
    runner.check_reserved_budget(output, cache, {'scene_0': 100, 'scene_1': 100}, args)
    (cache / 'unreserved').mkdir()
    (cache / 'unreserved' / 'extra.tif').write_bytes(b'x')
    with pytest.raises(RuntimeError, match='Cache bound'):
        runner.check_reserved_budget(output, cache, {'scene_0': 100, 'scene_1': 100}, args)


def test_existing_stop_file_starts_no_download(job):
    args, features, cfg, calls = job
    args.prefetch_scenes = 1
    args.stop_file = args.config.parent / 'stop.request'
    args.stop_file.touch()
    summary = runner.run(args)
    assert summary['status'] == 'stopped_at_boundary'
    assert not calls and not (args.state_dir / 'accepted').exists()


@pytest.mark.parametrize('workers', [1, 8])
def test_runtime_download_override_enforces_peak_and_preserves_config(job, monkeypatch, workers):
    args, features, cfg, calls = job
    args.download_workers = workers
    args.max_new_scenes = 1
    config_before = args.config.read_bytes()
    download = runner.download_asset
    first_wave = threading.Barrier(workers)
    mutex = threading.Lock()
    active = peak = started = 0

    def observed_download(asset, path, cfg):
        nonlocal active, peak, started
        with mutex:
            active += 1
            started += 1
            launch = started
            peak = max(peak, active)
        try:
            if launch <= workers:
                first_wave.wait(timeout=5)
            return download(asset, path, cfg)
        finally:
            with mutex:
                active -= 1

    monkeypatch.setattr(runner, 'download_asset', observed_download)
    summary = runner.run(args)
    assert peak == workers and active == 0 and started == 10
    assert summary['runtime']['download_workers'] == workers
    receipt = json.loads((args.state_dir / 'accepted/scene_0.json').read_text())
    assert receipt['runtime']['download_workers'] == workers
    assert args.config.read_bytes() == config_before
    assert json.loads(config_before)['download_workers'] == 4
    assert receipt['binding']['config_sha256'] == hashlib.sha256(config_before).hexdigest()


def test_runtime_download_override_rejects_unbounded_pool(job):
    args, features, cfg, calls = job
    args.download_workers = 9
    with pytest.raises(RuntimeError, match='Invalid runtime concurrency'):
        runner.run(args)
    assert not calls


class DownloadResponse:
    status_code = 200
    headers = {'Content-Length': '3'}

    def __init__(self, payload=b'abc', error=None):
        self.payload, self.error = payload, error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, _):
        yield self.payload
        if self.error:
            raise self.error


def ssl_eof():
    eof = ssl.SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol')
    return runner.requests.exceptions.SSLError(MaxRetryError(None, '/test.tif', reason=Urllib3SSLError(eof)))


@pytest.fixture
def retry_download(tmp_path, monkeypatch):
    asset = {'href': 'https://example.test/a.tif?private=not-for-log', 'file:size': 3, 'file:checksum': '1220' + hashlib.sha256(b'abc').hexdigest()}
    sleeps = []
    monkeypatch.setattr(runner.time, 'sleep', sleeps.append)
    return asset, tmp_path / 'red.tif', {'max_download_attempts': 3, 'transient_network_retries': 6}, sleeps


def test_ssl_eof_recovers_with_bounded_retry_and_counts_partial_bytes(retry_download, monkeypatch, capsys):
    asset, path, cfg, sleeps = retry_download
    responses = iter([DownloadResponse(b'a', ssl_eof()) for _ in range(3)] + [DownloadResponse()])
    monkeypatch.setattr(runner.requests, 'get', lambda *args, **kwargs: next(responses))
    result = runner.download_asset(asset, path, cfg)
    assert result == {'downloaded_bytes': 6, 'cache_hit': False}
    assert path.read_bytes() == b'abc'
    assert sleeps == [1, 2, 30]
    output = capsys.readouterr().out
    event = json.loads(output)
    assert event['status'] == 'transient_network_retry'
    assert event['item_id'] == path.parent.name and event['asset'] == 'red.tif'
    assert event['failed_attempt'] == 3 and event['next_attempt'] == 4
    assert event['error_class'] == 'SSLError'
    assert 'example.test' not in output and 'private' not in output


@pytest.mark.parametrize('error_factory', [ssl_eof, lambda: runner.requests.exceptions.Timeout('read timeout'), lambda: runner.requests.exceptions.ConnectionError('peer reset')])
def test_transient_retry_exhaustion_is_bounded(retry_download, monkeypatch, error_factory):
    asset, path, cfg, sleeps = retry_download
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise error_factory()

    monkeypatch.setattr(runner.requests, 'get', fail)
    with pytest.raises(runner.requests.exceptions.RequestException):
        runner.download_asset(asset, path, cfg)
    assert len(attempts) == 9
    assert sleeps == [1, 2, 30, 60, 120, 240, 300, 300]
    assert not path.exists()


@pytest.mark.parametrize('error', [runner.requests.exceptions.SSLError(ssl.SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED')), runner.requests.exceptions.SSLError('TLS unknown error')])
def test_tls_verification_and_unknown_ssl_do_not_get_extended_retry(retry_download, monkeypatch, error):
    asset, path, cfg, sleeps = retry_download
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise error

    monkeypatch.setattr(runner.requests, 'get', fail)
    with pytest.raises(runner.requests.exceptions.SSLError):
        runner.download_asset(asset, path, cfg)
    assert len(attempts) == 3 and sleeps == [1, 2]


@pytest.mark.parametrize('failure', ['checksum', 'http', 'size'])
def test_data_contract_errors_do_not_get_extended_retry(retry_download, monkeypatch, failure):
    asset, path, cfg, sleeps = retry_download
    responses = []

    def response(*args, **kwargs):
        value = DownloadResponse(b'bad' if failure == 'checksum' else b'abc')
        if failure == 'http':
            value.status_code = 503
        if failure == 'size':
            value.headers = {'Content-Length': '99'}
        responses.append(value)
        return value

    monkeypatch.setattr(runner.requests, 'get', response)
    with pytest.raises(RuntimeError):
        runner.download_asset(asset, path, cfg)
    assert len(responses) == 3 and sleeps == [1, 2]
    assert not path.exists()


def test_nontransient_failure_during_extra_retry_holds_immediately(retry_download, monkeypatch):
    asset, path, cfg, sleeps = retry_download
    responses = iter([DownloadResponse(b'a', ssl_eof()) for _ in range(3)] + [DownloadResponse(b'bad')])
    monkeypatch.setattr(runner.requests, 'get', lambda *args, **kwargs: next(responses))
    with pytest.raises(RuntimeError, match='SHA256'):
        runner.download_asset(asset, path, cfg)
    assert sleeps == [1, 2, 30]


def test_runtime_transient_retry_preserves_frozen_config_and_resume_binding(job, monkeypatch):
    args, features, cfg, calls = job
    args.max_new_scenes = 1
    config_before = args.config.read_bytes()
    runner.run(args)
    binding_before = (args.state_dir / 'binding.json').read_bytes()
    args.transient_network_retries = 6
    download = runner.download_asset
    seen = []

    def observe(asset, path, download_cfg):
        seen.append(download_cfg['transient_network_retries'])
        return download(asset, path, download_cfg)

    monkeypatch.setattr(runner, 'download_asset', observe)
    result = runner.run(args)
    assert result['skipped_this_run'] == 1 and result['accepted_this_run'] == 1
    assert seen == [6] * 10
    assert result['runtime']['transient_network_retries'] == 6
    assert args.config.read_bytes() == config_before
    assert (args.state_dir / 'binding.json').read_bytes() == binding_before
    assert 'transient_network_retries' not in cfg


@pytest.mark.parametrize('count', [-1, 7])
def test_runtime_transient_retry_rejects_unbounded_count(job, count):
    args, features, cfg, calls = job
    args.transient_network_retries = count
    with pytest.raises(RuntimeError, match='Invalid runtime transient'):
        runner.run(args)
    assert not calls
