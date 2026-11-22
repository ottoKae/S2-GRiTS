# SCL 是什么，S2-GRiTS 怎样保存和使用它

更新日期：2026-10-14。独立质量库入口及 Resolver 配对读取均已实现；Ecuador 冻结清单的 **2,430 景／39 瓦片已完成 P3 补齐及 P4 验收**。本日只读核对发布标识为 `published`、`complete=true`、`generation=42`。不是待启动的新下载任务。

历史计划集中在本地 `docs/plans/`，P1—P4与HTTPS恢复报告集中在本地 `docs/reports/`，不随公共仓库分发。本文是公共操作入口；历史报告保留各自阶段状态，不代替当前发布状态。

## 先选正确入口

| 需求 | 使用方式 |
|---|---|
| 新建含光谱及 SCL 的同库归档 | `process_scenes`，在 `assets.bands` 中包含 `scl` |
| 给已有十光谱库补历史 SCL，保留原库 | **`backfill_quality`＋独立质量 Zarr**，按原冻结场景清单补齐 |
| 已有完整配套库，准备模型输入 | 只读加载稳定发布版本，不再下载；使用 Resolver 关联，再由应用执行质量筛选 |

本文重点是第二、三种。S2-GRiTS 生产质量事实层，GRiTS-Resolver 负责只读关联与窗口读取，下游负责清晰候选规则、光谱定标、月度合成及模型输入。

## 用开关选择是否补齐独立 SCL（2026-10-14 已实现）

新入口为 `s2grits process_pipeline --config <父任务.yaml>`，模板见 [pipeline_scl.example.yaml](../config/pipeline_scl.example.yaml)。父任务引用现有场景或质量配置，不修改它们的内容和生产绑定：

```yaml
workflow: pipeline
spectral:
  root: D:/data/S2-native
  job_config: null
companions:
  scl:
    enabled: true
    job_config: ./scl_companion.yaml
```

`true`要求质量任务引用的光谱根与`spectral.root`一致，随后复用原冻结身份、三套组索引、全像元验收及缓存释放逻辑。`false`或省略`companions`时，返回`SKIPPED_DISABLED`，不加载质量配置、不创建质量缓存，也不删除已有质量库。只接受YAML布尔值，不接受带引号的`"false"`或`"是"`。开关只控制独立库生产，不禁用原光谱配置中已经指定的同库`scl`，也不控制Resolver读取。

`spectral.job_config: null`表示复用已有光谱库；设置为场景YAML时，开关必须关闭，运行原`process_scenes`工作流并检查其audit结果。新光谱库完成后，由原选景结果准备冻结质量合同，再清空`spectral.job_config`并启用SCL。当前禁止同一次调用既重新搜索光谱景又启用冻结SCL任务；不会自动更新摘要或生成另一套入选清单。

```powershell
s2grits process_pipeline --config config/local/pipeline_scl.yaml --dry-run
s2grits process_pipeline --config config/local/pipeline_scl.yaml --max-new-scenes 64
```

父配置中的`root`和两个`job_config`按父YAML目录解析；质量子配置按自己的目录解析。场景子配置保留原`output.base_dir`的工作目录相对规则，建议使用绝对路径，且其输出根必须与父配置一致。旧`process_scenes`拒绝含`companions`的配置，避免把新开关默默忽略；显式`backfill_quality`仍可单独使用。

新入口的`--max-new-scenes`、`--max-new-scenes-per-tile`和`--tiles`**仅作用于SCL**，不是光谱下载限制。停在合法部分前缀时返回顶层`status=partial`和`scl.status=PUBLISHED_PARTIAL`；已完成返回`COMPLETE`；只读质量预检为`PREFLIGHT_PASSED`。两个生产阶段都未启用时，普通调用返回`status=skipped`，不表示数据已验收。预检不测试实时HTTPS、不重验全库像元。身份／根／配置错误非零退出，质量阶段错误包含`SCL_FAILED`。光谱audit失败时整体报错，不称成功。

