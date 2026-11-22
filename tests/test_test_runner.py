"""The explicit integration entry must fail, not skip, when dependencies are absent."""
import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest


def test_integration_requires_resolver(tmp_path):
    environment = tmp_path / "empty-env"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [str(python), "-B", str(Path(__file__).with_name("run.py")), "--integration"],
        check=False, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "Resolver integration dependencies unavailable" in result.stderr
    assert "pip install -e ../GRiTS-Resolver" in result.stderr


def test_resolver_installer_requires_explicit_source():
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell launcher is Windows-specific")
    script = Path(__file__).parents[1] / "tools/install_grits_resolver.ps1"
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        check=False, capture_output=True,
    )
    assert result.returncode != 0
    assert b"Choose -LocalPath" in result.stderr
