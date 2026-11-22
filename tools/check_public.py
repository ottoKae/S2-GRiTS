"""Check publishable checkout files without printing potentially sensitive values."""
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "workstation-path": re.compile(r"(?i)(?:[A-Z]:[/\\](?:Users|Project|QGIS)[/\\]|/home[/][^/\s]+/)"),
    "url-credentials": re.compile(r"https?://[^\s/:]+:[^\s/@]+@"),
    "signed-url": re.compile(r"(?i)[?&](?:X-Amz-Signature|X-Goog-Signature|sig)=[a-z0-9%+/]{24,}"),
}
PRIVATE_PARTS = {".ssh", ".aws", ".azure", ".venv", "__pycache__"}
PRIVATE_PREFIXES = (
    "config/local/", "docs/reports/", "docs/plans/", "docs/evidence/",
    "tests/local/", "tests/reports/", "tests/data/", "tooling/diagnostics/",
    "outputs/", "data/", ".test-tmp/",
)


def inspect_text(name: str, text: str, public_files: set[str]) -> list[str]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append(f"{name}:{number}: {category}")
    if name.endswith(".md"):
        # shortcut: inline Markdown links only; add reference-link parsing if docs adopt it.
        for link in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", text):
            target = urlsplit(link)
            if target.scheme or not target.path:
                continue
            relative = posixpath.normpath(str(Path(name).parent.as_posix()) + "/" + unquote(target.path))
            if relative not in public_files:
                findings.append(f"{name}: link target absent from public files: {relative}")
    return findings


def check(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, check=True, stdout=subprocess.PIPE,
    )
    public_files = {name for name in result.stdout.decode("utf-8").split("\0")
                    if name and (root / name).is_file()}
    findings = []
    for name in sorted(public_files):
        path = Path(name)
        if (name.startswith(PRIVATE_PREFIXES) or PRIVATE_PARTS.intersection(path.parts)
                or path.name.startswith(".env")
                or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".log"}):
            findings.append(f"{name}: private/generated file in publication set")
        try:
            body = (root / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(inspect_text(name, body, public_files))
    return findings


if __name__ == "__main__":
    issues = check(Path(__file__).resolve().parents[1])
    print("\n".join(issues) if issues else "Public-file checks passed (working tree; not Git history).")
    sys.exit(bool(issues))
