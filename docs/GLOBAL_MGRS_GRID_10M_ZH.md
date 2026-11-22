# 全球 Sentinel-2 MGRS 10 米固定格网

本地生成文件：`data/grids/s2_mgrs_grid_10m.parquet`，不随源码发布。请先取得下述官方来源文件，再按文末命令重建。
包含 **56,686 个分幅**，每幅一行，大小 **2,517,762 字节（约 2.52 MB）**，采用 GeoParquet 1.0 / Zstandard 压缩。

## 来源和覆盖范围

来源是 [Copernicus SentiWiki 的 Sentinel-2 产品页](https://sentiwiki.copernicus.eu/web/s2-products)
所链接的官方 TILPAR 分幅文件，版本日期 **2015-12-09**，本次于 **2026-11-05** 获取。
源 ZIP 保存在 `data/grids/source/S2_TILPAR_20151209.zip`，来源 URL、SHA-256 和输出校验和见
本地生成的 `data/grids/s2_mgrs_grid_10m.provenance.json`。

本表完整保留该官方文件的所有分幅，涵盖南北半球及全部 60 个 UTM 经度分区。
分幅外边界纬度范围约为 **83.836°S 至 84.644°N**，不包含整个两极地区。
该表表示预定义分幅，**不表示每个分幅都存在 Sentinel-2 影像**，也不表示某景覆盖了分幅内所有像元。

每幅的 UTM 范围为 109,800 × 109,800 米，10 米格网为 **10,980 行 × 10,980 列**。
分幅间保留官方重叠范围；不同 UTM 分区使用各自的投影，没有执行全球去重或重投影。

## 字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `tile_id` | string | 五位 MGRS 分幅编号，无 `T` 前缀 |
| `crs` | string | **像元格网**的 UTM CRS，例如 `EPSG:32650` |
| `utm_epsg` | int32 | 同一 UTM CRS 的 EPSG 数字编码 |
| `transform` | float64[6] | Rasterio/Affine 顺序 `[a,b,c,d,e,f]`，定位像元左上角 |
| `width`, `height` | int32 | 均为 10980 |
| `resolution_x`, `resolution_y` | float64 | 均为 10，单位为投影米 |
| `grid_id` | string | 复用 S2-GRiTS canonical v7 的格网哈希算法 |
| `grid_version` | int32 | 格网标识版本，值为 1 |
| `utm_wkt` | string | 官方 UTM 矩形 WKT，坐标系由本行 `crs` 指定 |
| `geometry` | WKB MultiPolygon | 官方 KML 分幅轮廓，统一为 **EPSG:4326，经度/纬度** |

GeoParquet 几何列的 CRS 与每行 `crs` 含义不同：前者用于显示和空间检索，后者用于像元定位。
`geometry` 保留官方 KML 的多边形和日期变更线拆分；其中 298 幅包含多个多边形。
精确像元定位以 `transform` 和 UTM CRS 为准，不从经纬度轮廓反推原点。

本表只保存格网定义，没有逐像元展开，也没有光谱值。
`grid_id + row + col` 可标识该格网中的像元，行列从 0 开始，范围为 0–10979。

## 读取和定位

以下示例从 S2-GRiTS 仓库目录运行，仅用项目现有依赖：

```python
import pyarrow.parquet as pq
from rasterio.transform import Affine, xy, rowcol
from pyproj import Transformer

row = pq.read_table(
    "data/grids/s2_mgrs_grid_10m.parquet",
    filters=[("tile_id", "=", "50SMC")],
).to_pylist()[0]

transform = Affine(*row["transform"])
x, y = xy(transform, 0, 0, offset="center")
print(row["crs"], x, y)  # EPSG:32650 399965.0 3800035.0

to_utm = Transformer.from_crs("EPSG:4326", row["crs"], always_xy=True)
x, y = to_utm.transform(116.5, 34.0)
r, c = rowcol(transform, x, y)
if 0 <= r < row["height"] and 0 <= c < row["width"]:
    print(row["grid_id"], r, c)
```

安装了 GeoPandas 的 GIS 环境也可直接读取：

```python
import geopandas as gpd
from shapely.geometry import Point

tiles = gpd.read_parquet("data/grids/s2_mgrs_grid_10m.parquet")
hits = tiles[tiles.geometry.intersects(Point(116.5, 34.0))]
print(hits[["tile_id", "utm_epsg", "grid_id"]])
```

边界重叠处允许命中多个分幅；检索结果再按各自 UTM 格网定位。
这里不替下游指定重叠像元的归属规则。

## 验证结果

- 56,686 个分幅编号和 56,686 个 `grid_id` 均唯一。
- 全部原生 UTM 几何为 109,800 米方形，原点对齐 10 米；全部 WGS84 几何有效。
- Parquet 写入、回读后记录完全一致，GeoPandas 可直接读取。
- 与本地 S1-GRiTS 的全部 19,002 个分幅比较，UTM EPSG 和矩形范围零差异。
- 在亚洲、欧洲、非洲、美洲、大洋洲和南极抽样，从 Earth Search 的
  `sentinel-2-c1-l2a` 读取 2024 年场景元数据；取得的 **11 个场景全部匹配**
  `blue` 资产的 CRS、仿射变换和行列数，包含高纬度和跨日期变更线分幅。
- 另一个阿拉斯加样本 `03WVN` 在该集合中未查到 2024 年场景，因此未执行场景一致性核对。
- 自动测试使用官方文件中三个真实分幅，验证导出、南北半球坐标、日期变更线拆分和 GeoParquet 元数据，**1 项通过**。

完整验证记录和原始 STAC 响应保存在
本地 `tooling/diagnostics/mgrs-grid-20261105/validation.json`（不随源码发布）。
真实场景核对是抽样验证，未逐幅查询全球影像目录。

## 重建

```powershell
python -B tools/build_mgrs_grid.py --source data/grids/source/S2_TILPAR_20151209.zip --output data/grids/s2_mgrs_grid_10m.parquet
```

生成器直接读取官方 `EPSG` 和 `UTM_WKT`，不根据 MGRS 编号猜测原点。
它复用 `src/s2grits/canonical_catalog_schema.py` 的 `make_grid_id`，因此同一原生
完整分幅的 10 米 Catalog 记录可以按 `grid_id` 对照。
