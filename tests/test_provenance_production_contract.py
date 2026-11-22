"""Independent recovery-contract checks using the real tiny native writer."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_provenance_native_writer import item as _item_fixture

from s2grits import provenance as publication
from s2grits import provenance_production as production
from s2grits import zarr_writer as writer
from s2grits.catalog import read_catalog


@pytest.fixture
def case(tmp_path, monkeypatch):
    item = _item_fixture.__wrapped__(tmp_path)
    local = {band: Path(asset['href']) for band, asset in item.assets.items()}
    public_item = replace(item, assets={band: {**asset, 'href': 'https://example.invalid/' + band + '.tif'}
                                       for band, asset in item.assets.items()})
    monkeypatch.setattr(writer, '_resolve_asset_paths', lambda **kwargs: local)
    root = tmp_path / 'dataset'
    root.mkdir()
    store = root / item.tile_id / 'scenes_L2A_native/zarr/spectral.zarr'
    context = {'workflow_batch_id': 'batch', 'recipe': {'source': {'stac_api': 'https://example.invalid/stac', 'provider': 'fixture'}}}
    def run(run_id, replace_existing=False):
        tracker = production.SceneRun(root, 'test-dataset', run_id, context)
        writer.append_item_to_native_zarr(item=public_item, bands=list(item.assets), zarr_path=store,
            chunk_size=2, read_mode='remote_stream_first', _provenance_tracker=tracker,
            on_item_conflict='overwrite' if replace_existing else 'skip')
        return tracker
    return root, store, public_item, run


def snapshot(root):
    root = publication._io_path(root)
    return {p.relative_to(root).as_posix(): publication._digest(p.read_bytes())
            for p in root.rglob('*') if p.is_file()}


def test_same_value_replacement_changes_generation_and_catalog_binding(case):
    root, store, item, run = case
    first = run('first')
    first.recover()
    before = publication.read_snapshot(root)
    catalogs_before = {p: publication._digest((root / p).read_bytes()) for p in
                       ['catalog.parquet', item.tile_id + '/catalog.parquet']}
    pixels = snapshot(store)
    second = run('second', replace_existing=True)
    second.recover()
    after = publication.read_snapshot(root)
    assert pixels == snapshot(store)
    assert before['pointer']['data_revision'] != after['pointer']['data_revision']
    assert {r['output_id'] for r in before['outputs']} == {r['output_id'] for r in after['outputs']}
    assert {r['generation_id'] for r in before['outputs']} != {r['generation_id'] for r in after['outputs']}
    assert all(publication._digest((root / p).read_bytes()) != h for p, h in catalogs_before.items())
    for record in read_catalog(root / 'catalog.parquet'):
        for source in json.loads(record['source_assets_json']).values():
            assert source['provenance_generation_id'] == after['outputs'][0]['generation_id']


def test_catalog_third_state_rejects_before_other_catalog_install(case, monkeypatch):
    root, _, _, run = case
    tracker = run('first')
    original = production._install_catalog
    calls = []
    def fail_second(target, staged):
        calls.append(str(target))
        if len(calls) == 2:
            raise OSError('second catalog interruption')
        original(target, staged)
    monkeypatch.setattr(production, '_install_catalog', fail_second)
    with pytest.raises(OSError, match='catalog interruption'):
        tracker.recover()
    intent = publication.load_json(tracker.folder / 'intent.json')
    plan = publication.load_json(tracker.folder / 'catalog_plan.json')
    changed = publication.safe_relative(root, plan['entries'][1]['path'])
    changed.write_bytes(b'external change')
    before = snapshot(root)
    monkeypatch.setattr(production, '_install_catalog', lambda *args: pytest.fail('third state mutated Catalog'))
    with pytest.raises(ValueError, match='third state'):
        tracker.recover()
    assert snapshot(root) == before
    assert intent['generation_id'] == publication.load_json(tracker.folder / 'receipt.json')['generation_id']


def test_changed_current_rejects_before_catalog_install(case, monkeypatch):
    root, _, _, run = case
    first = run('first')
    first.recover()
    first_pointer = publication.read_current(root)
    second = run('second', replace_existing=True)
    second.recover()
    third = run('third', replace_existing=True)
    publication.atomic_json(root / 'provenance/current.json', first_pointer)
    before = snapshot(root)
    monkeypatch.setattr(production, '_install_catalog', lambda *args: pytest.fail('wrong current mutated Catalog'))
    with pytest.raises(ValueError, match='metadata base changed'):
        third.recover()
    assert snapshot(root) == before


def test_writing_without_receipt_never_resumes_or_clears_pending(case, monkeypatch):
    root, _, _, run = case
    monkeypatch.setattr(production.SceneRun, 'commit', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('lost receipt')))
    with pytest.raises(OSError, match='lost receipt'):
        run('first')
    pending = list(publication.safe_relative(root, 'provenance/pending').glob('*.json'))
    assert len(pending) == 1
    intent = publication.load_json(publication.safe_relative(root, 'provenance/runs/first/intent.json'))
    tracker = production.SceneRun(root, 'test-dataset', 'first', intent['context'])
    before = snapshot(root)
    with pytest.raises(ValueError, match='without durable receipt'):
        tracker.recover()
    assert snapshot(root) == before


def test_completed_current_pending_recovery_rechecks_catalog(case, monkeypatch):
    root, _, item, run = case
    tracker = run('first')
    tracker.recover()
    intent = publication.load_json(tracker.folder / 'intent.json')
    receipt = publication.load_json(tracker.folder / 'receipt.json')
    publication.atomic_json(tracker.pending, {'run_id': 'first', 'phase': 'committed_provenance_pending',
        'intent_sha256': receipt['intent_sha256'], 'receipt_sha256': production._file_sha256(tracker.folder / 'receipt.json')})
    catalog = root / item.tile_id / 'catalog.parquet'
    catalog.write_bytes(b'corrupt published catalog')
    before = snapshot(root)
    with pytest.raises(ValueError, match='Catalog'):
        tracker.recover()
    assert snapshot(root) == before
    assert intent['dataset_id'] == 'test-dataset'

@pytest.mark.parametrize('mutation', ['empty', 'missing_global', 'duplicate_tile', 'other_target'])
def test_catalog_plan_must_cover_exact_intended_targets(case, monkeypatch, mutation):
    root, _, _, run = case
    tracker = run('first')
    def interrupt(*args):
        raise OSError('before catalog installation')
    monkeypatch.setattr(production, '_install_catalog', interrupt)
    with pytest.raises(OSError, match='before catalog'):
        tracker.recover()
    path = tracker.folder / 'catalog_plan.json'
    plan = publication.load_json(path)
    if mutation == 'empty':
        plan['entries'] = []
    elif mutation == 'missing_global':
        plan['entries'] = plan['entries'][:1]
    elif mutation == 'duplicate_tile':
        plan['entries'] = [plan['entries'][0], plan['entries'][0]]
    else:
        plan['entries'][1]['path'] = 'unrelated.parquet'
    publication.atomic_json(path, plan)
    before = snapshot(root)
    monkeypatch.setattr(production, '_install_catalog', lambda *args: pytest.fail('Invalid plan installed an unexpected Catalog'))
    with pytest.raises(ValueError, match='[Cc]atalog'):
        tracker.recover()
    assert snapshot(root) == before

def test_unrelated_mapping_change_rejects_before_catalog_install(case, monkeypatch):
    import numpy as np
    import zarr
    root, store, item, run = case
    first = run('first')
    first.recover()
    next_item = replace(item, id='scene-b', datetime='2025-02-02T12:00:00Z')
    context = publication.load_json(first.folder / 'intent.json')['context']
    second = production.SceneRun(root, 'test-dataset', 'second', context)
    original = production.SceneRun.commit
    def corrupt_unrelated_then_commit(self, **kwargs):
        cube = zarr.open_group(str(store), mode='a', use_consolidated=False)
        altered = '2025-01-03T12:00:00Z'
        for group in [cube, cube['10m'], cube['20m']]:
            group['time'][0] = np.datetime64(altered.removesuffix('Z'), 'ns')
        metadata = writer._load_metadata(store)
        metadata['items'][0]['datetime'] = altered
        writer._save_metadata(store, metadata)
        return original(self, **kwargs)
    monkeypatch.setattr(production.SceneRun, 'commit', corrupt_unrelated_then_commit)
    writer.append_item_to_native_zarr(item=next_item, bands=list(item.assets), zarr_path=store,
        chunk_size=2, read_mode='remote_stream_first', _provenance_tracker=second)
    before = snapshot(root)
    monkeypatch.setattr(production, '_install_catalog', lambda *args: pytest.fail('Invalid mapping installed Catalog'))
    with pytest.raises(ValueError, match='unrelated scene mappings'):
        second.recover()
    assert snapshot(root) == before
