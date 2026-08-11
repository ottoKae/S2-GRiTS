"""Sentinel-2 tiled downloader and Zarr builder."""

from .runtime import configure_gdal_data

configure_gdal_data()

from .__version__ import __version__

__all__ = ["__version__"]
