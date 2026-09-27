param(
    [switch]$DoctorOnly,
    [string]$PythonExe = "python"
)

$Runner = Join-Path $PSScriptRoot "run_s2grits_jiangsu_native.ps1"
& $Runner -DoctorOnly:$DoctorOnly -PythonExe $PythonExe
exit $LASTEXITCODE
