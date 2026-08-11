param(
    [switch]$DoctorOnly
)

$Runner = "F:\AgrD_System\S2-GRiTS-native\tools\run_s2grits_jiangsu_native.ps1"
& $Runner -DoctorOnly:$DoctorOnly
exit $LASTEXITCODE
