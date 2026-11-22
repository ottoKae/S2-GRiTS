"""Run tests with isolated scratch data and an explicit Resolver integration mode."""
import argparse
import importlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", action="store_true",
                        help="Require Resolver and run the S2/SCL reader integration tests")
    args = parser.parse_args()
    targets = ["tests"]
    if args.integration:
        try:
            resolver = importlib.import_module("grits_resolver")
            importlib.import_module("xarray")
        except ImportError as error:
            print(f"Resolver integration dependencies unavailable: {error}", file=sys.stderr)
            print('Install into this Python: python -m pip install -e ../GRiTS-Resolver',
                  file=sys.stderr)
            return 1
        print(f"Python: {sys.executable}")
        print(f"grits-resolver={version('grits-resolver')} from {resolver.__file__}")
        targets = [
            "tests/test_compression_resolver.py",
            "tests/test_quality_backfill.py::test_resolver_end_to_end_same_scene_three_indices",
            "tests/test_quality_backfill.py::test_reader_rejects_changed_publication_artifact",
        ]
    repo = Path(__file__).resolve().parents[1]
    short_id = uuid4().hex[:8]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + short_id
    # Native Zarr paths are deep; keep Windows scratch paths short.
    scratch = repo / ".test-tmp" / short_id
    reports = repo / "tests" / "reports" / run_id
    (scratch / "temp").mkdir(parents=True, exist_ok=False)
    reports.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               TEMP=str(scratch / "temp"), TMP=str(scratch / "temp"))
    for name, folder in (("GDAL_DATA", "gdal"), ("PROJ_DATA", "proj")):
        data = Path(sys.prefix) / "Library" / "share" / folder
        if name not in env and data.is_dir():
            env[name] = str(data)
    command = [sys.executable, "-B", "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider",
               "--basetemp", str(scratch / "pytest"), "--junitxml", str(reports / "pytest.xml")]
    result = subprocess.run(command, check=False, cwd=repo, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    (reports / "pytest.txt").write_text(result.stdout, encoding="utf-8")
    print(result.stdout, end="")
    print(f"Reports: {reports}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
