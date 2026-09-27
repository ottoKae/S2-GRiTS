"""Copy a QUIESCENT local S2-GRiTS Zarr v3 store with verified lossless compression.

Stop every writer before using this tool and keep the source stopped until it exits.
The source is opened read-only. Existing, nested, linked or junction paths are refused.
This tool never replaces a production store or edits catalog/resolver paths. A failed
run leaves its destination for inspection; only a complete run creates the receipt.

Supported stores use fixed-width numeric/datetime arrays, a bytes serializer and
ordinary lossless codecs. Sharding, consolidated metadata and unknown files are
rejected explicitly, so they cannot disappear unnoticed during a copy.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import stat
import sys
import time
from pathlib import Path

import numpy as np
import zarr
from zarr.codecs import BloscCodec


SIDECARS = frozenset({"s2grits_resume_state.json", "s2grits_metadata.json"})
GROUP_KEYS = frozenset({"zarr_format", "node_type", "attributes", "consolidated_metadata"})
ARRAY_KEYS = frozenset({
    "zarr_format", "node_type", "shape", "data_type", "chunk_grid",
    "chunk_key_encoding", "fill_value", "codecs", "attributes",
    "dimension_names", "storage_transformers",
})


def _checked_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(value))
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise ValueError(f"Symlink/junction paths are unsupported: {component}")
    return path.resolve()


def _snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    """Check every entry without following links; enumeration failures propagate."""
    result = {}

    def scan(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or path.is_junction():
                    raise ValueError(f"Store contains a symlink/junction: {path}")
                # Windows DirEntry.stat may omit link count/inode; Path.stat does not.
                info = path.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    scan(path)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink > 1:
                        raise ValueError(f"Store contains a hard-linked file: {path}")
                    result[path.relative_to(root).as_posix()] = (
                        info.st_size, info.st_mtime_ns, info.st_ino,
                    )
                else:
                    raise ValueError(f"Unsupported store entry: {path}")

    scan(root)
    return result


def _inspect(source: Path, snapshot: dict) -> tuple[dict, dict]:
    """Validate supported metadata and account for every file before creating output."""
    nodes, arrays = {}, {}
    for filename in sorted(snapshot):
        if Path(filename).name != "zarr.json":
            continue
        node = json.loads((source / filename).read_text(encoding="utf-8"))
        relative = Path(filename).parent.as_posix()
        relative = "" if relative == "." else relative
        if node.get("zarr_format") != 3:
            raise ValueError(f"Only Zarr v3 is supported: {filename}")
        kind = node.get("node_type")
        if kind == "group":
            if set(node) - GROUP_KEYS or node.get("consolidated_metadata") is not None:
                raise ValueError(f"Unsupported group/consolidated metadata: {filename}")
        elif kind == "array":
            if set(node) - ARRAY_KEYS or node.get("storage_transformers"):
                raise ValueError(f"Unsupported array metadata: {filename}")
            codecs = node.get("codecs", [])
            if not codecs or codecs[0].get("name") != "bytes" or any(
                codec.get("name") not in {"blosc", "zstd", "gzip", "crc32c"}
                for codec in codecs[1:]
            ):
                raise ValueError(f"Unsupported serializer/filter/codec: {filename}")
            if node.get("chunk_grid", {}).get("name") != "regular":
                raise ValueError(f"Only regular unsharded chunks are supported: {filename}")
            if node.get("chunk_key_encoding", {}).get("name") not in {"default", "v2"}:
                raise ValueError(f"Unsupported chunk key encoding: {filename}")
            array = zarr.open_array(str(source / relative), mode="r")
            if np.dtype(array.dtype).kind not in "biufcMm":
                raise ValueError(f"Unsupported non-numeric dtype: {relative} ({array.dtype})")
            arrays[relative] = array
        else:
            raise ValueError(f"Unsupported node type: {filename}")
        nodes[relative] = node
    if not nodes or nodes.get("", {}).get("node_type") != "group":
        raise ValueError("Source must be a local Zarr v3 group store")
    for name in nodes:
        if name:
            parent = name.rpartition("/")[0]
            if nodes.get(parent, {}).get("node_type") != "group":
                raise ValueError(f"Node has no valid parent group: {name}")
    for filename in snapshot:
        if Path(filename).name == "zarr.json":
            continue
        if filename in SIDECARS:
            # Validate even legacy sidecars, but copy the original bytes unchanged.
            json.loads((source / filename).read_text(encoding="utf-8"))
            continue
        parent = Path(filename).parent
        while parent.as_posix() not in arrays and parent != Path("."):
            parent = parent.parent
        name = parent.as_posix()
        if name not in arrays:
            raise ValueError(f"Unrecognized file (not copied): {filename}")
        array = arrays[name]
        key = Path(filename).relative_to(parent).as_posix()
        encoding = array.metadata.chunk_key_encoding
        try:
            coords = encoding.decode_chunk_key(key)
            # v2 scalar chunks use '0', while the logical coordinate tuple is ().
            if array.ndim == 0 and key == encoding.encode_chunk_key(()):
                coords = ()
            valid = (
                len(coords) == array.ndim
                and encoding.encode_chunk_key(coords) == key
                and all(0 <= index < (size + chunk - 1) // chunk
                        for index, size, chunk in zip(coords, array.shape, array.chunks))
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError(f"Unknown or out-of-range chunk file: {filename}")
    return nodes, arrays


def _chunks(array):
    for coords in itertools.product(*(
        range((size + chunk - 1) // chunk) for size, chunk in zip(array.shape, array.chunks)
    )):
        selection = tuple(slice(i * c, min((i + 1) * c, n))
                          for i, c, n in zip(coords, array.chunks, array.shape))
        yield coords, selection


def _verify_chunk(expected: np.ndarray, actual: np.ndarray, label: str) -> bytes:
    raw = np.asarray(expected).tobytes(order="C")
    if expected.dtype != actual.dtype or expected.shape != actual.shape or (
        raw != np.asarray(actual).tobytes(order="C")
    ):
        raise RuntimeError(f"Bit-exact chunk verification failed: {label}")
    return raw


def recompress_store(
    source: str | Path,
    destination: str | Path,
    report: str | Path,
    *,
    source_quiescent: bool = False,
) -> dict:
    """Copy all logical chunks, verify bytes and preserve metadata; never overwrite."""
    if not source_quiescent:
        raise ValueError("Stop every source writer first; source_quiescent=True is required")
    source, destination, report = map(_checked_path, (source, destination, report))
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must be distinct, non-nested directories")
    if destination.exists():
        raise ValueError(f"Destination already exists; overwrite is forbidden: {destination}")
    if report.exists() or report == source or report == destination or (
        source in report.parents or destination in report.parents
        or report in source.parents or report in destination.parents
    ):
        raise ValueError("Report must be a new file outside both stores")

    started = time.perf_counter()
    before = _snapshot(source)
    nodes, arrays = _inspect(source, before)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for name, node in nodes.items():
            target_node = dict(node)
            if name in arrays:
                target_node["codecs"] = [node["codecs"][0], BloscCodec(
                    cname="zstd", clevel=5, shuffle="shuffle",
                    typesize=np.dtype(arrays[name].dtype).itemsize,
                ).to_dict()]
            metadata_path = destination / name / "zarr.json"
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            with metadata_path.open("x", encoding="utf-8") as stream:
                json.dump(target_node, stream, ensure_ascii=False, indent=2)

        verified = []
        for name, original in arrays.items():
            # Explicitly writing stored fill chunks preserves signed zeros/NaN payloads.
            # Zarr 3.3 open_array ignores creation-only config kwargs on existing
            # arrays. Set the default during open so its ArrayConfig captures it.
            with zarr.config.set({"array.write_empty_chunks": True}):
                target = zarr.open_array(str(destination / name), mode="r+")
            digest, chunk_count, byte_count = hashlib.sha256(), 0, 0
            for coords, selection in _chunks(original):
                expected = np.asarray(original[selection])
                key = original.metadata.chunk_key_encoding.encode_chunk_key(coords)
                if f"{name}/{key}" in before:
                    target[selection] = expected
                actual = np.asarray(target[selection])
                raw = _verify_chunk(expected, actual, f"{name}:{coords}")
                digest.update(raw)
                chunk_count += 1
                byte_count += len(raw)
            verified.append({"path": name, "dtype": str(original.dtype),
                             "shape": list(original.shape), "chunks": list(original.chunks),
                             "verified_chunks": chunk_count, "logical_bytes": byte_count,
                             "decoded_chunk_order_sha256": digest.hexdigest()})
        for filename in sorted(SIDECARS & before.keys()):
            payload = (source / filename).read_bytes()
            with (destination / filename).open("xb") as stream:
                stream.write(payload)
            if (destination / filename).read_bytes() != payload:
                raise RuntimeError(f"Sidecar verification failed: {filename}")
        if before != _snapshot(source):
            raise RuntimeError("Source changed during copy; destination is NOT verified")
        for name, original_node in nodes.items():
            target_node = json.loads((destination / name / "zarr.json").read_text("utf-8"))
            if name in arrays:
                target_node["codecs"] = original_node["codecs"]
            if target_node != original_node:
                raise RuntimeError(f"Metadata verification failed: {name}")
        after = _snapshot(destination)
        source_bytes = sum(value[0] for value in before.values())
        destination_bytes = sum(value[0] for value in after.values())
        receipt = {
            "status": "verified", "source": str(source), "destination": str(destination),
            "source_quiescent_asserted": True, "source_file_manifest_unchanged": True,
            "metadata_preserved_except_codecs": True,
            "verification": "Every logical chunk compared dtype, shape and C-order bytes",
            "compression": {"name": "blosc", "cname": "zstd", "clevel": 5,
                            "shuffle": "shuffle", "typesize": "dtype.itemsize"},
            "source_bytes": source_bytes, "destination_bytes": destination_bytes,
            "source_to_destination_ratio": source_bytes / destination_bytes,
            "arrays": verified, "sidecars": sorted(SIDECARS & before.keys()),
            "elapsed_seconds": time.perf_counter() - started,
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
        return receipt
    except Exception as exc:
        raise RuntimeError(
            f"Recompression failed; partial destination retained at {destination}. "
            f"Do not use it as a verified copy. This tool did not modify the source. {exc}"
        ) from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--source-quiescent", action="store_true",
                        help="Confirm every source writer is stopped for the whole copy")
    args = parser.parse_args(argv)
    try:
        receipt = recompress_store(args.source, args.destination, args.report,
                                   source_quiescent=args.source_quiescent)
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": receipt["status"], "report": str(args.report),
                      "source_to_destination_ratio": receipt["source_to_destination_ratio"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
