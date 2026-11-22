"""Source-declared Sentinel-2 track and acquisition identity, without mosaicking."""
from __future__ import annotations

import re


def relative_orbit_number(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,3}", str(value)):
        raise ValueError("Sentinel-2 relative orbit must be an integer from 1 to 143")
    number = int(value)
    if not 1 <= number <= 143:
        raise ValueError("Sentinel-2 relative orbit must be an integer from 1 to 143")
    return number


def scene_identity(item) -> dict:
    """Read STAC orbit or the official product URI; preserve unknown legacy fields.

    A datatake key follows GRiTS-Resolver: satellite + start + absolute orbit.
    Relative orbit, tile timestamp and calendar date are not acquisition keys.
    """
    properties = item.properties
    orbit = relative_orbit_number(properties.get("sat:relative_orbit"))
    product_uri = properties.get("s2:product_uri")
    product = None
    if product_uri:
        product = re.fullmatch(
            r"(S2[A-Z])_MSIL(?:1C|2A)_(\d{8}T\d{6})_N\d{4}_R(\d{3})_"
            r"T(\d{2}[A-Z]{3})_\d{8}T\d{6}(?:\.SAFE)?", str(product_uri)
        )
        if not product or product[4] != item.tile_id:
            raise ValueError("Unsupported Sentinel-2 product URI or mismatching tile")
        uri_orbit = relative_orbit_number(product[3])
        if orbit is not None and orbit != uri_orbit:
            raise ValueError("STAC relative orbit and product URI disagree")
        orbit = uri_orbit
    datatake_id = properties.get("s2:datatake_id")
    datatake = None
    if datatake_id:
        datatake = re.fullmatch(
            r"G(S2[A-Z])_(\d{8}T\d{6})_(\d{6})_N\d{2}\.\d{2}", str(datatake_id)
        )
        if not datatake:
            raise ValueError("Unsupported Sentinel-2 datatake identity")
        if product and datatake.group(1, 2) != product.group(1, 2):
            raise ValueError("Product URI and datatake identity disagree")
    for identity in (product, datatake):
        if identity and item.platform and str(item.platform).lower() != (
            "sentinel-2" + identity[1][-1].lower()
        ):
            raise ValueError("Platform and Sentinel-2 acquisition identity disagree")
    state = properties.get("sat:orbit_state")
    if state is not None and state not in {"ascending", "descending"}:
        raise ValueError("Unsupported Sentinel-2 orbit state")
    return {
        "collection": item.collection,
        "platform": item.platform,
        "relative_orbit": orbit,
        "orbit_state": state,
        "product_uri": product_uri,
        "datatake_id": datatake_id,
        "acquisition_id": "_".join(datatake.group(1, 2, 3)) if datatake else None,
    }
