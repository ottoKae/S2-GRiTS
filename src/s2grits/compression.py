"""Explicit lossless storage codecs; never change scientific data encoding."""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
from zarr.codecs import BloscCodec


DEFAULT_COMPRESSION = {"cname": "zstd", "clevel": 5, "shuffle": "shuffle"}


def compression_settings(value: Mapping | None = None) -> dict:
    if value is None:
        return dict(DEFAULT_COMPRESSION)
    if not isinstance(value, Mapping):
        raise ValueError("output.zarr_compression must be a mapping.")
    unknown = set(value) - set(DEFAULT_COMPRESSION)
    if unknown:
        raise ValueError(f"Unknown output.zarr_compression key(s): {sorted(map(str, unknown))}")
    settings = {**DEFAULT_COMPRESSION, **value}
    if settings["cname"] != "zstd":
        raise ValueError("output.zarr_compression.cname must be 'zstd'.")
    level = settings["clevel"]
    if type(level) is not int or not 0 <= level <= 9:
        raise ValueError("output.zarr_compression.clevel must be an integer from 0 to 9.")
    if settings["shuffle"] not in ("shuffle", "bitshuffle", "noshuffle"):
        raise ValueError("output.zarr_compression.shuffle must be shuffle, bitshuffle or noshuffle.")
    return settings


def make_compressor(dtype, config: Mapping | None = None) -> BloscCodec:
    # Explicit typesize is essential: byte shuffle must use the element width,
    # not a library-dependent default (uint16 = 2 bytes, uint8 = 1 byte).
    return BloscCodec(typesize=np.dtype(dtype).itemsize, **compression_settings(config))
