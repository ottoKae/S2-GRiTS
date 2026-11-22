"""Immutable GRiTS v1 metadata snapshots; the caller holds its writer root lock.

This module never opens imagery or writes Catalogs/quality bindings. Source
validation and pending-write protection belong to the calling producer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import uuid4

from .provenance_contract import (
    SCHEMA_VERSION,
    TABLE_SCHEMAS,
    canonical_json,
    data_revision,
    read_tables,
    validate_records,
    write_tables,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _io_path(path) -> Path:
    path = Path(path)
    if os.name != "nt":
        return path.absolute()
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    return Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\")
                else "\\\\?\\" + absolute)


def safe_relative(root, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or relative == "."
            or any(c in relative for c in '\\:?*<>|"')
            or any(ord(c) < 32 or ord(c) == 127 for c in relative)):
        raise ValueError("invalid relative provenance path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
        raise ValueError("provenance path escapes root")
    root = _io_path(root)
    target = root.joinpath(*pure.parts)
    current = target
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise ValueError("provenance links are not permitted")
        current = current.parent
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("provenance path escapes root")
    return target


def load_json(path):
    return json.loads(_io_path(path).read_text(encoding="utf-8"))


def atomic_json(path, value) -> None:
    path = _io_path(path)
    data = canonical_json(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_pointer(pointer):
    if (not isinstance(pointer, dict) or pointer.get("schema_version") != SCHEMA_VERSION
            or not re.fullmatch(r"meta-[0-9a-f]{64}", str(pointer.get("metadata_release_id", "")))
            or not re.fullmatch(r"data-[0-9a-f]{64}", str(pointer.get("data_revision", "")))
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(pointer.get(key, "")))
                   for key in ("dataset_sha256", "manifest_sha256"))):
        raise ValueError("invalid provenance current pointer")
    return pointer


def read_current(root) -> dict | None:
    path = safe_relative(root, "provenance/current.json")
    return _validate_pointer(load_json(path)) if path.exists() else None


def _walk_error(error):
    raise error


def _manifest(directory) -> dict:
    directory = _io_path(directory)
    result = {}
    for folder, dirs, files in os.walk(directory, onerror=_walk_error):
        for name in dirs + files:
            path = Path(folder) / name
            relative = path.relative_to(directory).as_posix()
            safe_relative(directory, relative)
            if name in files and relative != "FILES.sha256":
                result[relative] = _digest(path.read_bytes())
    return dict(sorted(result.items()))


def _read_release(directory, pointer) -> dict:
    directory = _io_path(directory)
    _validate_pointer(pointer)
    manifest_path = safe_relative(directory, "FILES.sha256")
    if _digest(manifest_path.read_bytes()) != pointer["manifest_sha256"]:
        raise ValueError("provenance manifest hash mismatch")
    listed = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or name in listed:
            raise ValueError("invalid provenance manifest record")
        safe_relative(directory, name)
        listed[name] = digest
    if listed != _manifest(directory):
        raise ValueError("provenance release contents differ from manifest")
    dataset = load_json(directory / "dataset.json")
    if _digest((directory / "dataset.json").read_bytes()) != pointer["dataset_sha256"]:
        raise ValueError("provenance dataset hash mismatch")
    if (any(dataset.get(key) != pointer[key] for key in
            ("schema_version", "metadata_release_id", "data_revision"))
            or dataset.get("delivery_kind") != "metadata_only"):
        raise ValueError("provenance pointer and dataset disagree")
    outputs, sources, links = read_tables(directory)
    for row in outputs + sources + links:
        if row.get("evidence_ref") and row["evidence_ref"] not in listed:
            raise ValueError("missing provenance evidence reference")
    witnesses = load_json(directory / "evidence/data_witnesses.json")
    if data_revision(outputs, witnesses) != dataset["data_revision"]:
        raise ValueError("provenance data revision disagrees with output mapping")
    if dataset.get("counts") != {"outputs": len(outputs), "sources": len(sources),
                                 "source_links": len(links)}:
        raise ValueError("provenance table counts disagree")
    evidence = {name[len("evidence/"):]: load_json(directory / name)
                for name in listed if name.startswith("evidence/")
                and name != "evidence/data_witnesses.json"}
    return {"outputs": outputs, "sources": sources, "source_links": links,
            "variables": load_json(directory / "variables.json"),
            "limitations": load_json(directory / "limitations.json"),
            "witnesses": witnesses, "evidence": evidence,
            "dataset": dataset, "pointer": pointer}


def read_snapshot(root) -> dict:
    pointer = read_current(root)
    if pointer is None:
        return {"outputs": [], "sources": [], "source_links": [], "variables": [], "limitations": [],
                "witnesses": {}, "evidence": {}, "dataset": None, "pointer": None}
    return _read_release(safe_relative(root, "provenance/releases/" + pointer["metadata_release_id"]), pointer)


def publish_snapshot(root, *, dataset_id: str, inventory: dict,
                     execution_kind="production", extra_dataset=None,
                     expected_release_id=None) -> dict:
    """Validate and publish under the caller's lock; no imagery mutation.

    None expects an unpublished root. An identical current payload is an
    idempotent retry; all other updates require its exact previous release ID.
    """
    if not isinstance(dataset_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", dataset_id):
        raise ValueError("dataset_id must be a safe stable public identifier")
    if execution_kind not in {"production", "repair", "metadata_recheck", "historical_import"}:
        raise ValueError("unsupported provenance execution_kind")
    rows = [inventory.get(name, []) for name in TABLE_SCHEMAS]
    validate_records(*rows)
    if not rows[0]:
        raise ValueError("provenance snapshot has no outputs")
    normalized = {name: sorted([{key: row.get(key) for key in schema.names} for row in records], key=canonical_json)
                  for (name, schema), records in zip(TABLE_SCHEMAS.items(), rows)}
    normalized.update({key: inventory.get(key, default) for key, default in
                       (("variables", []), ("limitations", []), ("witnesses", {}), ("evidence", {}))})
    for name in normalized["evidence"]:
        safe_relative(root, "evidence/" + name)
        if name == "data_witnesses.json" or not name.endswith(".json"):
            raise ValueError("evidence must have a non-reserved relative JSON name")
    revision = data_revision(normalized["outputs"], normalized["witnesses"])
    snapshot = read_snapshot(root)
    if snapshot["dataset"] and snapshot["dataset"]["dataset_id"] != dataset_id:
        raise ValueError("dataset_id differs from the published snapshot")
    extra_dataset = extra_dataset or {}
    release_id = "meta-" + _digest(canonical_json({"schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id, "inventory": normalized,
        "execution_kind": execution_kind, "extra_dataset": extra_dataset}))
    current = snapshot["pointer"]
    actual_id = current["metadata_release_id"] if current else None
    if actual_id == release_id:
        return {**current, "unchanged": True}
    if actual_id != expected_release_id:
        raise ValueError("stale expected metadata release")
    dataset = {"schema_version": SCHEMA_VERSION, "dataset_id": dataset_id,
        "data_revision": revision, "metadata_release_id": release_id,
        "delivery_kind": "metadata_only", "execution_kind": execution_kind,
        "evidence_status": "partial_evidence", "observed_at": datetime.now(UTC).isoformat(),
        "counts": {name: len(normalized[name]) for name in TABLE_SCHEMAS},
        "selection": {"store_paths": sorted({row["store_path"] for row in rows[0]}), "start": None, "end": None},
        "revision_scope": "Output generations and supplied original-data witnesses; not a full pixel checksum."}
    if set(extra_dataset) & set(dataset):
        raise ValueError("extra_dataset cannot replace publisher fields")
    dataset.update(extra_dataset)
    root = _io_path(root)
    staging_parent = safe_relative(root, "provenance/staging")
    staging_parent.mkdir(parents=True, exist_ok=True)
    release = safe_relative(root, "provenance/releases/" + release_id)
    with TemporaryDirectory(prefix="pub-", dir=staging_parent) as temporary:
        staging = _io_path(temporary)
        write_tables(staging, *(normalized[name] for name in TABLE_SCHEMAS))
        for name, value in normalized["evidence"].items():
            atomic_json(safe_relative(staging, "evidence/" + name), value)
        atomic_json(staging / "evidence/data_witnesses.json", normalized["witnesses"])
        atomic_json(staging / "variables.json", normalized["variables"])
        atomic_json(staging / "limitations.json", normalized["limitations"])
        atomic_json(staging / "dataset.json", dataset)
        files = _manifest(staging)
        (staging / "FILES.sha256").write_text(
            "".join(digest + "  " + name + "\n" for name, digest in files.items()), encoding="utf-8")
        pointer = {"schema_version": SCHEMA_VERSION, "metadata_release_id": release_id,
                   "data_revision": revision, "dataset_sha256": _digest((staging / "dataset.json").read_bytes()),
                   "manifest_sha256": _digest((staging / "FILES.sha256").read_bytes())}
        _read_release(staging, pointer)
        if read_current(root) != current:
            raise ValueError("metadata current changed before publication")
        release.parent.mkdir(parents=True, exist_ok=True)
        if release.exists():
            old_pointer = {**pointer,
                "dataset_sha256": _digest((release / "dataset.json").read_bytes()),
                "manifest_sha256": _digest((release / "FILES.sha256").read_bytes())}
            old = _read_release(release, old_pointer)
            if ({k: v for k, v in old["dataset"].items() if k != "observed_at"}
                    != {k: v for k, v in dataset.items() if k != "observed_at"}
                    or {k: v for k, v in _manifest(release).items() if k != "dataset.json"}
                    != {k: v for k, v in files.items() if k != "dataset.json"}):
                raise ValueError("orphan provenance release disagrees with requested snapshot")
            pointer = old_pointer
        else:
            os.replace(staging, release)
        atomic_json(safe_relative(root, "provenance/current.json"), pointer)
    return {**pointer, "unchanged": False}
