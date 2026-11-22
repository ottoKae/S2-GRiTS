param(
    [string]$Version = "",
    [string]$LocalPath = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
if (-not $LocalPath -and -not $Version) {
    throw "Choose -LocalPath for a checkout, or -Version for an explicit Git tag/commit."
}
if ($LocalPath) {
    $resolved = (Resolve-Path -LiteralPath $LocalPath).Path
    & $Python -m pip install -e $resolved --no-build-isolation
} else {
    $requirement = "grits-resolver @ git+https://github.com/ottoKae/GRiTS-Resolver.git@$Version"
    & $Python -m pip install --upgrade $requirement --no-build-isolation
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not $LocalPath -and $Version -match '^v?\d+\.\d+\.\d+$') {
    & $Python "$PSScriptRoot\verify_grits_resolver.py" --expected-version $Version.TrimStart("v")
} else {
    & $Python "$PSScriptRoot\verify_grits_resolver.py"
}
exit $LASTEXITCODE
