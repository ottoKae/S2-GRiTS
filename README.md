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

SCL 是每景配套的像元质量分类，不是反射率或人工林标签。下载由 `assets.bands` 中的 `scl` 控制，与自动去云是两件事。新库可使用 [十光谱＋SCL 配置](config/tessera10_scl_native.example.yaml)同库保存；已有光谱库的历史补齐使用下面的独立质量库入口。

## SCL 独立配套质量层

**原光谱库保持不变，SCL 单独保存为质量 Zarr 的 `20m/scl(time,y,x)`**，保留原生 20 m、`uint8` 和类别 0—11。它相当于每景的一张质量图，不增加 Tessera 的光谱通道数。

1. 用原入选场景的冻结清单准备 [专用配置](config/quality_companion.example.yaml)，填写来源摘要、光谱根、独立质量根及专用缓存。模板的路径和摘要占位值必须替换，不能直接生产；环境须符合 [版本锁](config/quality_environment.lock.json)。
2. 使用新增的 **`process_pipeline`＋`companions.scl.enabled`** 选择是否补齐；也可直接调用 `backfill_quality`。不能给旧光谱配置加上 `scl` 后普通 resume。父任务及专用质量配置的相对路径按各自 **YAML 所在目录**解析。

可选开关配置（完整模板见 [pipeline_scl.example.yaml](config/pipeline_scl.example.yaml)）：

```yaml
workflow: pipeline
spectral:
  root: D:/data/S2-native
  job_config: null  # 复用已有光谱库
companions:
  scl:
    enabled: true  # false：跳过配套生产，不访问或删除质量库
    job_config: ./scl_companion.yaml  # 原冻结质量任务配置
```

```powershell
# 在仓库根运行；先按模板准备本地父任务与质量任务配置
s2grits process_pipeline --config config/local/pipeline_scl.yaml --dry-run
# 以下会实际补齐；同一绑定下重跑跳过已接受景，每次最多新增 64 景
s2grits process_pipeline --config config/local/pipeline_scl.yaml --max-new-scenes 64
```

开关只控制独立质量任务，同库 SCL 仍由 `assets.bands` 控制。新建光谱库须先在开关关闭时运行 `spectral.job_config`，完成并冻结选景后再设为 `null`、开启SCL；当前不自动生成冻结清单。批次输出 `partial` 表示仍有未补齐场景，不能当成全量完成。

