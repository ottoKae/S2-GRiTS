from __future__ import annotations

from collections import OrderedDict


NATIVE_BAND_GROUPS: dict[str, tuple[str, ...]] = OrderedDict(
    {
        "10m": ("blue", "green", "red", "nir"),
        "20m": (
            "rededge1",
            "rededge2",
            "rededge3",
            "nir08",
            "swir16",
            "swir22",
            "scl",
            "cloud",
        ),
        "60m": ("coastal", "nir09"),
    }
)

BAND_LABELS = {
    "coastal": "B01",
    "blue": "B02",
    "green": "B03",
    "red": "B04",
    "rededge1": "B05",
    "rededge2": "B06",
    "rededge3": "B07",
    "nir": "B08",
    "nir08": "B8A",
    "nir09": "B09",
    "swir16": "B11",
    "swir22": "B12",
    "scl": "SCL",
    "cloud": "CLD",
}

NATIVE_BANDS = tuple(
    band for group_bands in NATIVE_BAND_GROUPS.values() for band in group_bands
)


def selected_native_groups(bands: list[str]) -> dict[str, list[str]]:
    selected = set(bands)
    return {
        group: [band for band in group_bands if band in selected]
        for group, group_bands in NATIVE_BAND_GROUPS.items()
        if any(band in selected for band in group_bands)
    }


def resolution_metres(group_name: str) -> int:
    if not group_name.endswith("m"):
        raise ValueError(f"Invalid native resolution group: {group_name}")
    return int(group_name[:-1])


def display_bands(bands: list[str]) -> str:
    return ", ".join(f"{BAND_LABELS.get(band, band)}({band})" for band in bands)
