from pathlib import Path

from s2grits.bands import BAND_LABELS, NATIVE_BANDS, variable_name
from s2grits.config import apply_defaults, output_path


def test_stable_semantic_names_and_source_band_ids() -> None:
    expected = {
        "blue": ("blue", "B02"),
        "green": ("green", "B03"),
        "red": ("red", "B04"),
        "nir": ("nir_broad", "B08"),
        "nir08": ("nir_narrow", "B8A"),
        "swir16": ("swir_1", "B11"),
        "swir22": ("swir_2", "B12"),
        "rededge1": ("red_edge_1", "B05"),
    }
    assert set(expected).issubset(NATIVE_BANDS)
    for source_asset, (variable, source_band) in expected.items():
        assert variable_name(source_asset) == variable
        assert BAND_LABELS[source_asset] == source_band

    assert variable_name("rededge1") != "nir_narrow"


def test_auxiliary_paths_are_relative_to_dataset_root() -> None:
    cfg = {"output": {"base_dir": "D:/datasets/S2-test"}}
    apply_defaults(cfg)
    assert cfg["output"]["logs_dir"] == "../logs"
    assert cfg["output"]["cache_dir"] == "../_cache"
    assert output_path(cfg, "logs_dir") == Path("D:/datasets/S2-test/../logs")
    assert output_path(cfg, "cache_dir") == Path("D:/datasets/S2-test/../_cache")
