"""Shared GRiTS canonical catalog contract.

The core columns and grid hash are compatible with S1-GRiTS canonical schema
version 7. S2-specific extension columns describe native-resolution Zarr
subgroups without changing the common resolver contract.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pyproj import CRS

from .__version__ import __version__


SCHEMA_VERSION = 7
GRID_ID_VERSION = 1
_GRID_ROUND_PRECISION = 9

CANONICAL_CATALOG_COLUMNS = [
    "item_id",
    "collection_id",
    "product_type",
    "product_label",
    "schema_version",
    "tile_id",
    "flight_direction",
    "crs",
    "transform",
    "width",
    "height",
    "grid_id",
    "grid_name",
    "grid_version",
    "resolution_x",
    "resolution_y",
    "grid_source",
    "reference_grid_id",
    "reference_zarr_path",
    "datetime",
    "start_datetime",
    "end_datetime",
    "month",
    "geometry_group_id",
    "track",
    "n_bursts",
    "n_scenes",
    "jpl_burst_ids",
    "opera_ids",
    "pass_id",
    "primary_track",
    "track_coverage_json",
    "zarr_path",
    "cog_path",
    "preview_path",
    "item_path",
    "bands",
    "processing_level",
    "product_variant",
    "processing_signature",
    "processing_variant_json",
    "status",
    "created_at",
    "software_version",
]

# Backward-compatible extension columns. A consumer that only knows schema v7
# may ignore these; a group-aware resolver uses them to open the native S2 grid.
S2_EXTENSION_COLUMNS = [
    "sensor",
    "platform",
    "source_collection",
    "provider",
    "stac_api",
    "stac_item_url",
    "resolution_group",
    "zarr_group",
    "temporal_semantics",
    "spatial_alignment_mode",
    "cloud_cover",
    "declared_size_bytes",
    "source_assets_json",
    "bbox",
    "geometry_json",
]

CATALOG_COLUMNS = CANONICAL_CATALOG_COLUMNS + S2_EXTENSION_COLUMNS


def make_grid_id(
    tile_id: str,
    crs: str,
    transform: list[float],
    width: int,
    height: int,
    resolution: float | list[float] | None = None,
    version: int = GRID_ID_VERSION,
) -> str:
    """Return the same deterministic 12-hex grid identity as S1-GRiTS."""
    crs_obj = CRS.from_user_input(crs)
    authority = crs_obj.to_authority()
    if authority:
        normalized_crs = f"{authority[0]}:{authority[1]}"
    else:
        normalized_crs = "".join(crs_obj.to_wkt().split()).upper()

    payload: dict[str, Any] = {
        "v": version,
        "tile": str(tile_id).upper(),
        "crs": normalized_crs,
        "transform": [
            float(round(float(value), _GRID_ROUND_PRECISION)) for value in transform
        ],
        "w": int(width),
        "h": int(height),
    }
    if resolution is not None:
        if isinstance(resolution, (list, tuple)):
            payload["res"] = [
                round(float(value), _GRID_ROUND_PRECISION) for value in resolution
            ]
        else:
            payload["res"] = round(float(resolution), _GRID_ROUND_PRECISION)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def format_grid_name(tile_id: str, transform: list[float], crs: str) -> str:
    crs_obj = CRS.from_user_input(crs)
    unit = "deg" if crs_obj.is_geographic else "m"
    resolution_x = abs(float(transform[0]))
    resolution_y = abs(float(transform[4]))
    if abs(resolution_x - resolution_y) < 1e-9:
        suffix = f"{resolution_x:g}{unit}"
    else:
        suffix = f"{resolution_x:g}x{resolution_y:g}{unit}"
    return f"{str(tile_id).upper()}_native_{suffix}"


def make_processing_signature(values: dict[str, Any]) -> str:
    """Use S1-GRiTS' compact, sorted JSON SHA-256 processing signature."""
    payload = json.dumps(values, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "p" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:11]


def normalize_catalog_record(record: dict[str, Any], status: str = "complete") -> dict[str, Any]:
    """Normalize one S2 record to canonical v7 plus S2 extension columns."""
    normalized = {column: record.get(column) for column in CATALOG_COLUMNS}
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["status"] = status
    normalized["created_at"] = record.get("created_at") or datetime.now(
        timezone.utc
    ).isoformat()
    normalized["software_version"] = record.get("software_version") or __version__

    transform = normalized.get("transform")
    crs = normalized.get("crs")
    width = normalized.get("width")
    height = normalized.get("height")
    tile_id = normalized.get("tile_id")
    if tile_id and crs and transform and width and height:
        resolution_x = abs(float(transform[0]))
        resolution_y = abs(float(transform[4]))
        normalized["grid_id"] = make_grid_id(
            str(tile_id),
            str(crs),
            list(transform),
            int(width),
            int(height),
            resolution=[resolution_x, resolution_y],
        )
        normalized["grid_name"] = record.get("grid_name") or format_grid_name(
            str(tile_id), list(transform), str(crs)
        )
        normalized["grid_version"] = GRID_ID_VERSION
        normalized["resolution_x"] = round(resolution_x, _GRID_ROUND_PRECISION)
        normalized["resolution_y"] = round(resolution_y, _GRID_ROUND_PRECISION)

    for column in ("bands", "processing_variant_json"):
        value = normalized.get(column)
        if value is not None and not isinstance(value, str):
            normalized[column] = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return normalized
