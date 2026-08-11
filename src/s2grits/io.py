from __future__ import annotations

from pathlib import Path

import requests


def download_file(url: str, out_path: Path, *, timeout: float = 90) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    tmp.replace(out_path)
    return out_path


def save_thumbnail(item, out_path: Path, *, timeout: float = 60) -> Path | None:
    asset = item.assets.get("thumbnail") or item.assets.get("preview")
    href = asset.get("href") if asset else None
    if not href:
        return None
    return download_file(href, out_path, timeout=timeout)
