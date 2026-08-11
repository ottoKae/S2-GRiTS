# S2-GRiTS Native

Sentinel-2 Collection 1 L2A downloader for Jiangsu. This isolated fork keeps
the original 10 m, 20 m, and 60 m grids in one per-tile Zarr store.

## Production configuration

- MGRS tiles: 26 Jiangsu tiles
- Time: 2025-01-01 through 2026-07-30
- Scene cloud cover: less than or equal to 20 percent
- Collection: `sentinel-2-c1-l2a`
- Output: `E:\JiangSu_S2_native`
- Formats: grouped Zarr and JPEG preview; no output COG
- Pixel cloud mask: not generated; source SCL and CLD are archived at 20 m

The source package is loaded directly from this project by the PowerShell
runner, so it does not replace the existing editable `s2grits` installation.

Validate the environment and network without downloading:

```powershell
powershell -ExecutionPolicy Bypass -File F:\AgrD_System\S2-GRiTS-native\tools\run_s2grits_jiangsu_native.ps1 -DoctorOnly
```

Run a one-tile, one-scene pilot:

```powershell
powershell -ExecutionPolicy Bypass -File F:\AgrD_System\S2-GRiTS-native\tools\run_s2grits_native_pilot.ps1
```

Start the complete Jiangsu download:

```powershell
powershell -ExecutionPolicy Bypass -File F:\AgrD_System\S2-GRiTS-native\tools\run_s2grits_jiangsu_native.ps1
```

See `docs/NATIVE_IMPLEMENTATION.md` for the storage layout, progress model,
manifest fields, resume behavior, and audit rules.
