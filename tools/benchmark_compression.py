"""Read-only source sampling and disk-backed lossless codec comparison.

Example (use a NEW scratch directory and report filename):
    python -B tools/benchmark_compression.py --source SOURCE.zarr \
        --scratch SCRATCH_RUN --report evidence/compression.json

Only deterministic sample chunks are copied. Sparse scratch stores retain the
source shape/chunks but are NOT complete datasets and must not be used as such.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numcodecs
import numpy as np
import zarr
from zarr.codecs import BloscCodec, ZstdCodec


BASELINE = "zstd_level0"
CANDIDATE = "blosc_zstd5_byte_shuffle"


def sample_positions(shape: tuple, chunks: tuple) -> list[tuple[int, int]]:
    """Fixed spatial strata, including the last row, column and corner."""
    ny, nx = ((shape[i] + chunks[i] - 1) // chunks[i] for i in (1, 2))
    return sorted({
        (0, 0), (ny // 4, nx // 4), (ny // 2, nx // 2),
        (3 * ny // 4, nx // 4), (ny // 2, nx - 1),
        (ny - 1, nx // 2), (ny - 1, nx - 1),
    })


def _hash(array: np.ndarray) -> str:
    return hashlib.sha256(array.tobytes(order="C")).hexdigest()


def _chunk_bytes(store: Path, array, coords: tuple[int, ...]) -> int:
    path = store / array.path / array.metadata.encode_chunk_key(coords)
    return path.stat().st_size if path.exists() else 0


def _validate_paths(source: Path, scratch: Path, report: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"Source must be an existing local Zarr directory: {source}")
    if scratch.exists():
        raise FileExistsError(f"Refusing to reuse scratch directory: {scratch}")
    for output in (scratch, report, report.with_suffix(".md")):
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError(f"Output must not overlap the read-only source: {output}")
    if report == scratch or scratch.is_relative_to(report):
        raise ValueError("Report file cannot contain the scratch directory")
    if report.is_relative_to(scratch):
        raise ValueError("Keep durable report evidence outside the disposable scratch directory")
    if report.suffix.lower() != ".json":
        raise ValueError("Report must have a .json suffix; a sibling .md is also written")
    for path in (report, report.with_suffix(".md")):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite report: {path}")


def _aggregate(records: list[dict]) -> dict:
    raw = sum(r["raw_valid_bytes"] for r in records)
    baseline = sum(r["encoded_bytes"][BASELINE] for r in records)
    candidate = sum(r["encoded_bytes"][CANDIDATE] for r in records)
    return {
        "sample_chunks": len(records),
        "raw_valid_bytes": raw,
        "raw_padded_chunk_bytes": sum(r["raw_padded_chunk_bytes"] for r in records),
        "source_existing_encoded_bytes": sum(r["source_encoded_bytes"] for r in records),
        "baseline_encoded_bytes": baseline,
        "candidate_encoded_bytes": candidate,
        "raw_to_baseline_ratio": raw / baseline if baseline else None,
        "raw_to_candidate_ratio": raw / candidate if candidate else None,
        "baseline_to_candidate_ratio": baseline / candidate if candidate else None,
        "reduction_vs_baseline_percent": 100 * (1 - candidate / baseline) if baseline else None,
        "bit_exact_chunks_per_codec": {
            label: sum(r["bit_exact"][label] for r in records)
            for label in (BASELINE, CANDIDATE)
        },
    }


def _markdown(report: dict) -> str:
    total = report["totals"]
    def ratio(value):
        return f"{value:.3f}x" if value is not None else "n/a (all fill)"

    saving = total["reduction_vs_baseline_percent"]
    lines = [
        "# S2-GRiTS real-data lossless compression benchmark", "",
        f"UTC: {report['created_utc']}", "",
        f"Read-only source: `{report['source']}`", "",
        f"Scratch sample copies: `{report['scratch']}`", "",
        "Baseline: Zarr v3 Zstd level 0. Candidate: Blosc + Zstd level 5 + byte shuffle, "
        "with explicit `typesize=dtype.itemsize`. Source grid, dtype, chunk shape and fill "
        "values are retained; edge chunks use Zarr's normal padding.", "",
        f"{report['band_count']} bands; scene indices {report['scene_indices']}; "
        f"{total['sample_chunks']} sampled chunks. Every sampled chunk was read back from "
        "both disk-backed copies and checked by byte comparison and SHA-256.", "",
        "| Measurement | Result |", "|---|---:|",
        f"| Valid raw bytes | {total['raw_valid_bytes']:,} |",
        f"| Raw padded chunk bytes | {total['raw_padded_chunk_bytes']:,} |",
        f"| Baseline encoded chunk bytes | {total['baseline_encoded_bytes']:,} |",
        f"| Candidate encoded chunk bytes | {total['candidate_encoded_bytes']:,} |",
        f"| Raw / candidate | {ratio(total['raw_to_candidate_ratio'])} |",
        f"| Baseline / candidate | {ratio(total['baseline_to_candidate_ratio'])} |",
        f"| Reduction from baseline | {f'{saving:.2f}%' if saving is not None else 'n/a'} |", "",
        "## Interior and edge samples", "",
        "Raw ratios can be dominated by fill-only chunks. Inspect interior and edge "
        "strata separately; neither is weighted to estimate the complete archive.", "",
        "| Stratum | Chunks | Raw / candidate | Baseline / candidate |",
        "|---|---:|---:|---:|",
        *[f"| {name} | {row['sample_chunks']} | {ratio(row['raw_to_candidate_ratio'])} | "
          f"{ratio(row['baseline_to_candidate_ratio'])} |"
          for name, row in report["per_stratum"].items()], "",
        "## Per-band sampled payloads", "",
        "| Band | Raw bytes | Baseline bytes | Candidate bytes | Baseline / candidate |",
        "|---|---:|---:|---:|---:|",
    ]
    for band, row in report["per_band"].items():
        band_ratio = row["baseline_to_candidate_ratio"]
        lines.append(
            f"| {band} | {row['raw_valid_bytes']:,} | {row['baseline_encoded_bytes']:,} | "
            f"{row['candidate_encoded_bytes']:,} | {band_ratio:.3f}x |"
            if band_ratio is not None else f"| {band} | all fill | 0 | 0 | n/a |"
        )
    lines += [
        "", "## Warm disk-backed Zarr reads", "",
        "Each timed batch visits every sampled band and spatial stratum; window and "
        "point queries request all sampled dates. One warm-up pass per codec/pattern "
        "is excluded, then codec order alternates between repetitions. Array opening, "
        "source reads, verification and report generation are excluded from timings.", "",
        "| Pattern | Requests/batch | Returned bytes/batch | Baseline median ms | "
        "Candidate median ms | Baseline / candidate time |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for pattern, row in report["reads"].items():
        old, new = (row["median_seconds"][label] for label in (BASELINE, CANDIDATE))
        lines.append(
            f"| {pattern} | {row['requests_per_batch']} | {row['returned_bytes_per_batch']:,} | "
            f"{1000 * old:.2f} | {1000 * new:.2f} | {old / new:.3f}x |"
        )
    lines += ["", "## Limits", ""] + [f"- {item}" for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def benchmark(source: Path, scratch: Path, report: Path, *, scenes: int = 4,
              repeats: int = 5) -> dict:
    source, scratch, report = (p.resolve() for p in (source, scratch, report))
    _validate_paths(source, scratch, report)
    if scenes < 1 or repeats < 2:
        raise ValueError("scenes must be positive and repeats must be at least 2")
    root = zarr.open_group(str(source), mode="r")
    arrays = {}
    for resolution in ("10m", "20m", "60m"):
        if resolution in root:
            for name, array in root[resolution].arrays():
                if array.ndim == 3:
                    arrays[f"{resolution}/{name}"] = array
    arrays = dict(sorted(arrays.items()))
    if not arrays:
        raise ValueError("No native-resolution 3D band arrays found")
    scene_count = min(a.shape[0] for a in arrays.values())
    if not scene_count:
        raise ValueError("Source contains no scenes")
    scene_indices = np.unique(np.linspace(0, scene_count - 1, min(scenes, scene_count),
                                         dtype=int)).tolist()
    for path, array in arrays.items():
        if array.chunks[0] != 1 or array.dtype.kind not in "ui":
            raise ValueError(f"Expected integer bands with time chunk 1: {path}")
        if array.compressors != (ZstdCodec(level=0, checksum=False),):
            raise ValueError(f"Source is not the expected Zstd level 0 baseline: {path}")
        if array.filters:
            raise ValueError(f"Unexpected source filters: {path}")
    metadata_files = [source / "zarr.json"] + [source / path / "zarr.json" for path in arrays]
    metadata_before = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in metadata_files}
    scratch.mkdir(parents=True, exist_ok=False)
    (scratch / "SAMPLED_STORES_ONLY.txt").write_text(
        "These sparse Zarr copies contain ONLY the chunks listed in the benchmark report.\n"
        "They are not complete scientific datasets. No source files were modified.\n",
        encoding="utf-8",
    )
    stores = {label: scratch / f"{label}.zarr" for label in (BASELINE, CANDIDATE)}
    roots = {label: zarr.open_group(str(path), mode="w-") for label, path in stores.items()}
    copies = {label: {} for label in stores}
    records, queries = [], {"single_date_chunk": [], "time_window_64": [], "time_point": []}
    for path, array in arrays.items():
        for label in stores:
            codec = (ZstdCodec(level=0, checksum=False) if label == BASELINE else
                     BloscCodec(cname="zstd", clevel=5, shuffle="shuffle",
                                typesize=array.dtype.itemsize))
            copies[label][path] = roots[label].create_array(
                path, shape=array.shape, chunks=array.chunks, dtype=array.dtype,
                fill_value=array.fill_value, compressors=[codec],
                dimension_names=array.metadata.dimension_names,
                config={"write_empty_chunks": False},
            )
        for cy, cx in sample_positions(array.shape, array.chunks):
            y, x = cy * array.chunks[1], cx * array.chunks[2]
            y1, x1 = min(y + array.chunks[1], array.shape[1]), min(x + array.chunks[2], array.shape[2])
            window = (slice(y, min(y + 64, y1)), slice(x, min(x + 64, x1)))
            queries["single_date_chunk"].append((path, (scene_indices[0], slice(y, y1), slice(x, x1))))
            queries["time_window_64"].append((path, (scene_indices, *window)))
            queries["time_point"].append((path, (scene_indices, y, x)))
            for scene in scene_indices:
                selection = (slice(scene, scene + 1), slice(y, y1), slice(x, x1))
                data = np.asarray(array[selection])
                row = {
                    "band": path, "scene_index": scene, "chunk_coords": [scene, cy, cx],
                    "window": [y, y1, x, x1], "dtype": str(data.dtype),
                    "raw_valid_bytes": data.nbytes,
                    "raw_padded_chunk_bytes": int(np.prod(array.chunks)) * array.dtype.itemsize,
                    "edge_chunk": data.shape != array.chunks,
                    "fill_fraction": float(np.mean(data == array.fill_value)),
                    "sha256_source": _hash(data),
                    "source_encoded_bytes": _chunk_bytes(source, array, (scene, cy, cx)),
                    "encoded_bytes": {}, "sha256_readback": {}, "bit_exact": {},
                }
                for label, targets in copies.items():
                    target = targets[path]
                    target[selection] = data
                    readback = np.asarray(target[selection])
                    row["bit_exact"][label] = (readback.dtype == data.dtype and
                                                readback.tobytes() == data.tobytes())
                    row["sha256_readback"][label] = _hash(readback)
                    row["encoded_bytes"][label] = _chunk_bytes(stores[label], target, (scene, cy, cx))
                    if not row["bit_exact"][label]:
                        raise AssertionError(f"Lossless check failed: {label} {path} {selection}")
                records.append(row)
        print(f"sampled {path}: {len(sample_positions(array.shape, array.chunks)) * len(scene_indices)} "
              "chunks, both codec readbacks bit-exact", flush=True)

    # Reopen local stores for access tests. No in-memory array/chunk cache is used;
    # filesystem pages may be warm and are intentionally not flushed.
    readers = {label: zarr.open_group(str(path), mode="r") for label, path in stores.items()}
    read_arrays = {label: {path: group[path] for path in arrays} for label, group in readers.items()}
    read_results = {}
    for pattern, requests in queries.items():
        def batch(label):
            count = 0
            for path, selection in requests:
                value = np.asarray(read_arrays[label][path].oindex[selection])
                count += value.nbytes
            return count

        bytes_returned = {label: batch(label) for label in stores}
        durations = {label: [] for label in stores}
        for repetition in range(repeats):
            order = list(stores) if repetition % 2 == 0 else list(reversed(stores))
            for label in order:
                start = time.perf_counter()
                batch(label)
                durations[label].append(time.perf_counter() - start)
        if len(set(bytes_returned.values())) != 1:
            raise AssertionError("Read patterns returned unequal data sizes")
        read_results[pattern] = {
            "requests_per_batch": len(requests),
            "returned_bytes_per_batch": bytes_returned[BASELINE],
            "seconds_per_batch": durations,
            "median_seconds": {label: statistics.median(values) for label, values in durations.items()},
        }
        print(f"timed {pattern}: {len(requests)} requests/batch, {repeats} warm repetitions", flush=True)

    metadata_after = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in metadata_files}
    if metadata_before != metadata_after:
        raise RuntimeError("Source metadata changed during the benchmark; discard these results")
    result = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source), "scratch": str(scratch), "command": sys.argv,
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "zarr": zarr.__version__, "numpy": np.__version__,
                        "numcodecs": numcodecs.__version__,
                        "zarr_async_concurrency": zarr.config.get("async.concurrency"),
                        "blosc_threads": numcodecs.blosc.get_nthreads()},
        "source_metadata_sha256": metadata_before,
        "source_metadata_unchanged": True,
        "band_count": len(arrays), "source_scene_count": scene_count,
        "scene_indices": scene_indices,
        "scene_times": ([str(value) for value in root["time"][scene_indices]] if "time" in root else []),
        "sampling": "Seven deterministic chunk-index strata per band, including three edges; all "
                    "requested scenes. Fixed equal chunk counts per band are NOT area/byte weights.",
        "codecs": {label: {path: [c.to_dict() for c in target.compressors]
                           for path, target in targets.items()} for label, targets in copies.items()},
        "totals": _aggregate(records),
        "per_band": {path: _aggregate([row for row in records if row["band"] == path]) for path in arrays},
        "per_stratum": {name: _aggregate([row for row in records if row["edge_chunk"] == edge])
                        for name, edge in (("interior", False), ("edge", True))},
        "scratch_store_file_bytes": {label: sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
                                     for label, path in stores.items()},
        "reads": read_results, "repeats": repeats, "samples": records,
        "limitations": [
            "This is a deterministic, bounded sample of one tile and a few dates, not a probability "
            "sample or a whole-dataset compression estimate. Edge and low-resolution bands are "
            "deliberately represented; their proportions differ from the full archive.",
            "Compression ratios count encoded chunk payload bytes. Metadata, directory entries, "
            "filesystem allocation, COG cache and other products are excluded. Logical raw bytes "
            "exclude edge padding; padded raw bytes are reported separately.",
            "The baseline already uses Zstd level 0. A raw/candidate ratio above 2 does not establish "
            "a twofold improvement over existing compressed storage. Ratios are data-dependent.",
            "Read timings use disk-backed Zarr arrays with warm filesystem caches, not cold HDD, "
            "network or resolver end-to-end timings. File writes, metadata opening, source sampling "
            "and verification are excluded. Sparse copies contain only sampled chunks.",
            "A point time-series request still decompresses one spatial chunk per date. Codec "
            "selection does not remove this read amplification or establish a long-history SLA.",
            "Source opened read-only; root and sampled band metadata hashes were unchanged. No "
            "concurrent source writer or full source-content checksum audit is covered by this test.",
        ],
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    with report.with_suffix(".md").open("x", encoding="utf-8") as stream:
        stream.write(_markdown(result))
    print(json.dumps({"report": str(report), "totals": result["totals"]}, indent=2), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path, help="NEW directory; never reused or removed")
    parser.add_argument("--report", required=True, type=Path, help="NEW .json path; also writes sibling .md")
    parser.add_argument("--scenes", type=int, default=4, help="Evenly spaced scenes; default 4")
    parser.add_argument("--repeats", type=int, default=5, help="Warm read timing repetitions; default 5")
    args = parser.parse_args()
    benchmark(args.source, args.scratch, args.report, scenes=args.scenes, repeats=args.repeats)


if __name__ == "__main__":
    main()
