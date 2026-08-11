param(
    [switch]$DoctorOnly
)

$Runner = "F:\AgrD_System\S2-GRiTS-native\tools\run_s2grits_jiangsu_native.ps1"
$PilotConfig = "F:\AgrD_System\S2-GRiTS-native\config\pilot_s2_l2a_native.yaml"

& $Runner -Config $PilotConfig -DoctorOnly:$DoctorOnly
exit $LASTEXITCODE
