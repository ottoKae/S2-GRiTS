"""Optional SCL orchestration; existing producers retain their storage contracts."""
from pathlib import Path

import yaml

from .config import load_config, load_pipeline_config, output_path, validate_config


def run_pipeline(config_path, *, dry_run=False, max_new_scenes=None,
                 max_new_scenes_per_tile=None, tiles=None):
    cfg = load_pipeline_config(config_path)
    for name, value in (("max_new_scenes", max_new_scenes),
                        ("max_new_scenes_per_tile", max_new_scenes_per_tile)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a nonnegative integer")
    report = {"status": "dry_run" if dry_run else "skipped",
              "spectral_root": str(cfg["spectral_root"]),
              "spectral": {"status": "REUSE_EXISTING"},
              "scl": {"status": "SKIPPED_DISABLED"}}
    if cfg["scene_job"] is not None:
        scenes = load_config(cfg["scene_job"])
        errors = validate_config(scenes)
        if errors:
            raise ValueError("Invalid spectral job: " + "; ".join(errors))
        # Preserve the original scenes workflow's cwd-relative base_dir semantics.
        if output_path(scenes, "base_dir").resolve() != cfg["spectral_root"]:
            raise ValueError("spectral.root differs from spectral job output.base_dir")
        if cfg["scl_enabled"]:
            # Frozen Catalog/source contracts cannot be prepared before an arbitrary
            # scene search finishes. Keep this explicit, rather than auto-rebinding.
            raise ValueError("Run spectral.job_config with SCL disabled first, freeze the selected "
                             "scenes, then set spectral.job_config: null and enable SCL")
        if dry_run:
            report["spectral"] = {"status": "CONFIG_VALIDATED_NO_NETWORK"}
        else:
            from .workflow import run_scenes_workflow
            result = run_scenes_workflow(cfg["scene_job"])
            if not result.get("audit_report", {}).get("ok", False) or any(
                tile.get("status") in {"failed", "partial"}
                for tile in result.get("results", {}).values()
            ):
                raise RuntimeError("SPECTRAL_FAILED: scene workflow audit did not pass; results retained")
            report["spectral"] = {"status": "COMPLETE", "result": result}
            report["status"] = "complete"
    if not cfg["scl_enabled"]:
        return report

    # Import lazily: disabled SCL must not inspect a quality root or require its lock environment.
    from .quality_backfill import run_quality_backfill
    try:
        job = yaml.safe_load(cfg["quality_job"].read_text(encoding="utf-8"))
        root = Path(job["quality"]["spectral_root"])
        if not root.is_absolute():
            root = cfg["quality_job"].parent / root
        if root.resolve() != cfg["spectral_root"]:
            raise ValueError("SCL job spectral_root differs from the pipeline spectral.root")
        result = run_quality_backfill(cfg["quality_job"], dry_run=dry_run,
                                      max_new_scenes=max_new_scenes,
                                      max_new_scenes_per_tile=max_new_scenes_per_tile, tiles=tiles)
    except Exception as exc:
        raise RuntimeError(f"SCL_FAILED: {exc}") from exc
    stage = "PREFLIGHT_PASSED" if dry_run else ("COMPLETE" if result["complete"] else "PUBLISHED_PARTIAL")
    report["scl"] = {"status": stage, "result": result}
    if not dry_run:
        report["status"] = "complete" if result["complete"] else "partial"
    return report
