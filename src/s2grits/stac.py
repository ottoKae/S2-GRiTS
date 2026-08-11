from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from .grid import normalize_mgrs


@dataclass(frozen=True)
class S2Item:
    id: str
    collection: str
    datetime: str
    tile_id: str
    cloud_cover: float | None
    platform: str | None
    bbox: list[float] | None
    geometry: dict[str, Any] | None
    assets: dict[str, dict[str, Any]]
    properties: dict[str, Any]
    links: list[dict[str, Any]]

    @property
    def dt(self) -> datetime:
        return datetime.fromisoformat(self.datetime.replace("Z", "+00:00"))

    @property
    def self_href(self) -> str | None:
        for link in self.links:
            if link.get("rel") == "self" and link.get("href"):
                return str(link["href"])
        return None


def _post_with_retry(
    url: str,
    body: dict[str, Any],
    *,
    timeout: float,
    max_retries: int,
    backoff_seconds: float,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, json=body, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            time.sleep(backoff_seconds * (2**attempt))
    raise RuntimeError(f"Earth Search request failed after {max_retries + 1} attempt(s): {last_exc}") from last_exc


def _next_body(data: dict[str, Any], current_body: dict[str, Any]) -> dict[str, Any] | None:
    for link in data.get("links", []):
        if link.get("rel") == "next":
            if "body" in link:
                return link["body"]
            if "href" in link:
                body = dict(current_body)
                token = link["href"].split("token=")[-1] if "token=" in link["href"] else None
                if token:
                    body["token"] = token
                    return body
    return None


def _tile_from_feature(feature: dict[str, Any]) -> str | None:
    props = feature.get("properties") or {}
    grid_code = props.get("grid:code") or props.get("s2:mgrs_tile")
    if grid_code:
        return normalize_mgrs(grid_code)
    item_id = str(feature.get("id", ""))
    for token in item_id.replace("_", " ").split():
        cleaned = normalize_mgrs(token)
        if len(cleaned) == 5 and cleaned[:2].isdigit():
            return cleaned
        if len(cleaned) == 6 and cleaned[0] == "T" and cleaned[1:3].isdigit():
            return cleaned[1:]
    return None


def feature_to_item(feature: dict[str, Any]) -> S2Item | None:
    props = feature.get("properties") or {}
    tile_id = _tile_from_feature(feature)
    dt = props.get("datetime")
    if not tile_id or not dt:
        return None
    cloud = props.get("eo:cloud_cover")
    return S2Item(
        id=str(feature["id"]),
        collection=str(feature.get("collection", "")),
        datetime=str(dt),
        tile_id=tile_id,
        cloud_cover=float(cloud) if cloud is not None else None,
        platform=props.get("platform"),
        bbox=feature.get("bbox"),
        geometry=feature.get("geometry"),
        assets=feature.get("assets") or {},
        properties=props,
        links=feature.get("links") or [],
    )


def _is_supported_asset_href(asset: dict[str, Any]) -> bool:
    href = str(asset.get("href") or "")
    if not href:
        return False
    scheme = urlparse(href).scheme.lower()
    return scheme in {"http", "https"}


def search_items(
    *,
    stac_api: str,
    collection: str,
    datetime_interval: str,
    tiles: Iterable[str],
    bands: list[str],
    bbox: list[float] | None,
    cloud_cover_max: float,
    page_limit: int,
    timeout: float,
    max_retries: int,
    backoff_seconds: float,
    grid_code: str | None = None,
) -> list[S2Item]:
    url = stac_api.rstrip("/") + "/search"
    tile_set = {normalize_mgrs(t) for t in tiles}

    query: dict[str, Any] = {}
    if grid_code:
        query["grid:code"] = {"eq": f"MGRS-{normalize_mgrs(grid_code)}"}
    if cloud_cover_max < 100:
        query["eo:cloud_cover"] = {"lte": cloud_cover_max}

    body: dict[str, Any] = {
        "collections": [collection],
        "datetime": datetime_interval,
        "limit": int(page_limit),
    }
    if bbox:
        body["bbox"] = bbox
    if query:
        body["query"] = query

    items: dict[str, S2Item] = {}
    while body:
        data = _post_with_retry(
            url,
            body,
            timeout=timeout,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
        )
        for feature in data.get("features", []):
            item = feature_to_item(feature)
            if item is None or item.tile_id not in tile_set:
                continue
            missing = [band for band in bands if band not in item.assets]
            if missing:
                continue
            unsupported = [
                band
                for band in bands
                if not _is_supported_asset_href(item.assets[band])
            ]
            if unsupported:
                continue
            items[item.id] = item
        body = _next_body(data, body)

    return sorted(items.values(), key=lambda item: (item.tile_id, item.datetime, item.id))


def asset_summary_json(item: S2Item, bands: list[str]) -> str:
    doc = {
        band: {
            "href": item.assets[band].get("href"),
            "type": item.assets[band].get("type"),
            "gsd": item.assets[band].get("gsd"),
            "raster:bands": item.assets[band].get("raster:bands"),
            "file:size": item.assets[band].get("file:size"),
            "roles": item.assets[band].get("roles"),
            "title": item.assets[band].get("title"),
        }
        for band in bands
        if band in item.assets
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True)
