"""Exercise the exporter on three unmodified official TILPAR placemarks."""
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import Point


ROOT = Path(__file__).resolve().parents[1]


def test_official_grid_export(tmp_path):
    source = tmp_path / "sample.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.write(ROOT / "tests/fixtures/mgrs_official_sample.kml", "sample.kml")
    output = tmp_path / "grid.parquet"
    subprocess.run([
        sys.executable, "-B", str(ROOT / "tools/build_mgrs_grid.py"),
        "--source", str(source), "--output", str(output),
    ], check=True, capture_output=True, text=True)
    table = pq.read_table(output)
    rows = {row["tile_id"]: row for row in table.to_pylist()}
    assert list(rows) == ["01CCV", "17MPT", "50SMC"]
    assert rows["50SMC"]["transform"] == [10, 0, 399960, 0, -10, 3800040]
    assert rows["50SMC"]["crs"] == "EPSG:32650"
    assert rows["17MPT"]["transform"] == [10, 0, 600000, 0, -10, 9800020]
    assert rows["17MPT"]["crs"] == "EPSG:32717"
    assert all(row["width"] == row["height"] == 10980 for row in rows.values())
    assert len({row["grid_id"] for row in rows.values()}) == 3
    footprint = from_wkb(rows["01CCV"]["geometry"])
    assert len(footprint.geoms) == 2
    assert footprint.is_valid
    assert footprint.covers(Point(179.9, -72.5))
    assert footprint.covers(Point(-179.9, -72.5))
    assert not footprint.covers(Point(0, -72.5))
    geo = json.loads(table.schema.metadata[b"geo"])
    assert geo["columns"]["geometry"]["crs"]["id"]["code"] == 4326
    assert geo["columns"]["geometry"]["encoding"] == "WKB"
    provenance = json.loads(output.with_suffix(".provenance.json").read_text())
    assert provenance["tile_count"] == 3
    assert provenance["output_bytes"] == output.stat().st_size
