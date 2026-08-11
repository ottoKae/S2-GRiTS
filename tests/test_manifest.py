from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

from s2grits.bands import NATIVE_BANDS
from s2grits.manifest import DownloadManifest
from s2grits.stac import S2Item


class DownloadManifestTest(unittest.TestCase):
    def test_writes_csv_parquet_and_event_ledger(self) -> None:
        assets = {}
        for band in NATIVE_BANDS:
            resolution = 10 if band in {"blue", "green", "red", "nir"} else 20
            if band in {"coastal", "nir09"}:
                resolution = 60
            assets[band] = {
                "href": f"https://example.invalid/{band}.tif",
                "gsd": resolution,
                "file:size": 100,
                "raster:bands": [{"data_type": "uint16", "spatial_resolution": resolution}],
            }
        item = S2Item(
            id="scene-1",
            collection="sentinel-2-c1-l2a",
            datetime="2025-01-01T00:00:00Z",
            tile_id="50RPV",
            cloud_cover=5.0,
            platform="sentinel-2b",
            bbox=None,
            geometry=None,
            assets=assets,
            properties={"s2:processing_baseline": "05.11"},
            links=[{"rel": "self", "href": "https://example.invalid/items/scene-1"}],
        )
        with tempfile.TemporaryDirectory() as tmp_value:
            manifest = DownloadManifest(Path(tmp_value), flush_interval_seconds=1)
            manifest.discover(
                item=item,
                bands=list(NATIVE_BANDS),
                tile_index=1,
                tile_count=26,
                scene_index=1,
                scene_count=8,
                stac_api="https://earth-search.aws.element84.com/v1",
                provider="Element 84 Earth Search / AWS Open Data",
            )
            manifest.update(
                item.id,
                event="scene_complete",
                status="complete",
                current_stage="complete",
                downloaded_size_bytes=1400,
            )
            manifest.flush_if_due(force=True)
            row = pq.read_table(manifest.parquet_path).to_pylist()[0]
            self.assertEqual(row["tile_id"], "50RPV")
            self.assertEqual(row["declared_size_bytes"], 1400)
            self.assertEqual(row["bands_60m"], "B01,B09")
            self.assertEqual(row["status"], "complete")
            self.assertTrue(manifest.csv_path.exists())
            self.assertIn("scene_complete", manifest.events_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
