"""Launch real PowerShell wrappers with a local recorder, never the workflow."""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest


POWERSHELL = shutil.which("powershell")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell required")
LAUNCHERS = (
    "run_s2grits_jiangsu.ps1", "run_s2grits_jiangsu_native.ps1",
    "run_s2grits_native_pilot.ps1",
)


@pytest.fixture
def portable_repo(tmp_path):
    repo = tmp_path / "portable repository"
    (repo / "tools").mkdir(parents=True)
    (repo / "config").mkdir()
    source = Path(__file__).resolve().parents[1] / "tools"
    for name in LAUNCHERS:
        text = (source / name).read_text(encoding="utf-8-sig")
        assert not re.search(r"[A-Za-z]:[\\/]", text), "launcher contains a fixed machine path"
        shutil.copyfile(source / name, repo / "tools" / name)
    for name in ("jiangsu_s2_l2a_native_20250101_20260730.yaml", "pilot_s2_l2a_native.yaml"):
        (repo / "config" / name).write_text("workflow: scenes\n", encoding="utf-8")
    env_root = tmp_path / "mock environment"
    python = env_root / "Scripts" / "python.cmd"
    python.parent.mkdir(parents=True)
    python.write_text(
        '@echo off\n'
        '>>"%S2GRITS_TEST_TRACE%" echo %*\n'
        '>>"%S2GRITS_TEST_ENV%" echo CWD=%CD%\n'
        '>>"%S2GRITS_TEST_ENV%" echo PYTHONPATH=%PYTHONPATH%\n'
        '>>"%S2GRITS_TEST_ENV%" echo PROJ_DATA=%PROJ_DATA%\n'
        '>>"%S2GRITS_TEST_ENV%" echo GDAL_DATA=%GDAL_DATA%\n'
        '>>"%S2GRITS_TEST_ENV%" echo DOWNLOADER=%S2GRITS_DOWNLOADER%\n'
        '>>"%S2GRITS_TEST_ENV%" echo HTTP_MULTIRANGE=%GDAL_HTTP_MULTIRANGE%\n'
        'exit /b %S2GRITS_TEST_EXIT%\n', encoding="ascii",
    )
    env = {
        **os.environ, "PATH": str(python.parent) + os.pathsep + os.environ["PATH"],
        "S2GRITS_TEST_TRACE": str(tmp_path / "calls.txt"),
        "S2GRITS_TEST_ENV": str(tmp_path / "environment.txt"), "S2GRITS_TEST_EXIT": "0",
    }
    return repo, python, env_root, env


def run_launcher(fixture, name, *arguments):
    repo, _, _, env = fixture
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(repo / "tools" / name), *map(str, arguments)],
        cwd=repo.parent, env=env, capture_output=True, text=True, timeout=30,
    )
    trace = Path(env["S2GRITS_TEST_TRACE"])
    calls = [
        [arg.strip('"') for arg in shlex.split(line, posix=False)]
        for line in trace.read_text().splitlines()
    ] if trace.exists() else []
    return result, calls


@pytest.mark.parametrize("name", LAUNCHERS)
@pytest.mark.parametrize("doctor_only", [False, True])
def test_portable_launchers_forward_python_config_and_doctor_switch(portable_repo, name, doctor_only):
    repo, python, env_root, env = portable_repo
    arguments = ["-PythonExe", str(python)] + (["-DoctorOnly"] if doctor_only else [])
    result, calls = run_launcher(portable_repo, name, *arguments)
    assert result.returncode == 0, result.stderr
    assert [call[2] for call in calls] == (["doctor"] if doctor_only else ["doctor", "process_scenes"])
    config = "pilot_s2_l2a_native.yaml" if "pilot" in name else "jiangsu_s2_l2a_native_20250101_20260730.yaml"
    for call in calls:
        assert call[:2] == ["-m", "s2grits"] and call[3] == "--config"
        assert Path(call[4]).resolve() == (repo / "config" / config).resolve()
    assert calls[0][5:] == ["--network"]
    settings = dict(line.split("=", 1) for line in Path(env["S2GRITS_TEST_ENV"]).read_text().splitlines())
    assert Path(settings["CWD"]) == repo
    assert Path(settings["PYTHONPATH"]) == repo / "src"
    assert Path(settings["GDAL_DATA"]) == env_root / "Library" / "share" / "gdal"
    assert Path(settings["PROJ_DATA"]) == env_root / "Lib" / "site-packages" / "rasterio" / "proj_data"
    assert settings["DOWNLOADER"] == "requests" and settings["HTTP_MULTIRANGE"] == "YES"


@pytest.mark.parametrize("exit_code", ["0", "7"])
def test_active_python_custom_config_and_failure_propagation(portable_repo, exit_code):
    repo, _, _, env = portable_repo
    config = repo / "custom config.yaml"
    config.write_text("workflow: scenes\n", encoding="utf-8")
    env["S2GRITS_TEST_EXIT"] = exit_code
    result, calls = run_launcher(portable_repo, "run_s2grits_jiangsu_native.ps1", "-Config", config)
    assert (result.returncode == 0) == (exit_code == "0")
    assert len(calls) == (2 if exit_code == "0" else 1)
    assert all(Path(call[4]).resolve() == config.resolve() for call in calls)
