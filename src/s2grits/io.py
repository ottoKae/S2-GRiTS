from __future__ import annotations

from pathlib import Path

import requests


def download_file(
    url: str, out_path: Path, *, timeout: float = 90, max_bytes: int | None = None
) -> Path:
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise ValueError("max_bytes must be a positive integer or None.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        declared_bytes = None
        if max_bytes is not None:
            length = resp.headers.get("Content-Length")
            if length is not None:
                declared_bytes = int(length)
                if declared_bytes < 0 or declared_bytes > max_bytes:
                    raise ValueError("Content-Length exceeds max_bytes or is negative.")
        received_bytes = 0
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    received_bytes += len(chunk)
                    if max_bytes is not None and received_bytes > max_bytes:
                        raise ValueError("Downloaded data exceeds max_bytes.")
                    f.write(chunk)
        if declared_bytes is not None and received_bytes != declared_bytes:
            raise ValueError("Downloaded byte count differs from Content-Length.")
    tmp.replace(out_path)
    return out_path


def save_thumbnail(item, out_path: Path, *, timeout: float = 60) -> Path | None:
    asset = item.assets.get("thumbnail") or item.assets.get("preview")
    href = asset.get("href") if asset else None
    if not href:
        return None
    return download_file(href, out_path, timeout=timeout)
