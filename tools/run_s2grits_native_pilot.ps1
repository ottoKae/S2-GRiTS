param(
    [switch]$DoctorOnly,
    [string]$PythonExe = "python"
)

$Runner = Join-Path $PSScriptRoot "run_s2grits_jiangsu_native.ps1"
$PilotConfig = Join-Path $PSScriptRoot "..\config\pilot_s2_l2a_native.yaml"

& $Runner -Config $PilotConfig -DoctorOnly:$DoctorOnly -PythonExe $PythonExe
exit $LASTEXITCODE