3. 每景依次经过源 COG 校验、写 Zarr、重新读取落盘 Zarr 与源 TIFF 的全像元比较、接受后释放本任务 TIFF。最终保留质量 Zarr、Catalog、`quality_links.parquet` 与 `companion.json`；仍存在 TIFF→Zarr 转换，但不长期保留第二套任务 TIFF。
4. 任务暂停并发布稳定版本后，由 [GRiTS-Resolver](https://github.com/ottoKae/GRiTS-Resolver) 的 `s2_quality_root` 按 **collection＋tile＋item_id** 配对读取；10 m 光谱、20 m 光谱、20 m SCL 分别解析时间索引。`quality_valid` 表示质量覆盖有效，**不等于晴空**；缺质量不能当晴空。原生质量筛选应在光谱汇总前完成，月度合成由下游负责。

截至 2026-10-14 核对，Ecuador 案例已完成 **2,430 景／39 瓦片**，稳定发布 `generation=42, complete=true`；这是冻结清单补齐完成，不表示所有月份都有晴空观测。新 pipeline 另完成一景真实 SCL 下载：30,140,100 个像元全量比对零差异，重复执行未重下，任务 TIFF 已释放；另有 15 景生产瓦片的独立重压缩副本通过验证，生产原件未变。随后在全新环境安装公开代码，S2 测试 151 项通过，配套 Resolver 7 项通过、3 项历史数据测试明确跳过；默认 HTTPS 下载器在不注入本地适配器时也通过测试景下载与哈希验证。详细操作、读取示例、恢复规则与本地验收依据见 [SCL 使用说明](docs/SCL_USAGE_ZH.md)。

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

通用场景工作流不生成 `download_manifest`、`download_events`、`run_status`、`run_summary` 或独立审计 JSON。完整过程和错误写入正式日志；`catalog.parquet` 只记录已经成功提交、可供下游读取的产品。缓存负责下载复用，Zarr 内的 `s2grits_resume_state.json` 负责事务提交和场景级断点续传。独立质量补齐入口另存绑定、逐景验收收据及最终发布标识，以保证跨库关联和缓存安全释放。

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

**独立 SCL 质量库另外要求精确环境锁**，普通光谱环境安装不能替代它。使用 Python **3.12.13** 创建环境后，在仓库根执行：

```powershell
python -m pip install -r config/requirements-quality.txt -e ".[dev]"
```

该文件与 `config/quality_environment.lock.json` 对齐；不要修改锁文件绕过旧库的绑定检查。需要配对读取或运行完整集成测试时，另安装下方指定的兼容 Resolver。`backfill_quality --dry-run` 会核对实际版本、清单和数据身份；它不测试实时 HTTPS。

关键配置示例：

```yaml
output:
  base_dir: "D:/data/S2-native"
  logs_dir: "../logs"
  cache_dir: "../_cache"
  existing_store: resume
  zarr_compression:
    cname: zstd
    clevel: 5
    shuffle: shuffle  # byte shuffle
  formats:
    zarr: true
    cog: false
    preview: true
    raw_assets: false
```

相对的 `logs_dir/cache_dir` 以 `output.base_dir` 为基准解析，因此 `../logs` 和 `../_cache` 表示数据集旁的共享目录，不依赖代码仓库或当前工作目录。缓存也可以显式指向独立高速盘。日志文件名包含数据集、工作流、正式启动 UTC 时间与 PID；缓存以全局 STAC Item ID 和资产键组织。若省略配置，默认值就是上述两个相对路径。

新建数据变量默认采用 **Zarr v3 + Blosc/Zstd level 5 + byte shuffle**，保持原生 DN、dtype、nodata 和 `(1,512,512)` 分块（小图幅按边界缩小）。压缩参数省略时仍采用上述默认值。旧数组续写沿用原 codec，并在与配置不同时记录警告；不能通过修改旧数组的 `zarr.json` 转换数据。

压缩配置、旧库副本转换和 resolver 用法见 [无损压缩使用说明](docs/LOSSLESS_COMPRESSION_ZH.md)。压缩倍数取决于内容；相对未压缩数组的倍数不等于相对现有 Zstd 库的额外收益。

```powershell
s2grits doctor --config config/ecuador_17mpu_native_test.yaml --network
s2grits process_scenes --config config/ecuador_17mpu_native_test.yaml
```

仓库中的配置使用通用相对路径。个人数据位置和运行配置放在忽略的 `config/local/` 下，并通过 `--config config/local/<配置名>.yaml` 显式选择。PowerShell 启动脚本按自身位置定位仓库，默认使用当前环境的 `python`，也可用 `-PythonExe` 指定解释器。

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

可复用测试源码保留在 `tests/test_*.py`，纳入版本管理；`tests/data/`、`tests/reports/` 和依赖历史诊断脚本的 `tests/local/` 忽略。安装兼容的 GRiTS-Resolver 后运行：

```powershell
python -B tests/run.py
```

入口为每次运行创建独立的临时目录，禁用 pytest 缓存和 Python 字节码，报告保存到 `tests/reports/<run-id>/`。`docs/plans/`、`docs/reports/` 和 `tooling/diagnostics/` 中的计划、测试与诊断报告也仅保留本地；公共使用说明放在 `docs/`。

仓库目录职责：`src/` 放包代码，`config/` 放可移植模板，`config/local/` 放工作站配置，`docs/` 放公共说明，`tools/` 放可复用命令工具。历史冻结配置 `config/ecuador40_scl_companion.example.yaml` 为保证生产断点绑定原位保留并单独忽略；不复制或改写其内容。忽略文件不等于删除文件，真实验收记录仍留在本地归档目录。

共同的只读多传感器解析逻辑位于独立 `GRiTS-Resolver` 仓库；S2-GRiTS 继续只负责事实数据生产和安全提交。

并发写入使用共享 `logs/locks/` 中的跨进程锁：缓存按 `STAC item/asset` 加锁，Zarr 按瓦片加锁，tile/global catalog 分别加锁。catalog 通过临时文件加原子替换提交。锁文件是可复用的协调点，不是运行状态或下载清单。

安装本次 SCL 更新验证过的共同 Resolver（固定源码提交，旧 `v0.1.0` 不包含独立 SCL 接口）：

```powershell
python -m pip install "grits-resolver @ git+https://github.com/ottoKae/GRiTS-Resolver.git@1506519ab1364280c8e74298692f4961d6dd473e"
# 本地联调：
.\tools\install_grits_resolver.ps1 -LocalPath ..\GRiTS-Resolver
```
