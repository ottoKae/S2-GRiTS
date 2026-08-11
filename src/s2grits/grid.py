from __future__ import annotations

from pathlib import Path

import shapefile
from shapely.geometry import shape as shapely_shape
from shapely.ops import unary_union


JIANGSU_S1_MGRS_TILES = [
    "50RPV", "50RQV", "50SMC", "50SMD", "50SNB", "50SNC", "50SND",
    "50SPA", "50SPB", "50SPC", "50SPD", "50SQA", "50SQB", "50SQC",
    "50SQD", "50SQE", "51RTQ", "51RUQ", "51STR", "51STS", "51STT",
    "51STU", "51STV", "51SUR", "51SUS", "51SVR",
]


def normalize_mgrs(tile: str) -> str:
    return str(tile).upper().replace("MGRS-", "").strip()


def read_shapefile_geometry(path: str | Path):
    shp_path = Path(path)
    reader = shapefile.Reader(str(shp_path), encoding="utf-8")
    geoms = [shapely_shape(shp.__geo_interface__) for shp in reader.shapes()]
    if not geoms:
        raise ValueError(f"No geometry found in {shp_path}")
    return unary_union(geoms)


def enumerate_mgrs_tiles(cfg: dict) -> list[str]:
    roi = cfg.get("roi") or {}
    manual = [normalize_mgrs(t) for t in (roi.get("manual_mgrs_tiles") or []) if str(t).strip()]
    if manual:
        return sorted(dict.fromkeys(manual))

    shp_path = roi.get("shp_path")
    if not shp_path:
        raise ValueError("roi.manual_mgrs_tiles or roi.shp_path is required.")

    geom = read_shapefile_geometry(shp_path)

    try:
        from s1grits.mgrs_burst_data import get_mgrs_table
    except Exception as exc:
        raise RuntimeError(
            "Automatic MGRS enumeration needs s1grits.mgrs_burst_data in this "
            "environment. Fill roi.manual_mgrs_tiles to avoid this dependency."
        ) from exc

    mgrs = get_mgrs_table()
    selected = mgrs[mgrs.intersects(geom)].copy()
    if "mgrs_tile_id" not in selected.columns:
        raise ValueError("S1-GRiTS MGRS table is missing 'mgrs_tile_id'.")
    return sorted(normalize_mgrs(t) for t in selected["mgrs_tile_id"].tolist())


def roi_bbox(cfg: dict) -> list[float] | None:
    roi = cfg.get("roi") or {}
    if not roi.get("shp_path"):
        return None
    geom = read_shapefile_geometry(roi["shp_path"])
    minx, miny, maxx, maxy = geom.bounds
    return [float(minx), float(miny), float(maxx), float(maxy)]
