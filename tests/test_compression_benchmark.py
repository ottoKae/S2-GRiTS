"""Check benchmark safeguards and bit-exact integer/edge sampling."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest
import zarr
from zarr.codecs import ZstdCodec


_SPEC = importlib.util.spec_from_file_location(
    "benchmark_compression", Path(__file__).parents[1] / "tools" / "benchmark_compression.py"
)
benchmark_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark_module)


def test_rejects_existing_and_source_overlapping_output(tmp_path):
    source = tmp_path / "source.zarr"
    source.mkdir()
    scratch = tmp_path / "scratch"
    with pytest.raises(ValueError, match="overlap"):
        benchmark_module._validate_paths(source, source / "copy", tmp_path / "report.json")
    with pytest.raises(ValueError, match="overlap"):
        benchmark_module._validate_paths(source, scratch, source / "report.json")
    with pytest.raises(ValueError, match="outside"):
        benchmark_module._validate_paths(source, scratch, scratch / "report.json")
    scratch.mkdir()
    with pytest.raises(FileExistsError, match="reuse"):
        benchmark_module._validate_paths(source, scratch, tmp_path / "report.json")
    report = tmp_path / "report.json"
    report.write_text("keep this", encoding="utf-8")
    with pytest.raises(FileExistsError, match="overwrite"):
        benchmark_module._validate_paths(source, tmp_path / "new", report)
    assert report.read_text(encoding="utf-8") == "keep this"


def test_samples_partial_edges_and_deduplicates_small_grids():
    positions = benchmark_module.sample_positions((4, 10980, 10980), (1, 512, 512))
    assert len(positions) == 7
    assert (21, 21) in positions
    assert (11, 21) in positions
    assert (21, 11) in positions
    assert benchmark_module.sample_positions((4, 2, 3), (1, 4, 4)) == [(0, 0)]


def test_disk_roundtrip_with_integer_extremes_fill_and_partial_edges(tmp_path):
    source = tmp_path / "source.zarr"
    root = zarr.open_group(str(source), mode="w-")
    for resolution, name, dtype in (("10m", "blue", "uint16"), ("20m", "scl", "uint8")):
        values = np.arange(3 * 9 * 11).reshape(3, 9, 11).astype(dtype)
        values[:, :4, :4] = 0
        values[:, -1, -1] = np.iinfo(dtype).max
        root.create_array(f"{resolution}/{name}", data=values, chunks=(1, 4, 4),
                          compressors=[ZstdCodec(level=0, checksum=False)], fill_value=0)
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    report = tmp_path / "evidence.json"
    result = benchmark_module.benchmark(source, tmp_path / "scratch", report, scenes=3, repeats=2)
    assert result["totals"]["sample_chunks"] > 0
    assert result["per_stratum"]["edge"]["sample_chunks"] > 0
    assert result["source_metadata_unchanged"]
    assert set(result["reads"]) == {"single_date_chunk", "time_window_64", "time_point"}
    for sample in result["samples"]:
        assert all(sample["bit_exact"].values())
        assert set(sample["sha256_readback"].values()) == {sample["sha256_source"]}
    for path, typesize in (("10m/blue", 2), ("20m/scl", 1)):
        config = result["codecs"][benchmark_module.CANDIDATE][path][0]["configuration"]
        assert config["typesize"] == typesize
        assert config["shuffle"] == "shuffle"
    assert report.exists() and report.with_suffix(".md").exists()
    assert before == {str(p.relative_to(source)): p.read_bytes()
                      for p in source.rglob("*") if p.is_file()}
