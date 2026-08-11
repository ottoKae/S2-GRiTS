# S2-GRiTS

S2-GRiTS 保留 Sentinel-2 的 10/20/60 m 原生存储作为事实数据层，在 canonical Parquet 中把每个分辨率组声明为独立网格；共同 resolver 在应用阶段惰性投影到 S1-GRiTS 的 30 m 网格，并显式执行时间连接。这样既不破坏 Sentinel-2 原始分辨率，也能让下游快速获得统一的虚拟多传感器数据立方体。

本项目从 Element 84 Earth Search 检索 Sentinel-2 Collection 1 Level-2A 场景，按 MGRS 瓦片建立可增量写入的原生分辨率 Zarr，并生成兼容 S1-GRiTS canonical schema v7 的 Parquet 目录。

## 波段与变量命名

配置文件仍使用 Earth Search 的资产键；Zarr 使用稳定、物理语义明确的变量名。每个变量同时保存 `source_band`（真实 Sentinel-2 波段号）和 `source_asset`（STAC 资产键）。

| 原生组 | Sentinel-2 波段 | Zarr 变量 |
| --- | --- | --- |
| `10m` | B02, B03, B04, B08 | `blue`, `green`, `red`, `nir_broad` |
| `20m` | B05, B06, B07, B8A, B11, B12, SCL, CLD | `red_edge_1`, `red_edge_2`, `red_edge_3`, `nir_narrow`, `swir_1`, `swir_2`, `scl`, `cloud_probability` |
| `60m` | B01, B09 | `coastal`, `water_vapor` |

共同 resolver 的标准输出别名为 `blue`、`green`、`red`、`nir_narrow`、`swir_1`、`swir_2`。S2 的 B8A 对应 `nir_narrow`；B08 明确保留为 `nir_broad`，不会把 B05 红边误当成逻辑 NIR。

## 最简数据结构

数据根目录只保存事实数据和 canonical 目录：

```text
{dataset}/
├── {MGRS_TILE}/
│   ├── catalog.parquet
│   └── scenes_L2A_native/
│       ├── zarr/s2grits_scenes_{MGRS_TILE}_L2A_native.zarr/
│       │   ├── s2grits_resume_state.json
│       │   ├── 10m/{time,x,y,blue,green,red,nir_broad,...}
│       │   ├── 20m/{time,x,y,red_edge_1,...,nir_narrow,swir_1,swir_2,...}
│       │   └── 60m/{time,x,y,coastal,water_vapor,...}
│       └── preview/*.jpg                 # 可选
└── catalog.parquet
```

日志和缓存由配置指定为数据集旁的独立目录：

```text
logs/                                      # 可供多个任务共享
├── S2-GRiTS__dataset-{name}__workflow-scenes-L2A-native__started-{UTC}__pid-{PID}.log
└── S2-GRiTS__dataset-{name}__retry-scenes.parquet # 仅发现缺失时生成
_cache/                                   # 可供多个任务共享
└── {STAC_ITEM_ID}/{asset}.tif
```

正常运行不生成 `download_manifest`、`download_events`、`run_status`、`run_summary` 或独立审计 JSON。完整过程和错误写入正式日志；`catalog.parquet` 只记录已经成功提交、可供下游读取的产品。缓存负责下载复用，Zarr 内的 `s2grits_resume_state.json` 负责事务提交和场景级断点续传。

## Python 3.12 环境

项目严格要求 Python `>=3.12,<3.13`：

```powershell
conda env create -f environment.yml
conda activate py312_s2grits
python -m pip install -e .
```

也可使用 Python 3.12 创建虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

## 配置与运行

关键配置示例：

```yaml
output:
  base_dir: "D:/data/S2-native"
  logs_dir: "../logs"
  cache_dir: "../_cache"
  existing_store: resume
  formats:
    zarr: true
    cog: false
    preview: true
    raw_assets: false
```

相对的 `logs_dir/cache_dir` 以 `output.base_dir` 为基准解析，因此 `../logs` 和 `../_cache` 表示数据集旁的共享目录，不依赖代码仓库或当前工作目录。缓存也可以显式指向独立高速盘。日志文件名包含数据集、工作流、正式启动 UTC 时间与 PID；缓存以全局 STAC Item ID 和资产键组织。若省略配置，默认值就是上述两个相对路径。

```powershell
s2grits doctor --config config/ecuador_17mpu_native_test.yaml --network
s2grits process_scenes --config config/ecuador_17mpu_native_test.yaml
```

## 读取 Zarr

三个原生组空间网格不同，应分别打开：

```python
import xarray as xr

store = "D:/data/S2-native/17MPU/scenes_L2A_native/zarr/s2grits_scenes_17MPU_L2A_native.zarr"
ds10 = xr.open_zarr(store, group="10m", consolidated=False)
ds20 = xr.open_zarr(store, group="20m", consolidated=False)

rgb = ds10[["red", "green", "blue"]]
analysis = ds20[["nir_narrow", "swir_1", "swir_2"]]
```

不要直接 `xr.merge()` 三个组。跨传感器 resolver 将依据 canonical Parquet 的 `grid_id` 判断完全同网格或需要虚拟重投影，再对时间轴执行显式连接。

## 断点续传

- 完整且可读取的缓存资产会直接复用。
- 一场景的所有分辨率组共同提交；任一波段失败会回滚该场景的数组尾部。
- 只有全部波段成功后才更新时间轴和 `s2grits_resume_state.json`。
- 兼容读取旧 store 的 `s2grits_metadata.json`，新写入统一使用新名称。

## 测试

```powershell
python -m pytest -q
```

共同的只读多传感器解析逻辑位于独立 `GRiTS-Resolver` 仓库；S2-GRiTS 继续只负责事实数据生产和安全提交。

并发写入使用共享 `logs/locks/` 中的跨进程锁：缓存按 `STAC item/asset` 加锁，Zarr 按瓦片加锁，tile/global catalog 分别加锁。catalog 通过临时文件加原子替换提交。锁文件是可复用的协调点，不是运行状态或下载清单。

安装固定版本的共同 resolver：

```powershell
.\tools\install_grits_resolver.ps1 -Version v0.1.0
# 本地联调：
.\tools\install_grits_resolver.ps1 -LocalPath ..\GRiTS-Resolver
```
