from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pytest
import zarr


_spec = importlib.util.spec_from_file_location(
    "recompress_zarr", Path(__file__).parents[1] / "tools" / "recompress_zarr.py"
)
recompress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recompress)


def _files(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.zarr"
    root = zarr.open_group(str(path), mode="w", zarr_format=3)
    root.attrs.update({"tile_id": "50RPV", "source": "STAC", "commit": {"scene": 2}})
    root.create_group("empty").attrs["retained"] = True
    group = root.create_group("10m")
    group.attrs.update({"grid_id": "abc123", "crs": "EPSG:32650", "processing_signature": "sig"})
    pixels = np.array([0, 1, 255, 256, 32767, 32768, 65534, 65535] * 3, dtype="uint16")
    group.create_array("blue", data=pixels.reshape(2, 3, 4), chunks=(1, 2, 3),
                       dimension_names=("time", "y", "x"), fill_value=0,
                       attributes={"nodata": 0, "scale_factor": 0.0001, "source_band": "B02"})
    group.create_array("time", data=np.array(["2025-01-01", "NaT"], dtype="datetime64[ns]"),
                       chunks=(1,), dimension_names=("time",))
    group.create_array("x", data=np.array([500005.0, 500015.0, 500025.0, 500035.0]),
                       chunks=(3,), dimension_names=("x",), attributes={"axis": "X"})
    group.create_array("spatial_ref", data=np.array(0, dtype="int64"), chunks=(),
                       dimension_names=(), attributes={"crs_wkt": "test-wkt"})
    group.create_array("empty_time", shape=(0,), dtype="datetime64[ns]", chunks=(4,))
    group.create_array("unwritten", shape=(5,), dtype="uint16", chunks=(2,), fill_value=65535)
    # Distinct quiet NaN payloads and negative zero must survive byte-for-byte.
    floats = np.array([0x80000000, 0x7FC00001, 0x7FC12345, 0x00000000], dtype="uint32").view("float32")
    group.create_array("float_bits", data=floats, chunks=(1,), fill_value=float("nan"),
                       config={"write_empty_chunks": True})
    group.create_array("negative_zero", data=np.array([-0.0]), chunks=(1,), fill_value=0.0,
                       config={"write_empty_chunks": True})
    for filename in recompress.SIDECARS:
        (path / filename).write_text('{"items": [{"item_id": "scene-1"}], "provenance": "原始"}',
                                     encoding="utf-8")
    return path


def test_copy_preserves_all_bits_metadata_and_sidecars(source, tmp_path):
    original_files = _files(source)
    destination, report = tmp_path / "new.zarr", tmp_path / "receipt.json"
    result = recompress.recompress_store(source, destination, report, source_quiescent=True)
    assert _files(source) == original_files
    assert json.loads(report.read_text("utf-8")) == result
    assert result["status"] == "verified"
    assert result["source_file_manifest_unchanged"]
    assert result["metadata_preserved_except_codecs"]
    assert len(result["arrays"]) == 8
    for entry in result["arrays"]:
        original = zarr.open_array(str(source / entry["path"]), mode="r")
        copied = zarr.open_array(str(destination / entry["path"]), mode="r")
        selection = (...,) if original.ndim else ()
        assert np.asarray(original[selection]).tobytes() == np.asarray(copied[selection]).tobytes()
        assert original.shape == copied.shape
        assert original.chunks == copied.chunks
        assert original.dtype == copied.dtype
        assert original.attrs.asdict() == copied.attrs.asdict()
        codec = copied.metadata.to_dict()["codecs"][-1]
        assert codec == {"name": "blosc", "configuration": {
            "typesize": copied.dtype.itemsize, "cname": "zstd", "clevel": 5,
            "shuffle": "shuffle", "blocksize": 0,
        }}
        assert len(entry["decoded_chunk_order_sha256"]) == 64
    assert not (destination / "10m" / "unwritten" / "c").exists()
    for filename, raw in original_files.items():
        if filename.endswith("zarr.json"):
            old = json.loads(raw)
            new = json.loads((destination / filename).read_text("utf-8"))
            if old["node_type"] == "array":
                new["codecs"] = old["codecs"]
            assert old == new
        elif filename in recompress.SIDECARS:
            assert (destination / filename).read_bytes() == raw


@pytest.mark.parametrize("kind", ["same", "nested", "parent", "existing", "report_source", "report_destination", "report_exists"])
def test_unsafe_destinations_rejected_before_writing(source, tmp_path, kind):
    original_files = _files(source)
    destination, report = tmp_path / "copy.zarr", tmp_path / "receipt.json"
    if kind == "same":
        destination = source
    elif kind == "nested":
        destination = source / "nested"
    elif kind == "parent":
        destination = tmp_path
    elif kind == "existing":
        destination.mkdir()
    elif kind == "report_source":
        report = source / "receipt.json"
    elif kind == "report_destination":
        report = destination / "receipt.json"
    elif kind == "report_exists":
        report.write_text("existing receipt", encoding="utf-8")
    with pytest.raises(ValueError):
        recompress.recompress_store(source, destination, report, source_quiescent=True)
    assert _files(source) == original_files


def test_quiescence_acknowledgement_required(source, tmp_path):
    with pytest.raises(ValueError, match="Stop every source writer"):
        recompress.recompress_store(source, tmp_path / "copy.zarr", tmp_path / "report.json")
    assert not (tmp_path / "copy.zarr").exists()


@pytest.mark.parametrize("unknown", ["extra.txt", "10m/blue/unrecognized", "10m/blue/c/999/0/0"])
def test_unknown_files_rejected_without_losing_them(source, tmp_path, unknown):
    filename = source / unknown
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_bytes(b"must survive")
    with pytest.raises(ValueError, match="Unrecognized|Unknown"):
        recompress.recompress_store(source, tmp_path / "copy.zarr", tmp_path / "report.json",
                                    source_quiescent=True)
    assert filename.read_bytes() == b"must survive"
    assert not (tmp_path / "copy.zarr").exists()


def test_verification_failure_keeps_partial_destination_but_no_receipt(source, tmp_path, monkeypatch):
    original_files = _files(source)

    def fail(*args):
        raise RuntimeError("injected bit mismatch")

    monkeypatch.setattr(recompress, "_verify_chunk", fail)
    with pytest.raises(RuntimeError, match="partial destination retained.*injected bit mismatch"):
        recompress.recompress_store(source, tmp_path / "copy.zarr", tmp_path / "report.json",
                                    source_quiescent=True)
    assert (tmp_path / "copy.zarr").is_dir()
    assert not (tmp_path / "report.json").exists()
    assert _files(source) == original_files


def test_detects_source_mutation_and_does_not_issue_receipt(source, tmp_path, monkeypatch):
    verify = recompress._verify_chunk

    def modify_source(*args):
        (source / "s2grits_resume_state.json").write_text('{"items": []}', encoding="utf-8")
        return verify(*args)

    monkeypatch.setattr(recompress, "_verify_chunk", modify_source)
    with pytest.raises(RuntimeError, match="Source changed during copy"):
        recompress.recompress_store(source, tmp_path / "copy.zarr", tmp_path / "report.json",
                                    source_quiescent=True)
    assert not (tmp_path / "report.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction safety")
def test_junction_source_and_destination_rejected(source, tmp_path):
    import _winapi

    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(source), str(junction))
    with pytest.raises(ValueError, match="junction"):
        recompress.recompress_store(junction, tmp_path / "copy.zarr", tmp_path / "report.json",
                                    source_quiescent=True)
    with pytest.raises(ValueError, match="junction"):
        recompress.recompress_store(source, junction / "copy.zarr", tmp_path / "report.json",
                                    source_quiescent=True)


