param(
    [string]$Config = (Join-Path $PSScriptRoot "..\config\jiangsu_s2_l2a_native_20250101_20260730.yaml"),
    [switch]$DoctorOnly,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = (Get-Command -Name $PythonExe -CommandType Application -ErrorAction Stop).Source
$CondaEnv = Split-Path -Parent $PythonExe
if ((Split-Path -Leaf $CondaEnv) -eq "Scripts") {
    $CondaEnv = Split-Path -Parent $CondaEnv
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python executable was not found: $PythonExe"
}
if (-not (Test-Path -LiteralPath $Config)) {
    throw "Config file was not found: $Config"
}

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONPATH = "$ProjectRoot\src"
$env:PATH = "$CondaEnv\Library\bin;$CondaEnv\Scripts;$CondaEnv;$env:PATH"
$env:PROJ_LIB = "$CondaEnv\Lib\site-packages\rasterio\proj_data"
$env:PROJ_DATA = "$CondaEnv\Lib\site-packages\rasterio\proj_data"
$env:GDAL_DATA = "$CondaEnv\Library\share\gdal"
$env:GDAL_HTTP_MULTIRANGE = "YES"
$env:CPL_VSIL_CURL_ALLOWED_EXTENSIONS = ".tif,.TIF,.json,.xml,.jpg,.jpeg"
$env:S2GRITS_DOWNLOADER = "requests"
$env:S2GRITS_ARIA2_CONNECTIONS = "8"
$env:S2GRITS_ARIA2_SPLIT = "8"
$env:S2GRITS_ARIA2_MIN_SPLIT_SIZE = "8M"
$env:S2GRITS_ARIA2_STALL_SECONDS = "45"

Set-Location -LiteralPath $ProjectRoot

& $PythonExe -m s2grits doctor --config $Config --network
if ($LASTEXITCODE -ne 0) {
    throw "S2-GRiTS doctor failed with exit code $LASTEXITCODE"
}

if (-not $DoctorOnly) {
    & $PythonExe -m s2grits process_scenes --config $Config
    if ($LASTEXITCODE -ne 0) {
        throw "S2-GRiTS workflow failed with exit code $LASTEXITCODE"
    }
}
