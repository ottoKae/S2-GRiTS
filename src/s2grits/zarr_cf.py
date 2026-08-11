"""CF grid-mapping metadata shared by every S2 native-resolution group."""
from __future__ import annotations

from pyproj import CRS


_AUXILIARY_VARIABLES = frozenset({"time", "x", "y", "spatial_ref"})


def add_cf_grid_mapping(group, crs: str) -> None:
    crs_obj = CRS.from_user_input(crs)
    cf = crs_obj.to_cf()
    if crs_obj.is_geographic:
        x_standard, y_standard, units = "longitude", "latitude", "degrees"
    else:
        x_standard, y_standard, units = (
            "projection_x_coordinate",
            "projection_y_coordinate",
            "metre",
        )

    for name, standard_name, axis in (
        ("x", x_standard, "X"),
        ("y", y_standard, "Y"),
    ):
        coordinate = group[name]
        coordinate.attrs.update(
            {
                "standard_name": standard_name,
                "long_name": f"{name} coordinate of projection",
                "units": units,
                "axis": axis,
            }
        )

    spatial_ref = group.create_array(
        "spatial_ref", shape=(), dtype="int64", overwrite=True, dimension_names=[]
    )
    spatial_ref[...] = 0
    spatial_ref.attrs["_ARRAY_DIMENSIONS"] = []
    spatial_ref.attrs.update(cf)
    if cf.get("crs_wkt"):
        spatial_ref.attrs.setdefault("spatial_ref", cf["crs_wkt"])
    for name in group.keys():
        if name not in _AUXILIARY_VARIABLES and getattr(group[name], "ndim", 0) == 3:
            group[name].attrs["grid_mapping"] = "spatial_ref"
