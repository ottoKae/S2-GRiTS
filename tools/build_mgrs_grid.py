"""Build the global 10 m Sentinel-2 grid index from the official TILPAR ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import CRS
from shapely import from_wkt
from shapely.geometry import MultiPolygon, Polygon, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from s2grits.canonical_catalog_schema import make_grid_id


SOURCE_URL = (
    "https://sentiwiki.copernicus.eu/__attachments/"
    "a_69a662aa1cb30487acc66009f09b1dcfa9e4e32f92af1c3b2dd1fc8a3c011738/"
    "S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_21000101T000000_B00.zip"
)
NS = {"k": "http://www.opengis.net/kml/2.2"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Official TILPAR ZIP")
    parser.add_argument("--output", required=True, type=Path, help="Output GeoParquet")
    args = parser.parse_args()
    rows = read_tiles(args.source)
    table = pa.Table.from_pylist(rows, schema=pa.schema([
        ("tile_id", pa.string()),
        ("crs", pa.string()),
        ("utm_epsg", pa.int32()),
        ("transform", pa.list_(pa.float64(), 6)),
        ("width", pa.int32()),
        ("height", pa.int32()),
        ("resolution_x", pa.float64()),
        ("resolution_y", pa.float64()),
        ("grid_id", pa.string()),
        ("grid_version", pa.int32()),
        ("utm_wkt", pa.string()),
        ("geometry", pa.binary()),
    ]))
    provenance = {
        "source_url": SOURCE_URL,
        "source_file": args.source.name,
        "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
        "tile_count": len(rows),
        "scope": "All tiles in the official 2015-12-09 TILPAR, not scene availability",
        "transform_order": "Rasterio/Affine [a,b,c,d,e,f], pixel upper-left corner",
        "geometry_source": "Official KML polygons, including antimeridian splits",
        "grid_source": "Official UTM_WKT bounds and EPSG; 10 m pixel spacing",
        "grid_id_contract": "S2-GRiTS canonical v7, grid version 1, resolution=[10,10]",
    }
    geo = {
        "version": "1.0.0",
        "primary_column": "geometry",
        "columns": {"geometry": {
            "encoding": "WKB",
            "geometry_types": ["MultiPolygon"],
            "crs": CRS.from_epsg(4326).to_json_dict(),
            "edges": "planar",
        }},
    }
    table = table.replace_schema_metadata({
        b"geo": json.dumps(geo).encode(),
        b"s2grits:grid": json.dumps(provenance).encode(),
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, args.output, compression="zstd", row_group_size=4096)
    restored = pq.read_table(args.output)
    if not table.equals(restored):
        raise ValueError("Parquet round-trip differs from source records")
    provenance["output_sha256"] = hashlib.sha256(args.output.read_bytes()).hexdigest()
    provenance["output_bytes"] = args.output.stat().st_size
    args.output.with_suffix(".provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(provenance, indent=2), flush=True)


def read_tiles(source: Path) -> list[dict]:
    rows = []
    with zipfile.ZipFile(source) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".kml")]
        if len(members) != 1:
            raise ValueError("Expected exactly one KML in the official archive")
        with archive.open(members[0]) as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag == f"{{{NS['k']}}}Placemark":
                    rows.append(tile_record(element))
                    element.clear()
    ids = [row["tile_id"] for row in rows]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Empty or duplicate tile IDs in source")
    return sorted(rows, key=lambda row: row["tile_id"])


def tile_record(element: ET.Element) -> dict:
    tile = element.findtext("k:name", namespaces=NS)
    description = element.findtext("k:description", namespaces=NS)
    cells = re.findall(r"<tr>(.*?)</tr>", description, flags=re.DOTALL)
    properties = {}
    for cell in cells:
        values = re.findall(r"<td\b[^>]*>(.*?)</td>", cell, flags=re.DOTALL)
        key, value = [re.sub(r"<[^>]+>", "", v).strip() for v in values]
        properties[key] = value
    if not re.fullmatch(r"\d{2}[C-HJ-NP-X][A-HJ-NP-Z]{2}", tile):
        raise ValueError(f"Invalid tile ID: {tile}")
    epsg = int(properties["EPSG"])
    expected_epsg = (32600 if tile[2] >= "N" else 32700) + int(tile[:2])
    native = from_wkt(properties["UTM_WKT"])
    xmin, ymin, xmax, ymax = native.bounds
    if (properties["TILE_ID"] != tile or epsg != expected_epsg
            or not native.equals(box(xmin, ymin, xmax, ymax))
            or xmax - xmin != 109800 or ymax - ymin != 109800
            or any(v % 10 != 0 for v in native.bounds)):
        raise ValueError(f"Unexpected official grid definition: {tile}")
    polygons = []
    for polygon in element.findall(".//k:Polygon", NS):
        rings = []
        for tag in ("outerBoundaryIs", "innerBoundaryIs"):
            for node in polygon.findall(f"k:{tag}/k:LinearRing/k:coordinates", NS):
                rings.append([tuple(map(float, p.split(",")[:2]))
                              for p in node.text.split()])
        polygons.append(Polygon(rings[0], rings[1:]))
    geometry = MultiPolygon(polygons)
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError(f"Invalid KML footprint: {tile}")
    transform = [10.0, 0.0, xmin, 0.0, -10.0, ymax]
    crs = f"EPSG:{epsg}"
    return {
        "tile_id": tile, "crs": crs, "utm_epsg": epsg,
        "transform": transform, "width": 10980, "height": 10980,
        "resolution_x": 10.0, "resolution_y": 10.0,
        "grid_id": make_grid_id(tile, crs, transform, 10980, 10980, [10, 10]),
        "grid_version": 1, "utm_wkt": properties["UTM_WKT"],
        "geometry": geometry.wkb,
    }


if __name__ == "__main__":
    main()
