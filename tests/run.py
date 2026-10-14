"""Run local tests with isolated scratch data and ignored, durable reports."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    short_id = uuid4().hex[:8]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + short_id
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
    command = [sys.executable, "-B", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
               "--basetemp", str(scratch / "pytest"), "--junitxml", str(reports / "pytest.xml")]
    result = subprocess.run(command, cwd=repo, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    (reports / "pytest.txt").write_text(result.stdout, encoding="utf-8")
    print(result.stdout, end="")
    print(f"Reports: {reports}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
