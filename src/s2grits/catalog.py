from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


CATALOG_COLUMNS = [
    "collection_id",
    "product_type",
    "tile_id",
    "item_id",
    "datetime",
    "date",
    "cloud_cover",
    "platform",
    "source_collection",
    "provider",
    "stac_api",
    "stac_item_url",
    "bands",
    "resolution_groups_json",
    "declared_size_bytes",
    "zarr_path",
    "cog_path",
    "preview_path",
    "source_assets_json",
    "bbox",
    "geometry_json",
]


CATALOG_SCHEMA = pa.schema(
    [
        pa.field("collection_id", pa.string()),
        pa.field("product_type", pa.string()),
        pa.field("tile_id", pa.string()),
        pa.field("item_id", pa.string()),
        pa.field("datetime", pa.string()),
        pa.field("date", pa.string()),
        pa.field("cloud_cover", pa.float64()),
        pa.field("platform", pa.string()),
        pa.field("source_collection", pa.string()),
        pa.field("provider", pa.string()),
        pa.field("stac_api", pa.string()),
        pa.field("stac_item_url", pa.string()),
        pa.field("bands", pa.string()),
        pa.field("resolution_groups_json", pa.string()),
        pa.field("declared_size_bytes", pa.int64()),
        pa.field("zarr_path", pa.string()),
        pa.field("cog_path", pa.string()),
        pa.field("preview_path", pa.string()),
        pa.field("source_assets_json", pa.string()),
        pa.field("bbox", pa.string()),
        pa.field("geometry_json", pa.string()),
    ]
)


def _normalize_record(record: dict) -> dict:
    normalized = {column: record.get(column) for column in CATALOG_COLUMNS}
    if normalized["cloud_cover"] is not None:
        normalized["cloud_cover"] = float(normalized["cloud_cover"])
    if normalized["declared_size_bytes"] is not None:
        normalized["declared_size_bytes"] = int(normalized["declared_size_bytes"])
    for key, value in list(normalized.items()):
        if key not in {"cloud_cover", "declared_size_bytes"} and value is not None:
            normalized[key] = str(value)
    return normalized


def _dedupe_sort(records: list[dict]) -> list[dict]:
    keyed: dict[tuple[str, str, str], dict] = {}
    for record in records:
        normalized = _normalize_record(record)
        key = (
            str(normalized.get("product_type") or ""),
            str(normalized.get("tile_id") or ""),
            str(normalized.get("item_id") or ""),
        )
        keyed[key] = normalized
    return sorted(
        keyed.values(),
        key=lambda row: (
            str(row.get("tile_id") or ""),
            str(row.get("datetime") or ""),
            str(row.get("item_id") or ""),
        ),
    )


def _write_catalog(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=CATALOG_SCHEMA)
    pq.write_table(table, path)


def read_catalog(path: Path) -> list[dict]:
    if not path.exists():
        return []
    table = pq.read_table(path)
    return [_normalize_record(row) for row in table.to_pylist()]


def upsert_records(path: Path, records: list[dict]) -> list[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = read_catalog(path)
    merged = _dedupe_sort(old + records)
    _write_catalog(path, merged)
    return merged


def resync_global_catalog(output_root: Path) -> Path | None:
    rows: list[dict] = []
    for path in output_root.glob("*/catalog.parquet"):
        if path.parent == output_root:
            continue
        rows.extend(read_catalog(path))
    if not rows:
        return None
    merged = _dedupe_sort(rows)
    out = output_root / "catalog.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_catalog(out, merged)
    return out