def test_hardlinked_source_file_rejected(source, tmp_path):
    os.link(source / "s2grits_resume_state.json", tmp_path / "linked.json")
    with pytest.raises(ValueError, match="hard-linked"):
        recompress.recompress_store(source, tmp_path / "copy.zarr", tmp_path / "report.json",
                                    source_quiescent=True)


def test_consolidated_metadata_rejected_without_destination(source, tmp_path):
    filename = source / "zarr.json"
    metadata = json.loads(filename.read_text("utf-8"))
    metadata["consolidated_metadata"] = {"kind": "inline", "metadata": {}}
    filename.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="consolidated"):
        recompress.recompress_store(source, tmp_path / "copy.zarr", tmp_path / "report.json",
                                    source_quiescent=True)
    assert not (tmp_path / "copy.zarr").exists()


@pytest.mark.parametrize("encoding", [{"name": name, "separator": separator}
                                     for name in ("default", "v2") for separator in (".", "/")])
def test_supported_chunk_keys_and_scalar_shape(tmp_path, encoding):
    source = tmp_path / "source.zarr"
    root = zarr.open_group(str(source), mode="w", zarr_format=3)
    root.create_array("scalar", data=np.array(-32768, dtype="int16"), chunks=(),
                       chunk_key_encoding=encoding)
    root.create_array("values", data=np.arange(6, dtype="int32").reshape(2, 3), chunks=(1, 2),
                       chunk_key_encoding=encoding)
    receipt = recompress.recompress_store(source, tmp_path / "copy.zarr", tmp_path / "report.json",
                                          source_quiescent=True)
    assert receipt["status"] == "verified"
    copied = zarr.open_group(str(tmp_path / "copy.zarr"), mode="r")
    assert copied["scalar"][()] == -32768
    np.testing.assert_array_equal(copied["values"][:], np.arange(6).reshape(2, 3))


def test_unsupported_codec_rejected_before_destination(source, tmp_path):
    filename = source / "10m" / "blue" / "zarr.json"
    metadata = json.loads(filename.read_text("utf-8"))
    metadata["codecs"].insert(0, {"name": "quantize", "configuration": {}})
    filename.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported serializer/filter/codec"):
        recompress.recompress_store(source, tmp_path / "copy.zarr", tmp_path / "report.json",
                                    source_quiescent=True)
    assert not (tmp_path / "copy.zarr").exists()


def test_cli_reports_success_and_returns_nonzero_without_acknowledgement(source, tmp_path, capsys):
    arguments = ["--source", str(source), "--destination", str(tmp_path / "copy.zarr"),
                 "--report", str(tmp_path / "receipt.json")]
    assert recompress.main(arguments) == 1
    assert "Stop every source writer" in capsys.readouterr().err
    assert recompress.main([*arguments, "--source-quiescent"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
