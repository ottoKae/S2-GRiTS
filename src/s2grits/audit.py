from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import zarr
import pyarrow as pa
import pyarrow.parquet as pq

from .bands import resolution_metres, variable_name
from .catalog import read_catalog
from .monitor import format_bytes
from .stac import S2Item


_logger = logging.getLogger("s2grits")


def _zarr_metadata(zarr_path: Path) -> dict:
    meta_path = zarr_path / "s2grits_resume_state.json"
    legacy_path = zarr_path / "s2grits_metadata.json"
    if not meta_path.exists() and legacy_path.exists():
        meta_path = legacy_path
    if not meta_path.exists():
        return {"items": []}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}


def _scene_name(item: S2Item) -> str:
    token = item.dt.strftime("%Y%m%dT%H%M%S")
    return f"s2grits_scenes_{item.tile_id}_{token}_{item.id}"


def _store_warnings(
    zarr_path: Path,
    band_groups: dict[str, list[str]],
    committed_count: int,
) -> list[str]:
    warnings: list[str] = []
    if not zarr_path.exists():
        return ["Zarr store does not exist"]
    try:
        root = zarr.open_group(str(zarr_path), mode="r")
    except Exception as exc:
        return [f"Zarr store is unreadable: {exc}"]
    if "time" not in root:
        warnings.append("root time array is missing")
    elif int(root["time"].shape[0]) != committed_count:
        warnings.append(
            f"root time length={root['time'].shape[0]} but committed={committed_count}"
        )
    for group_name, bands in band_groups.items():
        if group_name not in root:
            warnings.append(f"resolution group {group_name} is missing")
            continue
        group = root[group_name]
        actual_resolution = group.attrs.get("resolution_metres")
        if actual_resolution != resolution_metres(group_name):
            warnings.append(
                f"{group_name} resolution attr={actual_resolution}, "
                f"expected={resolution_metres(group_name)}"
            )
        for attribute in (
            "crs",
            "transform",
            "width",
            "height",
            "grid_id",
            "grid_name",
            "grid_version",
            "processing_signature",
        ):
            if group.attrs.get(attribute) is None:
                warnings.append(f"{group_name} {attribute} attr is missing")
        if "spatial_ref" not in group:
            warnings.append(f"{group_name}/spatial_ref is missing")
        for coordinate in ("x", "y"):
            if coordinate not in group:
                warnings.append(f"{group_name}/{coordinate} is missing")
            elif group[coordinate].attrs.get("axis") != coordinate.upper():
                warnings.append(f"{group_name}/{coordinate} CF axis attr is missing")
        if "time" not in group:
            warnings.append(f"{group_name}/time is missing")
        elif int(group["time"].shape[0]) != committed_count:
            warnings.append(
                f"{group_name}/time length={group['time'].shape[0]}, "
                f"committed={committed_count}"
            )
        for band in bands:
            variable = variable_name(band)
            if variable not in group:
                warnings.append(f"{group_name}/{variable} is missing")
                continue
            if int(group[variable].shape[0]) != committed_count:
                warnings.append(
                    f"{group_name}/{variable} time length={group[variable].shape[0]}, "
                    f"committed={committed_count}"
                )
            if group[variable].attrs.get("grid_mapping") != "spatial_ref":
                warnings.append(f"{group_name}/{variable} grid_mapping attr is missing")
    return warnings


