"""Optional dispatch and real offline scene pairing; never use production data."""
import json
from pathlib import Path

import pytest

from s2grits import quality_backfill as q
from s2grits.cli import build_parser, main
from s2grits.config import load_pipeline_config, validate_config
from s2grits.pipeline import run_pipeline
from test_quality_backfill import job  # shared local COG/three-independent-index fixture


def parent(tmp_path, *, enabled=False, root=None, child=None, scene_job=None):
    path = tmp_path / 'pipeline.json'
    value = {'workflow': 'pipeline', 'spectral': {
        'root': str(root or tmp_path/'spectral'), 'job_config': scene_job},
        'companions': {'scl': {'enabled': enabled, 'job_config': child}}}
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


@pytest.mark.parametrize('dry_run', [False, True])
def test_disabled_never_opens_quality_or_creates_output(tmp_path, monkeypatch, dry_run):
    def forbidden(*args, **kwargs):
        pytest.fail('Disabled SCL accessed quality resources')
    monkeypatch.setattr(q, 'load_job', forbidden)
    path = parent(tmp_path, child='does-not-exist/quality.yaml')
    before = set(tmp_path.rglob('*'))
    result = run_pipeline(path, dry_run=dry_run)
    assert result['scl']['status'] == 'SKIPPED_DISABLED'
    assert result['status'] == ('dry_run' if dry_run else 'skipped')
    assert set(tmp_path.rglob('*')) == before


def test_enabled_validates_frozen_inventory_once(job, tmp_path, monkeypatch):
    child, cfg, _ = job
    path = parent(tmp_path, enabled=True, child=child.name, root=cfg['quality']['spectral_root'])
    original, calls = q.load_job, []
    def counted(config):
        calls.append(config)
        return original(config)
    monkeypatch.setattr(q, 'load_job', counted)
    assert run_pipeline(path, dry_run=True)['scl']['status'] == 'PREFLIGHT_PASSED'
    assert len(calls) == 1


@pytest.mark.parametrize('value', ['false', 'true', '是', 0, 1, None])
def test_nonboolean_switch_rejected(tmp_path, value):
    with pytest.raises(ValueError, match='true or false'):
        load_pipeline_config(parent(tmp_path, enabled=value))


def test_missing_default_and_invalid_enabled_child(tmp_path):
    path = parent(tmp_path)
    cfg = json.loads(path.read_text())
    cfg.pop('companions')
    path.write_text(json.dumps(cfg))
    assert not load_pipeline_config(path)['scl_enabled']
    with pytest.raises(ValueError, match='job_config'):
        load_pipeline_config(parent(tmp_path, enabled=True))


def test_legacy_scene_config_cannot_silently_ignore_switch():
    assert any('process_pipeline' in s for s in validate_config({'companions': {'scl': {'enabled': False}}}))


def test_enabled_actual_pairing_independent_indices_resume_and_disable(job, tmp_path, monkeypatch):
    child, cfg, _ = job
    path = parent(tmp_path, enabled=True, child=child.name, root=cfg['quality']['spectral_root'])
    monkeypatch.chdir(tmp_path.parent)  # child path resolution must not depend on cwd
    assert run_pipeline(path, dry_run=True)['scl']['result']['scenes'] == 3
    assert not Path(cfg['output']['base_dir']).exists()
    partial = run_pipeline(path, max_new_scenes=1, tiles=['17MNR'])
    assert partial['status'] == 'partial' and partial['scl']['status'] == 'PUBLISHED_PARTIAL'
    root = Path(cfg['output']['base_dir'])
    import pyarrow.parquet as pq
    link = pq.read_table(root/'quality_links.parquet').to_pylist()[0]
    assert link['tile_id'] == '17MNR' and link['item_id'] == 'scene_0'
    assert [link[p+'_time_index'] for p in ['spectral_10m','spectral_20m','quality_20m']] == [2,1,0]
    done = run_pipeline(path)
    assert done['status'] == 'complete' and done['scl']['result']['accepted_total'] == 3
    repeated = run_pipeline(path)['scl']['result']
    assert repeated['accepted_this_run'] == 0 and not repeated['download_started']
    hashes = {p: q.digest(p) for p in root.rglob('*') if p.is_file()}
    parent(tmp_path, enabled=False, child=child.name, root=cfg['quality']['spectral_root'])
    assert run_pipeline(path)['scl']['status'] == 'SKIPPED_DISABLED'
    assert hashes == {p: q.digest(p) for p in root.rglob('*') if p.is_file()}