现有Ecuador已完成库只需读取或dry-run，不需要再次生产。本地父配置为 `config/local/pipeline_scl_ecuador_20261014.yaml`（不随公共仓库分发）；实测新入口识别2,430景／39瓦片，未下载，原生产源码绑定仍一致。S1-GRiTS与Resolver源码无需因新增调度开关而改变。

SCL（Scene Classification Layer，场景分类层）是 Sentinel-2 L2A 随每次拍摄提供的像元分类图。它说明一个像元可能是云、云影、植被、非植被、水体等，是输入质量信息，不是人工林真值，也不是云概率图。本仓库使用其 **20 m、uint8、类别 0—11** 的源资产。参见 [Copernicus 产品规范](https://sentinels.copernicus.eu/documents/d/sentinel/s2-pdgs-cs-di-psd-v15-0)和[Sentinel Hub L2A 数据说明](https://docs.sentinel-hub.com/api/latest/data/sentinel-2-l2a/)。

| 类别 | 含义与首轮建议 |
|---|---|
| 0、1 | 无数据、饱和或坏像元；排除 |
| 2、7 | 阴影／暗目标、未分类；单列为不确定，严格质量策略暂不接收 |
| 3 | 云影；排除 |
| 4、5、6 | 植被、非植被、水体；作为首轮较严格的清晰观测候选 |
| 8、9、10 | 中概率云、高概率云、薄卷云；排除 |
| 11 | 雪／冰；在当前人工林任务的清晰观测策略中排除 |

类别 2 的名称随产品处理基线有所变化，应保留源产品版本。上述 4/5/6 规则是本项目首轮质量策略，不是“这些像元一定无云”的真值保证。SCL 可能误分，不能据它计算人工林类别标签。[Copernicus 处理基线说明](https://sentiwiki.copernicus.eu/web/s2-processing)。

## 仓库已有的支持

- 配置资产键为小写 `scl`，定义在 `src/s2grits/bands.py`，所属原生组为 `20m`。
- 通用下载入口 `s2grits process_scenes --config <yaml>` 按 `assets.bands` 下载；原生 writer 会根据源元数据保留 SCL 的 uint8，不将其按反射率归一化。
- 新库的结果是 `20m/scl(time,y,x)`。已有 `config/ecuador_17mpu_native_2024.yaml` 等示例包含 SCL。
- `source.cloud_cover_max` 是场景级元数据筛选，不能替代像元质量分类。
- `processing.cloud_mask.enabled: false` 不禁止下载 SCL；下载由 `assets.bands` 决定。当前原生工作流保存原始值，没有实现以该开关执行自动去云的路径；不要将它改为 true 后就认为像元已掩膜。

十光谱＋SCL 的最小片段：

```yaml
assets:
  resolution_mode: native_groups
  resampling: none
  bands: [red, blue, green, nir, nir08, rededge1, rededge2, rededge3, swir16, swir22, scl]
processing:
  monthly: {enabled: false}
  cloud_mask: {enabled: false}
```

完整的单景新库示例为 [tessera10_scl_native.example.yaml](../config/tessera10_scl_native.example.yaml)。它查询一个瓦片的一个月，最多写一景；不是“按冻结清单补齐旧库”的命令，也不是已运行的下载任务。需要运行时显式选择配置和新的输出根。

## 已有十光谱库怎样补齐

**不能只在旧配置加上 scl，然后对旧库普通 resume。** `append_item_to_native_zarr` 会跳过 sidecar 中已经提交的 item_id，新增资产不会自动回填历史场景。修改资产集合还涉及已有数组长度和处理签名的一致性。

现有 `backfill_quality` 按以下合同建立独立 SCL 配套库：

1. 从原冻结入选清单取得同一 `source collection + MGRS tile + item_id` 的 SCL 资产；保留完整 UTC 时间，不重新按日期选景。
2. 只下载 SCL，校验冻结的大小及 SHA256；源文件缺失或变更时列为异常，不静默换成另一景。
3. 下载的原始 COG 经过校验后，写入独立的 `20m/scl` Zarr 和配套目录。当前 Ecuador 补齐任务已确定以 Zarr 为最终交付，不能只保存 TIFF 就算完成；不重下十个光谱波段，也不直接修改已验收的光谱库。
4. 检查与对应光谱景的时间、投影、尺寸、像元原点及范围；按身份连接两份目录。
5. 先验证一个小范围，再按冻结清单扩大补齐。

普通 CLI 会重新搜索满足日期条件的全部场景；SCL-only YAML 虽能建立新库，却不能单独保证与旧入选清单一一对应。当前冻结任务的 `tools/run_selected_archive.py` 又明确限定十个光谱资产。历史 `archive_selected_scl.py` 完成过 17MPU 的 16 景清单驱动小试点，但保留缓存。P1 新增 `s2grits backfill_quality` 正式入口，按冻结身份补齐独立质量库，不修改历史十光谱 runner 的资产列表。

通用配置见 [quality_companion.example.yaml](../config/quality_companion.example.yaml)。填写真实路径和摘要后，存为本地任务配置再执行只读预检：

```powershell
s2grits backfill_quality --config config/local/scl_companion.yaml --dry-run
```

P2 已选择 4 个瓦片、各写最早 2 景。初次运行可用 `--max-new-scenes 8 --max-new-scenes-per-tile 2`；试点恢复必须按接受收据计算剩余配额，不能重复初次新增限额而扩大试点。运行环境必须符合 [版本锁](../config/quality_environment.lock.json)，不能将专用配置传给普通 `process_scenes`。

新机器使用 Python **3.12.13**，在仓库根执行 `python -m pip install -r config/requirements-quality.txt -e ".[dev]"`，即可安装与锁文件一致的质量层依赖。按 [README](../README.md) 将兼容的 Resolver 安装到同一环境，再执行 `python -B tests/run.py --integration`；不能用旧 `v0.1.0` 替代。2026-10-14 已验证公开文件构建的 wheel、全新环境测试和默认 HTTPS 下载；本地研究脚本不是安装依赖。冻结清单仍需按下表准备，不会由安装步骤自动生成。

### 配置准备、分批运行与恢复

1. **先确认已有库是否完整**。已完成的 Ecuador 库直接进入读取阶段，无需复制配置再重跑。
2. 新任务参考示例，将工作站路径配置保存在 `config/local/`。专用入口的文件路径相对于配置文件目录解析；复制到 `local/` 后，清单和环境锁路径要相应调整，建议明确写绝对路径。新任务确定后固定配置文件字节，已有非空质量库恢复时必须使用原配置、来源清单及绑定代码版本，不另存改写配置来绕过绑定检查。
3. 核对如下输入。当前入口要求 `sentinel-2-c1-l2a`、现有 10 m／20 m 光谱组及特定 Earth Search HTTPS 来源，不是任意供应商通用补齐工具；目前没有新增自动生成冻结清单的公开 CLI。

| 配置 | 必须准备的内容 |
|---|---|
| `quality.spectral_root` | 已验收光谱根，只读 |
| `quality.frozen_features`、`*_sha256` | 原选景完整 STAC JSONL，包含 SCL URL、文件长度、SHA256；不重新搜索选景 |
| `quality.backfill_manifest`、`*_sha256` | 同顺序质量清单：collection、tile_id、item_id、datetime_utc、href、source_file_bytes、source_sha256、shape、crs、transform、spectral_grid_id |
| `quality.spectral_catalog_sha256` | 对应光谱 Catalog 摘要 |
| `quality.environment_lock` | 实际锁定环境，不能为绕过检查直接改锁文件 |
| `output.base_dir`、`output.cache_dir` | 与光谱根及彼此不重叠、无链接混淆的质量根和任务缓存 |
| 资产及容量 | 仅 `[scl]`；原生不重采样；单景缓存≤64 MiB，零预取；默认输出预算100 GiB、磁盘预留20 GiB |

4. 在仓库根、正确环境下运行（下面为命令示例，本次文档更新不执行）：

```powershell
s2grits backfill_quality --config config/local/scl_companion.yaml --dry-run
s2grits backfill_quality --config config/local/scl_companion.yaml --max-new-scenes 64
```

`--dry-run` 不创建输出、不发 HTTP 请求，核对来源与环境等前置条件；**不等于现有质量 Zarr 已通过完整验收**，也不是所有待下载／恢复状态的汇总。`--max-new-scenes` 是本次新增上限，不是最终目标场景数；重复调用会继续下一批。可配合 `--tiles` 与 `--max-new-scenes-per-tile` 做固定范围试点，接受顺序始终为各瓦片冻结清单的时间前缀。专用入口用根旁固定质量锁防止重复 writer。

5. 中断后先查失败状态，保持同一绑定重跑：已接受景跳过；已提交但未接受的异常尾部按恢复逻辑处理，保留当前源文件重验；不得删除 sidecar 或回滚已接受前缀。清理失败会阻止取下一景。不要同时启动第二个 writer，也不要在 writer 运行时加载 Resolver。
6. 读取须见 `companion.json.state=published`；部分稳定前缀可读，但只有 `complete=true` 且全部冻结身份对账通过才算本任务补齐完成。发布绑定全局／瓦片 Catalog、关联表、收据及接受前缀；版本改变后重建 Resolver。

## 最终存储约定与 Ecuador 完成证据

**每景保存一张 SCL 类别图，最终为 `20m/scl(time, y, x)`。** 可以把它理解成每景附带一个质量 band；在本仓库的 Zarr 中，各 band 分别是命名数组，没有要求把它们拼成一个包含 `band` 维度的大数组。

Ecuador 案例采用独立配套 Zarr。光谱库与质量库放在同一父目录下；具体路径由接收者配置，不依赖原生产机器的位置。质量库根另有 `catalog.parquet`、`quality_links.parquet`、`companion.json` 与任务验收记录。每个瓦片的结构沿用原生写入器：

```text
<质量库根目录>/<MGRS瓦片>/scenes_L2A_native/zarr/
  s2grits_scenes_<MGRS瓦片>_L2A_native.zarr/
    time
    20m/
      time
      x
      y
      spatial_ref
      scl    # uint8，维度 (time, y, x)，原生 20 m
```

| 内容 | 本轮约定 |
|---|---|
| 下载与写入资产 | 仅 `scl`；最终必须有 Zarr 数组和 Catalog |
| 场景范围 | 现有冻结入选清单的 2,430 景、39 个瓦片；不重新选景 |
| 身份关联 | `collection + tile_id + item_id`，并核对完整 UTC 时间；不能仅凭日期关联 |
| 时间轴 | 按瓦片从早到晚写入，最终与对应光谱库逐景一致；不把真实拍摄时间改成月初 |
| 空间网格 | 每景与光谱库的原生 20 m 组核对 CRS、尺寸、仿射变换及范围；10 m 光谱另行按使用需要匹配 |
| 数值 | 保留 uint8 类别 0—11，0 为源无数据；云类别也保留，不预先全部改写为 0 |
| 压缩与分块 | Blosc＋Zstd level 5＋byte shuffle，分块 `(1, 512, 512)`，实际落盘后核验 |
| 模型输入 | SCL 用于生成质量掩膜及统计有效观测，Tessera 仍接收十个光谱通道 |

最终验收需要同时确认：冻结清单中的每景均已提交；SCL 时间轴和场景身份与原光谱库一致；源文件大小及 SHA256 正确；Zarr dtype、原生网格、实际压缩配置、Catalog 与 sidecar 一致；源 TIFF 与重新打开的落盘 Zarr 逐景全像元一致。10 m 光谱、20 m 光谱和 SCL 的时间索引分别解析。原始 TIFF 下载完成不等于这项任务完成。

**全量完成时间为 2026-10-10 14:38（北京时间）**，依据本地 `tooling/diagnostics/scl-p3-20261009/` 的 `status.json`及`acceptance.json`：2,430 景、39 瓦片，逐景累计比较 73,240,443,000 个像元，差异 0；复用本地源16景，另有下载来源收据2,414景；任务缓存无 TIFF／part 残留。质量库文件长度合计 3,404,490,437 B（约3.40 GB），不是 NTFS 实际分配占用。本地证据目录不随公开仓库分发；2026-10-14 本次仅重新核对完成回执及发布标识，没有重新下载或重新比较全库像元。

固定 Zarr format 3，`consolidated=False`；环境锁为 Python 3.12.13、zarr 3.1.5、xarray 2025.11.0、numcodecs 0.15.1，其余依赖见锁文件。读取必须匹配配套库的版本检查，不能假设某个旧 Resolver tag 已包含本接口。

## 只读配对及读取示例

使用包含 `s2_quality_root`、`s2_scene_quality` 接口的 GRiTS-Resolver；本地联调可按 README 用 `tools/install_grits_resolver.ps1 -LocalPath ../GRiTS-Resolver` 安装。下面的函数由调用者提供实际根、场景身份和**原生 SCL 20 m 网格内的行列范围**：

```python
import numpy as np
import xarray as xr
from grits_resolver import MultiSensorResolver

# 每个稳定发布版本初始化一次，后续复用，不要逐窗口重新验证全库。
def open_pairing(s1_root, spectral_root, quality_root):
    return MultiSensorResolver(
        s1_root, spectral_root, s2_quality_root=quality_root,
    )

def read_native_scl(resolver, tile_id, item_id, rows, cols):
    pair = resolver.s2_scene_quality(
        tile_id=tile_id, item_id=item_id,
        collection="sentinel-2-c1-l2a", allow_missing=True,
    )
    if not pair.quality_available:
        return {"quality_available": False, "reason": "QUALITY_MISSING"}
    entry = pair.quality_20m
    with xr.open_zarr(
        entry.zarr_path, group=entry.zarr_group, chunks=None,
        consolidated=False, mask_and_scale=False,
        use_zarr_fill_value_as_mask=False,
    ) as ds:
        scl = ds["scl"].isel(
            time=pair.quality_20m_index, y=rows, x=cols,
        ).values.copy()
    assert scl.dtype == np.uint8
    valid = scl != 0
    return {"quality_available": True, "scl": scl,
            "quality_valid": valid,
            "clear_candidate": valid & np.isin(scl, [4, 5, 6])}
```

`rows`、`cols` 例如为调用者事先确定的 `slice(0, 128)`，不能直接代入30 m或10 m网格索引。光谱10 m、光谱20 m分别使用 `pair.spectral_10m_index`、`pair.spectral_20m_index`，不复用 `quality_20m_index`。上例原生 xarray 读取应在 writer 停止且发布版本不变时使用；重启写入后，停止旧读取器并重新初始化。

若已有该接口支持的对应 S1 网格和 `TargetWindow`，可调用 `resolver.read_s2_quality_on_s1(pair, window=target_window)`，返回 `scl`、`quality_valid`、`quality_available` 和原因；它按最近邻读取类别，**不代表整个30 m像元的清晰面积比例**。不得把原生 SCL 掩膜直接广播到不同 shape 的光谱上；正式筛选应先按明确空间支持映射，再进行光谱汇总。

| 返回状态／数值 | 含义 |
|---|---|
| `quality_available=false` | 该光谱场景没有已发布质量配对，质量未知 |
| 已配对但 SCL=0 | 原始无数据，仍为 `uint8(0)`，质量覆盖无效 |
| 云等类别，SCL≠0 | 质量覆盖有效，但未必是清晰候选，不改写原类别 |
| `clear_candidate=false` | 不符合所选清晰策略；布尔0是有效判断值，不是 nodata |

## TIFF 与 Zarr 是否会重复占用空间

当前冻结源提供的是 SCL 的 COG（可分块读取的 GeoTIFF）地址，没有可直接下载的对应 Zarr 资产。因此，流程是从源 TIFF 读取像元，在本地无损写成 Zarr；不是从服务器分别下载 TIFF 和 Zarr 两套产品。这个格式转换仍然存在，但不改变像元类别值。

| 代码路径 | 当前实际行为 |
|---|---|
| 通用 `process_scenes` CLI | 先执行 `iter_cached_scenes` 下载 TIFF 缓存，再写 Zarr；未见成功后自动释放这些缓存的逻辑 |
| `output.formats.raw_assets: false` | 不另建正式 `raw/` 原文件归档；不等于清理 `_cache/` 的 TIFF |
| 底层 writer 的 `remote_stream_first` | 可直接按源地址读取窗口并写 Zarr，无需先落盘完整 TIFF；但通用 CLI 前面仍会下载缓存，仅改这个配置不能实现整个流程零 TIFF 缓存 |
| 现有十光谱 `tools/run_selected_archive.py` | 已实现验收后释放逐景缓存；它固定十光谱合同，不能直接用于本次 SCL-only 补齐 |
| 历史 16 景 `archive_selected_scl.py` | 已写入独立 SCL Zarr 并校验来源，但保留 TIFF／硬链接缓存，不具备本轮全区生产合同 |
| 新 `backfill_quality` | 单景任务缓存；重新读取落盘 Zarr 全像元验证，接受收据落盘后释放本任务 TIFF；P2 八景真实试点通过，缓存无 TIFF／part 残留 |

**新质量入口采用逐景临时缓存，最终保留 SCL Zarr、目录与溯源／验收记录，不长期保留一套任务 TIFF。** 顺序为：下载或复制复用一景 SCL → 核对大小与 SHA256 → 写入 Zarr → 全像元验收及提交记录 → 释放任务缓存 → 下一景。失败时保留当前景供恢复。此行为仅属于新增入口，不是通用 CLI 的缓存策略；历史源 TIFF、共享缓存与硬链接不在自动清理范围。64 MiB 约束是任务缓存文件上限，不是进程内存上限。

2,430 景源 COG 合计约 5.50 GB，指累计源文件体积，不是采用逐景释放时的同时缓存量。实际峰值是“已写入的 Zarr＋受限的临时缓存＋元数据与工作余量”；Zarr 压缩后体积需试点实测。解码像元总量约 73.24 GB，也不能当成最终 Zarr 的实测占用。

## 在 Tessera 输入准备阶段使用

推荐流程为：**原始十光谱＋配套 SCL → 像元质量判断 → 逐月代表值及有效观测数 → Tessera 的十个光谱通道。** SCL 单独保存，不作为第十一个光谱通道。

原生 SCL 类别图若重投影，使用最近邻；不要双线性插值或平均类别代码。若下游计算 30 m 清晰比例，先转成 0/1 掩膜，0 必须参与面积统计，不能继承 `nodata=0`。源覆盖、SCL 有效覆盖和清晰比例分别记录，明确分母；无有效覆盖返回未知。S2-GRiTS 和质量配对接口不执行这些比例或月合成。光谱合成只使用合格观测，没有合格值的月份保持缺失并记录有效次数。

已完成的 Tessera 应用案例（2026-10-13）采用更严格的策略：30 m空间支持范围完整覆盖，全部相交 SCL 像元为4/5/6且全部贡献光谱有效，才接受该景；逐景定标后生成月代表值与有效掩膜。该策略属于应用层，不是本仓库下载默认值，也不能用一次最近邻类别读取冒充。案例记录保存在工作区 `methods/tessera_windowed/docs/SCL_MONTHLY_INPUTS_20261013_ZH.md`；公开使用无需依赖该实验目录。

不能只保留 SCL=4：非植被（5）可能是真实采伐／裸地状态，水体（6）也不是云。质量筛选和目标地类标签要分开。整景或目标区晴空比例较高，也不能保证每个训练点都清晰。

月度最多一景的既有库，即使补齐 SCL，也只能知道入选景中哪些像元清晰；无法补回该月其他未归档景中的晴空观测。若实际有效月不足，再针对缺口补选场景，保留原库版本。

本项目的 S1 主训练总体仍按原规则准入。SCL 不合格时标记对应 S2 观测缺失，不因此删除整个 P/U 地点×窗口；S2-only 对照继续采用原总体权重分母及覆盖评价，不能另行筛出有 S2 的子总体后沿用原 π。