def audit_outputs(
    *,
    output_root: Path,
    cache_root: Path,
    operational_dir: Path,
    expected_by_tile: dict[str, list[S2Item]],
    bands: list[str],
    band_groups: dict[str, list[str]],
    formats: dict[str, Any],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(output_root),
        "resolution_mode": "native_groups",
        "band_groups": band_groups,
        "tiles_checked": len(expected_by_tile),
        "expected_scenes": sum(len(items) for items in expected_by_tile.values()),
        "missing": [],
        "warnings": [],
        "summary": {},
    }

    for tile, items in expected_by_tile.items():
        tile_dir = output_root / tile
        product_dir = tile_dir / "scenes_L2A_native"
        zarr_path = product_dir / "zarr" / f"s2grits_scenes_{tile}_L2A_native.zarr"
        catalog_path = tile_dir / "catalog.parquet"

        catalog_ids: set[str] = set()
        if catalog_path.exists():
            try:
                catalog = read_catalog(catalog_path)
                catalog_ids = {
                    str(row.get("item_id")) for row in catalog if row.get("item_id")
                }
            except Exception as exc:
                report["warnings"].append(f"{tile}: catalog unreadable: {exc}")

        metadata = _zarr_metadata(zarr_path) if formats.get("zarr", True) else {"items": []}
        zarr_ids = {
            str(entry.get("item_id"))
            for entry in metadata.get("items", [])
            if entry.get("item_id")
        }
        store_warnings = _store_warnings(zarr_path, band_groups, len(zarr_ids))
        report["warnings"].extend(f"{tile}: {warning}" for warning in store_warnings)
        tile_missing: list[dict[str, Any]] = []
        cached_bytes = 0

        for item in items:
            missing_parts: list[str] = []
            if item.id not in catalog_ids:
                missing_parts.append("catalog")
            if formats.get("zarr", True) and item.id not in zarr_ids:
                missing_parts.append("zarr")
            if formats.get("preview", True):
                preview_path = product_dir / "preview" / f"{_scene_name(item)}.jpg"
                if not preview_path.exists() or preview_path.stat().st_size <= 0:
                    missing_parts.append("preview")

            cache_dir = cache_root / item.id
            cached_bands = []
            for band in bands:
                candidates = list(cache_dir.glob(f"{band}.tif")) if cache_dir.exists() else []
                complete = [
                    path for path in candidates if path.is_file() and path.stat().st_size > 0
                ]
                if complete:
                    cached_bands.append(band)
                    cached_bytes += complete[0].stat().st_size
            if cached_bands and len(cached_bands) != len(bands):
                report["warnings"].append(
                    f"{tile}/{item.id}: cache has {len(cached_bands)}/{len(bands)} bands"
                )

            if missing_parts:
                tile_missing.append(
                    {
                        "tile": tile,
                        "item_id": item.id,
                        "datetime": item.datetime,
                        "cloud_cover": item.cloud_cover,
                        "missing": missing_parts,
                    }
                )

        report["missing"].extend(tile_missing)
        report["summary"][tile] = {
            "expected": len(items),
            "catalog": len(catalog_ids),
            "zarr": len(zarr_ids),
            "missing": len(tile_missing),
            "store_warnings": len(store_warnings),
            "cached_size": format_bytes(cached_bytes),
        }
        if tile_missing or store_warnings:
            _logger.warning(
                "[Audit] tile=%s expected=%d missing=%d store_warnings=%d",
                tile,
                len(items),
                len(tile_missing),
                len(store_warnings),
            )
        else:
            _logger.info(
                "[Audit] tile=%s OK expected=%d cached_size=%s",
                tile,
                len(items),
                format_bytes(cached_bytes),
            )

    report["ok"] = not report["missing"] and not report["warnings"]
    safe_dataset = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in output_root.name
    )
    retry_path = operational_dir / f"S2-GRiTS__dataset-{safe_dataset}__retry-scenes.parquet"
    if report["missing"]:
        operational_dir.mkdir(parents=True, exist_ok=True)
        retry_rows = [
            {**row, "missing": ",".join(row["missing"])} for row in report["missing"]
        ]
        pq.write_table(pa.Table.from_pylist(retry_rows), retry_path)
        report["retry_manifest"] = str(retry_path)
    elif retry_path.exists():
        retry_path.unlink()

    _logger.info(
        "[Audit] expected=%d missing=%d warnings=%d ok=%s",
        report["expected_scenes"],
        len(report["missing"]),
        len(report["warnings"]),
        report["ok"],
    )
    return report