def test_different_spectral_root_rejected_before_quality_writes(job, tmp_path):
    child, cfg, _ = job
    path = parent(tmp_path, enabled=True, root=tmp_path/'other', child=child.name)
    with pytest.raises(RuntimeError, match='spectral_root differs'):
        run_pipeline(path)
    assert not Path(cfg['output']['base_dir']).exists()


def test_wrong_tile_identity_in_manifest_is_not_paired(job, tmp_path):
    child, cfg, _ = job
    manifest = Path(cfg['quality']['backfill_manifest'])
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    rows[0]['tile_id'] = '18MTE'
    manifest.write_text('\n'.join(json.dumps(row) for row in rows))
    cfg['quality']['backfill_manifest_sha256'] = q.digest(manifest)
    q.atomic_json(child, cfg)
    path = parent(tmp_path, enabled=True, root=cfg['quality']['spectral_root'], child=child.name)
    with pytest.raises(RuntimeError, match='Frozen manifests disagree'):
        run_pipeline(path)
    assert not Path(cfg['output']['base_dir']).exists()


def scene_config(tmp_path):
    p = tmp_path/'scenes.json'
    p.write_text(json.dumps({'workflow': 'scenes', 'time': {'years': [2023]},
        'assets': {'bands': ['red', 'swir16']},
        'output': {'base_dir': str(tmp_path/'spectral'), 'formats': {'cog': False}}}))
    return p


def test_scene_dispatch_and_failed_audit_stop(tmp_path, monkeypatch):
    from s2grits import workflow
    scene = scene_config(tmp_path)
    path = parent(tmp_path, scene_job=scene.name)
    monkeypatch.setattr(workflow, 'run_scenes_workflow', lambda *a: pytest.fail('dry-run wrote scenes'))
    assert run_pipeline(path, dry_run=True)['spectral']['status'] == 'CONFIG_VALIDATED_NO_NETWORK'
    monkeypatch.setattr(workflow, 'run_scenes_workflow', lambda *a: {'audit_report': {'ok': True}})
    assert run_pipeline(path)['spectral']['status'] == 'COMPLETE'
    monkeypatch.setattr(workflow, 'run_scenes_workflow', lambda *a: {'audit_report': {'ok': False}})
    with pytest.raises(RuntimeError, match='SPECTRAL_FAILED'):
        run_pipeline(path)


def test_no_write_before_frozen_contract_and_no_root_mismatch(tmp_path, monkeypatch):
    from s2grits import workflow
    scene = scene_config(tmp_path)
    monkeypatch.setattr(workflow, 'run_scenes_workflow', lambda *a: pytest.fail('must fail before writing'))
    path = parent(tmp_path, scene_job=scene.name, enabled=True, child='pending.yaml')
    with pytest.raises(ValueError, match='freeze the selected'):
        run_pipeline(path)
    path = parent(tmp_path, scene_job=scene.name, root=tmp_path/'wrong')
    with pytest.raises(ValueError, match='output.base_dir'):
        run_pipeline(path)


def test_cli_switch_dispatch_and_limits(tmp_path, capsys):
    path = parent(tmp_path)
    main(['process_pipeline', '--config', str(path), '--dry-run'])
    assert json.loads(capsys.readouterr().out)['scl']['status'] == 'SKIPPED_DISABLED'
    args = build_parser().parse_args(['process_pipeline', '--config', str(path), '--max-new-scenes', '8', '--tiles', '17MNR'])
    assert args.max_new_scenes == 8 and args.tiles == ['17MNR']
    with pytest.raises(ValueError, match='nonnegative'):
        run_pipeline(path, max_new_scenes=-1)
