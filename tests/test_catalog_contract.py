from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq

from s2grits.canonical_catalog_schema import SCHEMA_VERSION, make_grid_id
from s2grits.catalog import relative_asset_path, upsert_records


class CanonicalCatalogContractTest(unittest.TestCase):
    def test_grid_id_is_stable_and_catalog_has_one_record_per_native_group(self) -> None:
        transform = [10.0, 0.0, 500000.0, 0.0, -10.0, 3600000.0]
        grid_id = make_grid_id(
            "50RPV", "EPSG:32650", transform, 12, 12, resolution=[10.0, 10.0]
        )
        self.assertEqual(grid_id, "7822f89ade23")

        with tempfile.TemporaryDirectory() as tmp_value:
            root = Path(tmp_value)
            store = root / "50RPV" / "scenes_L2A_native" / "zarr" / "cube.zarr"
            records = []
            for group_name, resolution in (("10m", 10), ("20m", 20), ("60m", 60)):
                records.append(
                    {
                        "item_id": "scene-1",
                        "collection_id": "s2grits-scenes-native",
                        "product_type": "scenes",
                        "product_label": f"scenes_L2A_native_{group_name}",
                        "tile_id": "50RPV",
                        "crs": "EPSG:32650",
                        "transform": [
                            float(resolution),
                            0.0,
                            500000.0,
                            0.0,
                            -float(resolution),
                            3600000.0,
                        ],
                        "width": 120 // resolution,
                        "height": 120 // resolution,
                        "datetime": "2025-01-01T00:00:00Z",
                        "zarr_path": relative_asset_path(store, root),
                        "bands": ["blue"],
                        "processing_signature": "p12345678901",
                        "sensor": "sentinel-2",
                        "resolution_group": group_name,
                        "zarr_group": group_name,
                    }
                )
            path = root / "50RPV" / "catalog.parquet"
            upsert_records(path, records)
            table = pq.read_table(path)
            self.assertEqual(table.num_rows, 3)
            rows = table.to_pylist()
            self.assertEqual({row["resolution_group"] for row in rows}, {"10m", "20m", "60m"})
            self.assertTrue(all(row["schema_version"] == SCHEMA_VERSION for row in rows))
            self.assertTrue(all(row["status"] == "complete" for row in rows))
            self.assertTrue(all(not Path(row["zarr_path"]).is_absolute() for row in rows))
            self.assertTrue(all(row["grid_id"] for row in rows))

    def test_asset_outside_global_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_value:
            root = Path(tmp_value)
            with self.assertRaises(ValueError):
                relative_asset_path(root.parent / "outside.zarr", root)


if __name__ == "__main__":
    unittest.main()
