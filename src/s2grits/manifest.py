from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .bands import BAND_LABELS
from .monitor import scene_declared_size
from .stac import S2Item, asset_summary_json


MANIFEST_COLUMNS = [
    "tile_id",
    "tile_index",
    "tile_count",
    "scene_index",
    "scene_count",
    "item_id",
    "datetime",
    "date",
    "platform",
    "cloud_cover",
    "source_collection",
    "provider",
    "stac_api",
    "stac_item_url",
    "product_uri",
    "processing_baseline",
    "granule_id",
    "bands_10m",
    "bands_20m",
    "bands_60m",
    "source_asset_count",
    "source_assets_json",
    "declared_size_bytes",
    "downloaded_size_bytes",
    "status",
    "current_stage",
    "download_seconds",
    "zarr_seconds",
    "preview_seconds",
    "total_seconds",
    "zarr_path",
    "preview_path",
    "error",
    "updated_at",
]

_INT_COLUMNS = {
    "tile_index",
    "tile_count",
    "scene_index",
    "scene_count",
    "source_asset_count",
    "declared_size_bytes",
    "downloaded_size_bytes",
}
_FLOAT_COLUMNS = {
    "cloud_cover",
    "download_seconds",
    "zarr_seconds",
    "preview_seconds",
    "total_seconds",
}


def _schema() -> pa.Schema:
    fields = []
    for column in MANIFEST_COLUMNS:
        if column in _INT_COLUMNS:
            fields.append(pa.field(column, pa.int64()))
        elif column in _FLOAT_COLUMNS:
            fields.append(pa.field(column, pa.float64()))
        else:
            fields.append(pa.field(column, pa.string()))
    return pa.schema(fields)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _property(item: S2Item, *names: str) -> str | None:
    for name in names:
        value = item.properties.get(name)
        if value is not None:
            return str(value)
    return None


def _band_list(bands: list[str], resolution: int, item: S2Item) -> str:
    selected = []
    for band in bands:
        asset = item.assets.get(band) or {}
        gsd = asset.get("gsd")
        raster_bands = asset.get("raster:bands") or []
        if gsd is None and raster_bands:
            gsd = raster_bands[0].get("spatial_resolution")
        if gsd is not None and int(gsd) == resolution:
            selected.append(BAND_LABELS.get(band, band))
    return ",".join(selected)


class DownloadManifest:
    def __init__(self, output_root: Path, *, flush_interval_seconds: float = 30.0) -> None:
        self.output_root = output_root
        self.parquet_path = output_root / "download_manifest.parquet"
        self.csv_path = output_root / "download_manifest.csv"
        self.events_path = output_root / "download_events.jsonl"
        self.flush_interval_seconds = max(1.0, float(flush_interval_seconds))
        self.rows: dict[str, dict[str, Any]] = {}
        self._last_flush = 0.0
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.parquet_path.exists():
            return
        try:
            for row in pq.read_table(self.parquet_path).to_pylist():
                if row.get("item_id"):
                    self.rows[str(row["item_id"])] = self._normalize(row)
        except Exception:
            return

    @staticmethod
    def _normalize(row: dict[str, Any]) -> dict[str, Any]:
        normalized = {column: row.get(column) for column in MANIFEST_COLUMNS}
        for column in _INT_COLUMNS:
            value = normalized.get(column)
            normalized[column] = int(value) if value not in (None, "") else None
        for column in _FLOAT_COLUMNS:
            value = normalized.get(column)
            normalized[column] = float(value) if value not in (None, "") else None
        for column in set(MANIFEST_COLUMNS) - _INT_COLUMNS - _FLOAT_COLUMNS:
            value = normalized.get(column)
            normalized[column] = str(value) if value is not None else None
        return normalized

    def discover(
        self,
        *,
        item: S2Item,
        bands: list[str],
        tile_index: int,
        tile_count: int,
        scene_index: int,
        scene_count: int,
        stac_api: str,
        provider: str,
    ) -> None:
        previous = self.rows.get(item.id, {})
        row = {
            "tile_id": item.tile_id,
            "tile_index": tile_index,
            "tile_count": tile_count,
            "scene_index": scene_index,
            "scene_count": scene_count,
            "item_id": item.id,
            "datetime": item.datetime,
            "date": item.dt.date().isoformat(),
            "platform": item.platform,
            "cloud_cover": item.cloud_cover,
            "source_collection": item.collection,
            "provider": provider,
            "stac_api": stac_api,
            "stac_item_url": item.self_href,
            "product_uri": _property(item, "s2:product_uri", "product_uri"),
            "processing_baseline": _property(item, "s2:processing_baseline"),
            "granule_id": _property(item, "s2:granule_id"),
            "bands_10m": _band_list(bands, 10, item),
            "bands_20m": _band_list(bands, 20, item),
            "bands_60m": _band_list(bands, 60, item),
            "source_asset_count": len(bands),
            "source_assets_json": asset_summary_json(item, bands),
            "declared_size_bytes": scene_declared_size(item, bands),
            "downloaded_size_bytes": previous.get("downloaded_size_bytes"),
            "status": previous.get("status") or "discovered",
            "current_stage": previous.get("current_stage") or "query_complete",
            "download_seconds": previous.get("download_seconds"),
            "zarr_seconds": previous.get("zarr_seconds"),
            "preview_seconds": previous.get("preview_seconds"),
            "total_seconds": previous.get("total_seconds"),
            "zarr_path": previous.get("zarr_path"),
            "preview_path": previous.get("preview_path"),
            "error": previous.get("error"),
            "updated_at": _now(),
        }
        self.rows[item.id] = self._normalize(row)

    def update(self, item_id: str, *, event: str | None = None, **values: Any) -> None:
        row = self.rows.get(item_id)
        if row is None:
            raise KeyError(f"Manifest item was not discovered: {item_id}")
        row.update(values)
        row["updated_at"] = _now()
        self.rows[item_id] = self._normalize(row)
        self.append_event(item_id=item_id, event=event or str(values.get("current_stage") or "update"), **values)

    def append_event(self, *, item_id: str, event: str, **values: Any) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": _now(), "item_id": item_id, "event": event, **values}
        with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def flush_if_due(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_flush < self.flush_interval_seconds:
            return False
        rows = sorted(
            (self._normalize(row) for row in self.rows.values()),
            key=lambda row: (
                str(row.get("tile_id") or ""),
                str(row.get("datetime") or ""),
                str(row.get("item_id") or ""),
            ),
        )
        self.output_root.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows, schema=_schema())
        parquet_tmp = self.parquet_path.with_suffix(".parquet.tmp")
        pq.write_table(table, parquet_tmp)
        os.replace(parquet_tmp, self.parquet_path)

        csv_tmp = self.csv_path.with_suffix(".csv.tmp")
        with csv_tmp.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(csv_tmp, self.csv_path)
        self._last_flush = now
        return True
