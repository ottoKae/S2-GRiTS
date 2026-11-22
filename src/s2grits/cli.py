from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

from .__version__ import __version__
from .bands import display_bands, selected_native_groups
from .config import date_intervals, load_config, output_path, validate_config
from .grid import enumerate_mgrs_tiles, roi_bbox
from .workflow import run_scenes_workflow


def cmd_doctor(args) -> None:
    cfg = load_config(args.config)
    errors = validate_config(cfg)

    print(f"S2-GRiTS Native {__version__}")
    print(f"Config: {Path(args.config).resolve()}")
    print(f"Workflow: {cfg.get('workflow')}")
    print(f"Output: {cfg['output']['base_dir']}")
    print(f"Logs: {cfg['output']['logs_dir']}")
    print(f"Cache: {cfg['output']['cache_dir']}")
    print(f"Collection: {cfg['source']['collection']}")
    intervals = date_intervals(cfg)
    if len(intervals) == 1:
        print(f"Date interval: {intervals[0]}")
    else:
        print(f"Date intervals: {len(intervals)} ({intervals[0]} ... {intervals[-1]})")
    groups = selected_native_groups([str(band) for band in cfg["assets"]["bands"]])
    print(f"Resolution mode: {cfg['assets']['resolution_mode']}")
    for group_name, bands in groups.items():
        print(f"Bands {group_name}: {display_bands(bands)}")
    print(f"Resampling: {cfg['assets']['resampling']}")
    print(f"New array compression: Blosc {cfg['output']['zarr_compression']}")
    print(f"Pixel cloud mask generated: {cfg['processing']['cloud_mask']['enabled']}")
    selected_downloader = str(cfg["runtime"].get("downloader", "auto"))
    print(f"Downloader selected: {selected_downloader}")

    try:
        tiles = enumerate_mgrs_tiles(cfg)
        print(f"MGRS tiles ({len(tiles)}): {', '.join(tiles)}")
    except Exception as exc:
        errors.append(f"MGRS enumeration failed: {exc}")

    for label, key in (("Output", "base_dir"), ("Logs", "logs_dir"), ("Cache", "cache_dir")):
        directory = output_path(cfg, key)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".s2grits_write_probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            print(f"{label} writable: OK ({directory})")
        except Exception as exc:
            errors.append(f"{label} directory is not writable: {exc}")

    try:
        import shutil

        aria2c = shutil.which("aria2c")
        print(f"aria2c available: {'yes (' + aria2c + ')' if aria2c else 'no'}")
    except Exception as exc:
        errors.append(f"Downloader check failed: {exc}")

    if args.network:
        try:
            root = cfg["source"]["stac_api"].rstrip("/")
            resp = requests.get(root, timeout=20)
            resp.raise_for_status()
            print(f"Earth Search: OK ({root})")
            bbox = roi_bbox(cfg)
            print(f"ROI bbox: {bbox}")
        except Exception as exc:
            errors.append(f"Earth Search network check failed: {exc}")

    if errors:
        print("\nERRORS:")
        for err in errors:
            print(f"  - {err}")
        raise SystemExit(1)
    print("Doctor: OK")


def cmd_process_scenes(args) -> None:
    result = run_scenes_workflow(args.config)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("audit_report", {}).get("ok", False) or any(
        tile.get("status") in {"failed", "partial"}
        for tile in result.get("results", {}).values()
    ):
        raise SystemExit(1)


def cmd_backfill_quality(args) -> None:
    from .quality_backfill import run_quality_backfill
    result = run_quality_backfill(args.config, dry_run=args.dry_run,
                                 max_new_scenes=args.max_new_scenes, tiles=args.tiles,
                                 max_new_scenes_per_tile=args.max_new_scenes_per_tile)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_rebind_quality(args) -> None:
    from .quality_rebind import rebind_quality
    result = rebind_quality(args.config, run_id=args.run_id, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_process_pipeline(args) -> None:
    from .pipeline import run_pipeline
    result = run_pipeline(args.config, dry_run=args.dry_run,
                          max_new_scenes=args.max_new_scenes, tiles=args.tiles,
                          max_new_scenes_per_tile=args.max_new_scenes_per_tile)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s2grits",
        description="Sentinel-2 L2A native-resolution COG downloader and Zarr builder",
    )
    parser.add_argument("--version", action="version", version=f"s2grits {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Validate config and optional network access")
    doctor.add_argument("--config", required=True)
    doctor.add_argument("--network", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    scenes = sub.add_parser("process_scenes", help="Download/process Sentinel-2 L2A scenes")
    scenes.add_argument("--config", required=True)
    scenes.set_defaults(func=cmd_process_scenes)

    quality = sub.add_parser("backfill_quality", help="Build a frozen-scene SCL companion Zarr")
    quality.add_argument("--config", required=True)
    quality.add_argument("--dry-run", action="store_true", help="Read-only preflight; no HTTP or output creation")
    quality.add_argument("--max-new-scenes", type=int)
    quality.add_argument("--max-new-scenes-per-tile", type=int)
    quality.add_argument("--tiles", nargs="+", help="Process chronological prefixes of these frozen tiles")
    quality.set_defaults(func=cmd_backfill_quality)

    rebind = sub.add_parser("rebind_quality", help="Publish a new SCL pairing without changing old quality records")
    rebind.add_argument("--config", required=True)
    rebind.add_argument("--run-id", required=True)
    rebind.add_argument("--dry-run", action="store_true")
    rebind.set_defaults(func=cmd_rebind_quality)

    pipeline = sub.add_parser("process_pipeline", help="Run spectral jobs or optional frozen SCL pairing")
    pipeline.add_argument("--config", required=True)
    pipeline.add_argument("--dry-run", action="store_true", help="Read-only config/source preflight; no HTTP")
    pipeline.add_argument("--max-new-scenes", type=int, help="SCL additions only; not a spectral scene limit")
    pipeline.add_argument("--max-new-scenes-per-tile", type=int, help="SCL additions per frozen tile")
    pipeline.add_argument("--tiles", nargs="+", help="SCL frozen tiles only; spectral ROI stays in its job YAML")
    pipeline.set_defaults(func=cmd_process_pipeline)

    return parser


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "YES")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.json,.xml,.jpg,.jpeg")
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
