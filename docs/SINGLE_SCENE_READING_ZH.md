# 单轨道、单景读取

MGRS 瓦片是空间容器，不是轨道或采集身份。同一瓦片中的不同相对轨道、不同日期和不同产品各自保留。S2-GRiTS 延续 [GRiTS-Resolver 的 S1 方案](https://github.com/ottoKae/GRiTS-Resolver/blob/main/README.md)：列出所有轨道，明确选择来源，逐轨道读取；读取层不自动补洞或拼接。

## 身份与存储

| 字段 | 含义与来源 |
| --- | --- |
| `tile_id` | MGRS 分块，例如 `49QHE` |
| `relative_orbit` | 相对轨道 `1–143`，优先读取 STAC `sat:relative_orbit`；未提供时读取官方 `s2:product_uri` 中的 `_R032_` 等字段 |
| `item_id` | 单个已提交的 STAC 产品，决定精确的时间索引 |
| `datatake_id` | 源 `s2:datatake_id` 原文 |
| `acquisition_id` | 卫星 + datatake 开始 UTC + 绝对轨道；不含处理基线，与 Resolver 的采集身份规则一致 |
| `platform` / `collection` | 源卫星和产品集合 |

有多个身份字段时必须一致，不能把相对轨道与绝对轨道混用，也不能按日历日期代替 datatake。源未提供轨道方向时保持未知。产品命名和 `R001–R143` 范围见 [Copernicus Sentinel-2 产品说明](https://sentiwiki.copernicus.eu/web/s2-products)。

归档目录和数组保持兼容：每个瓦片一个原生 Zarr，`10m`、`20m`、`60m` 分组，每个变量为 `(time,y,x)`。每条时间记录是一个场景；相同时间戳的两个产品仍是两条观测。轨道筛选是索引操作，无须复制整套像元或重写旧库。新的身份信息保存在 `s2grits_resume_state.json` 的逐景记录；canonical catalog 的 `track` 保存相对轨道，`flight_direction` 保存已知方向。

旧库没有新增身份字段时，`list_tracks()` 将每个未知轨道场景分别列出，`read_scene(item_id=...)` 仍可读取；不会把未知场景归为同一轨道，也不会从日期、文件位置或瓦片猜轨道，或修改旧库补写元数据。

## 读取

```python
from s2grits.reader import list_tracks, list_scenes, read_scene

store = "outputs/pilots/49QHE-20261021/49QHE/scenes_L2A_native/zarr/s2grits_scenes_49QHE_L2A_native.zarr"
print(list_tracks(store))

# 一个相对轨道的时序：保留每个场景及其时间，逐景读取同一个空间窗口。
for scene in list_scenes(store, relative_orbit=132):
    patch = read_scene(store, "20m", item_id=scene["item_id"], relative_orbit=132,
                       variables=["nir_narrow", "swir_1", "scl"],
                       window=(100, 100, 256, 256))
    print(patch["scene"]["datetime"], patch["scene"]["acquisition_id"])
```

- `read_scene` 必须得到唯一场景。只传轨道但有多景时拒绝；传入冲突的 `item_id` 与轨道时拒绝。
- `window=(row,col,height,width)` 相对于所选原生组。10 m 与 20 m 组相同行列不是同一地面窗口；应用应按各组的 CRS、transform 转换。
- `data` 是原生整数 DN，不自动缩放；普通变量的 `valid` 根据源 nodata 计算，不等同无云掩膜。SCL 分类需显式使用。
- 云概率 `0` 表示 `0%`，不能直接视为缺测。Earth Search 的 CLD COG 同时声明 `nodata=0`，因此读取 `cloud_probability` 时使用**同一场景、同一 20 m 组的 `SCL!=0`** 确定覆盖，并要求概率在 `0–100` 内；缺测区不会从另一轨道补齐。原始 DN 与原始 nodata 元数据保持不变。
- `validity_method` 和各变量的 `band_metadata.validity_method` 声明掩膜来源。缺少同景 SCL 且源声明 `nodata=0` 时使用 `conservative_source_nodata_zero_ambiguous`，并返回 `validity_note`：被排除的零值可能是无云，不能断言是缺测。SCL 的分类定义见 [Copernicus 处理说明](https://sentiwiki.copernicus.eu/web/s2-processing)。
- 每次读取返回所选场景、变量元数据、CRS、窗口 transform 和像元中心坐标。不同波段均来自同一场景。
- 提交记录和所有组的时间轴、数组长度必须一致。发现未提交尾部或读取期间提交记录改变时拒绝，等待写入完成后重试。建议在写入完成后读取；侧车前后检查不等同跨进程写锁。
- 不自动执行跨轨道合成、跨日期合成、最优景选择或跨分辨率重采样。需要合成时应在下游明确指定来源优先级，并保存所选来源索引。

## 深圳大学测试

粤海校区参考点为 WGS84 `113.9314749, 22.5359023`，标准 100 km MGRS 瓦片为 **49QHE**。GRiTS 的扩展格网有边缘重叠，因此包含该点的扩展多边形可能不止一个；这不改变校区的标准 MGRS 归属。

本次使用公开 [Earth Search](https://element84.com/earth-search) 的 `sentinel-2-c1-l2a` HTTPS COG，冻结两个原生完整产品：

| 相对轨道 | 场景 | 蓝波段整格有效覆盖 | 校区 32×32 窗口有效像元 |
| --- | --- | --- | --- |
| R132 | `S2B_T49QHE_20241126T025722_L2A` | 82.0211% | 0 / 1024 |
| R032 | `S2B_T49QHE_20241129T030828_L2A` | 55.0541% | 1024 / 1024 |

每景保留完整的 `10980×10980`（10 m）、`5490×5490`（20 m）、`1830×1830`（60 m）网格。轨道条带未覆盖区域保持源 nodata；两个轨道不能仅为填满整格而自动合并。冻结源、逐波段源像元比较和校区窗口验证记录保存在本地 `docs/reports/evidence/49QHE-20261021/`；下载与 Zarr 保存在 `outputs/pilots/49QHE-20261021/`。这些运行材料不随源码发布。

两个产品各含 12 个光谱波段、SCL 和云概率，共 28 个变量时相。全部 **1,460,120,400 个变量像元**在 **5,872 个窗口**中与源 COG 逐值相等，28 个源文件的 SHA-256 与 STAC 声明一致。两个场景重复运行均跳过；读取与重复提交前后的整个 Zarr 内容摘要一致。Zarr 为 875,346,516 字节，保留的源 COG 为 1,387,863,179 字节。

整格覆盖更高的 R132 在校区没有数据，R032 则有完整窗口。这说明选景应检查目标区域的有效覆盖，不能只按整格覆盖排名；R132 的校区窗口仍保持缺测。

验收额外检查两幅云概率的全部 **60,280,200 个掩膜像元**。使用同景 SCL 后，R132 云概率有效像元为 24,721,236，R032 为 16,593,372；R032 校区的 0% 云概率也正确保留，窗口有效数为 1024。最初按 COG `nodata=0` 得到的保守统计保存在 `acceptance-summary-initial-source-nodata.json`；最终 `acceptance-summary.json` 同时列出原始 nodata 掩膜计数、修正的读取掩膜计数及来源，不改写像元。
## 同瓦片日合成

在原始单景读取之外，GRiTS-Resolver 0.3 的 `OpticalDailyResolver` 支持同一 MGRS
瓦片内按 UTC 日合成。它调用本仓库 `read_scene`，逐像元选择质量合格来源，
全部请求波段及 QA 来自同一景，并保留来源索引、轨道和精确时间。原始归档不变。
10/20/60 m 分组分别读取；质量筛选使用同景 SCL，云概率有效零值保留。
用法见 [Resolver README](https://github.com/ottoKae/GRiTS-Resolver)。

