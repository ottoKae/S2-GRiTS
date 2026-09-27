from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .canonical_catalog_schema import CATALOG_COLUMNS, normalize_catalog_record
from .locking import InterProcessLock


_LIST_STRING_COLUMNS = {"jpl_burst_ids", "opera_ids"}
_INT_COLUMNS = {
    "schema_version",
    "width",
    "height",
    "grid_version",
    "track",
    "n_bursts",
    "n_scenes",
    "pass_id",
    "primary_track",
    "declared_size_bytes",
}
_FLOAT_COLUMNS = {"resolution_x", "resolution_y", "cloud_cover"}
_TIMESTAMP_COLUMNS = {"datetime", "start_datetime", "end_datetime"}


def _field(column: str) -> pa.Field:
    if column == "transform":
        return pa.field(column, pa.list_(pa.float64()))
    if column in _LIST_STRING_COLUMNS:
        return pa.field(column, pa.list_(pa.string()))
    if column in _INT_COLUMNS:
        return pa.field(column, pa.int64())
    if column in _FLOAT_COLUMNS:
        return pa.field(column, pa.float64())
    if column in _TIMESTAMP_COLUMNS:
        return pa.field(column, pa.timestamp("ns", tz="UTC"))
    return pa.field(column, pa.string())


CATALOG_SCHEMA = pa.schema([_field(column) for column in CATALOG_COLUMNS])


def relative_asset_path(path: str | Path | None, output_root: Path) -> str | None:
    """Return a portable POSIX path relative to the global output root."""
    if path is None:
        return None
    root = output_root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Asset path is outside the global output root: {candidate}") from exc
    return relative.as_posix()


def _timestamp(value):
    if value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_record(record: dict) -> dict:
    normalized = normalize_catalog_record(record, status=str(record.get("status") or "complete"))
    for column in _TIMESTAMP_COLUMNS:
        normalized[column] = _timestamp(normalized.get(column))
    for column in _INT_COLUMNS:
        if normalized.get(column) is not None:
            normalized[column] = int(normalized[column])
    for column in _FLOAT_COLUMNS:
        if normalized.get(column) is not None:
            normalized[column] = float(normalized[column])
    if normalized.get("transform") is not None:
        normalized["transform"] = [float(value) for value in normalized["transform"][:6]]
    return normalized


def _dedupe_sort(records: list[dict]) -> list[dict]:
    keyed: dict[tuple[str, str, str, str, str], dict] = {}
    for record in records:
        normalized = _normalize_record(record)
        key = (
            str(normalized.get("sensor") or ""),
            str(normalized.get("product_type") or ""),
            str(normalized.get("tile_id") or ""),
            str(normalized.get("item_id") or ""),
            str(normalized.get("resolution_group") or ""),
        )
        keyed[key] = normalized
    return sorted(
        keyed.values(),
        key=lambda row: (
            str(row.get("tile_id") or ""),
            row.get("datetime") or datetime.min.replace(tzinfo=timezone.utc),
            str(row.get("item_id") or ""),
            str(row.get("resolution_group") or ""),
        ),
    )


def _write_catalog(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=CATALOG_SCHEMA)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)


def read_catalog(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [_normalize_record(row) for row in pq.read_table(path).to_pylist()]


def upsert_records(
    path: Path, records: list[dict], lock_path: Path | None = None
) -> list[dict]:
    with InterProcessLock(lock_path) if lock_path is not None else nullcontext():
        merged = _dedupe_sort(read_catalog(path) + records)
        _write_catalog(path, merged)
        return merged


def resync_global_catalog(output_root: Path, lock_path: Path | None = None) -> Path | None:
    with InterProcessLock(lock_path) if lock_path is not None else nullcontext():
        rows: list[dict] = []
        for path in output_root.glob("*/catalog.parquet"):
            rows.extend(read_catalog(path))
        if not rows:
            return None
        out = output_root / "catalog.parquet"
        _write_catalog(out, _dedupe_sort(rows))
        return out
