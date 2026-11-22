"""Exercise the release check on real Git file selection, including ignored tracked files."""
import importlib.util
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_public", Path(__file__).parents[1] / "tools/check_public.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_checks_public_tree_and_tracked_ignored_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("config/local/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("[Guide](guide.md)\nD:/data/S2\n", encoding="utf-8")
    (tmp_path / "guide.md").write_text("Public guide", encoding="utf-8")
    private = tmp_path / "config/local/job.yaml"
    private.parent.mkdir(parents=True)
    private.write_text("root: " + "D:/" + "Users/example/data", encoding="utf-8")
    assert checker.check(tmp_path) == []
    subprocess.run(["git", "add", "-f", "config/local/job.yaml"], cwd=tmp_path, check=True)
    issues = checker.check(tmp_path)
    assert any("workstation-path" in issue for issue in issues)
    assert any("private/generated" in issue for issue in issues)
    assert all("Users/example" not in issue for issue in issues)
    (tmp_path / "README.md").write_text("[Report](config/local/unpublished.md)", encoding="utf-8")
    assert any("link target absent" in issue for issue in checker.check(tmp_path))


def test_secret_values_are_not_echoed():
    secret = "ghp_" + "a" * 36
    issues = checker.inspect_text("example.txt", secret, {"example.txt"})
    assert issues == ["example.txt:1: github-token"]
    assert secret not in "\n".join(issues)
