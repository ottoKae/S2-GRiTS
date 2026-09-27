# 无损压缩配置与读取

S2-GRiTS 的新建波段数组使用 Zarr v3、Blosc/Zstd level 5 和 byte shuffle，保持原生 DN、dtype、nodata、时间轴及 10/20/60 m 网格。

## 配置

```yaml
output:
  existing_store: resume
  zarr_compression:
    cname: zstd
    clevel: 5
    shuffle: shuffle
runtime:
  chunk_size: 512
```

省略压缩配置时也使用上述默认值。`clevel` 支持整数 0—9，`shuffle` 支持 `shuffle`（字节重排）、`bitshuffle` 或 `noshuffle`。程序根据元素宽度显式设置 `typesize`。空间块默认为 512×512，时间块为 1；小图幅按边界缩小，坐标和标量沿用 Zarr 默认编码。

已有数组续写保留其记录的 codec；配置不同会写入警告。不能只修改旧数组的 `zarr.json` 来转换压缩方式。续写使用已存储的分块边界，源影像与目标 dtype 不一致时拒绝隐式转换并回滚。

压缩不改变 grid ID 或 processing signature，不做量化、降位、空间重采样或时间合成。压缩率和读取成本随数据及访问方式变化，不承诺固定压缩倍数。

## 按需读取

先选择变量、时间和窗口，再加载数值：

```python
import xarray as xr

with xr.open_zarr(
    "D:/data/native.zarr", group="20m",
    consolidated=False, mask_and_scale=False,
) as ds:
    batch = ds[["nir_narrow", "swir_1", "scl"]].isel(
        time=[0, 1], y=slice(1024, 1536), x=slice(1024, 1536)
    ).load()
```

跨传感器读取可以复用 resolver，并批量取同一窗口的多个变量：

```python
from grits_resolver import MultiSensorResolver
from grits_resolver.spatial import TargetWindow

resolver = MultiSensorResolver("D:/data/S1", "D:/data/S2")
pixels = resolver.read_s2_variables_on_s1(
    "17MPU", ["nir_narrow", "swir_1", "scl"],
    s2_time_index=0, resolution_group="20m",
    window=TargetWindow(row_off=1024, col_off=1024, height=256, width=256),
)
```

resolver 根据元数据自动解压，按变量语义重采样。光谱输出仍是 DN，不能把重投影后的 float32 当成反射率；下游应读取来源 `scale/offset` 并处理 nodata。单像元长时序仍需要逐日期解压空间块，可将相邻样本组织为窗口批次以复用读取。

## 转换已有库

停止源库全部写入后，使用独立目标目录：

```powershell
python -B tools/recompress_zarr.py --source "D:/data/original.zarr" --destination "D:/data/compressed-copy.zarr" --report "docs/reports/recompress-001.json" --source-quiescent
```

工具逐逻辑块校验字节，保留数组元数据与 resume sidecar，仅在完成后生成验收收据。目标必须不存在，收据位于两个库之外。失败会保留不完整目标供检查；工具不会删除源库、切换 catalog 或替换生产路径。它明确拒绝未知文件、sharding、consolidated metadata 和不支持的编码。

可重复的抽样比较使用独立临时目录：

```powershell
python -B tools/benchmark_compression.py --source "D:/data/original.zarr" --scratch ".test-tmp/benchmark-001" --report "docs/reports/benchmark-001.json" --scenes 4 --repeats 7
```

抽样副本不是完整产品，不能加入 catalog。测试结果、转换收据和计划只保留本地，分别放在忽略的 `tests/reports/`、`docs/reports/`、`tooling/diagnostics/` 和 `docs/plans/` 下。
