# S2/SCL 增量生产记录与配对版本

本说明介绍原生光谱与独立 SCL 的生产记录、恢复和配对范围。可复用验收逻辑保存在 [测试目录](../tests/README.md)，本机执行报告保存在忽略的 `docs/reports/`，不作为安装依赖。

## 1. 这次增加了什么

开启可选生产记录后，每次实际写入都留下来源、参数、代码摘要、场景身份、分辨率组、时间索引和写入代次。元数据位于数据根的 `provenance/`，原 Zarr 数组结构与数值处理方法保持不变。已有场景默认跳过；明确要求替换时，仅修改选中的同身份场景。

| 层次 | 保存内容 | 能证明什么 |
| --- | --- | --- |
| 公共文档 | 本文、配置模板、测试源代码 | 当前功能怎样使用及其边界 |
| 生产意图与收据 | S2：`provenance/runs/<operation>/`；SCL：`_task_state/intents/` 与 `accepted/` | 本次准备使用和实际写入的来源、参数、代次；SCL 收据另归档至 release 的 `evidence/quality/` |
| 不可变元数据版本 | `provenance/releases/<metadata_release_id>/` | 输出与来源的关联及发布摘要 |
| 当前指针 | `provenance/current.json` | 已完整发布的元数据版本 |
| 独立 SCL 配对 | 质量根的 `provenance/pairings/` | 哪些旧质量场景通过了对新光谱版本的重新核验 |

旧数据缺少原执行证据时，仍明确标为未知或历史观察。当前源码不能证明它们当年怎样处理。并非给整个历史库补写“已验证”的处理历史。

## 2. 开启原生光谱生产记录

复制 [配置模板](../config/provenance_native.example.yaml) 到 `config/local/`，修改区域、时间、输出路径和两个身份：

```yaml
metadata:
  provenance:
    enabled: true
    dataset_id: ecuador-s2-native
    run_id: append-202601-v1
output:
  existing_store: resume
  existing_item: skip
```

- `dataset_id` 是这份数据集的稳定名称，发布后不能改名。值必须为安全字符串。
- `run_id` 是本批次身份。同一批次恢复沿用原值；新时间、新来源或明确替换必须使用新值。不要在未完成批次中换代码、依赖或参数。
- 入口仍为 `s2grits process_scenes --config config/local/s2.yaml`。执行此命令会实际生产数据。
- 首轮支持原生分组、Zarr 单一输出、本地来源缓存、串行场景；波段可并行。必须关闭预览、额外 TIFF 输出、月合成和云掩膜。SCL 使用独立质量流程。
- 省略或关闭 `metadata.provenance.enabled` 保留原工作流。已有记录的库应持续开启；用不留痕的工具修改后，原记录会过期，需要明确核查，不能继续当作有效版本。

现有同 ID 场景默认 `skip`：不重下、不重写像元，也不把最新查询到的资产冒充成旧场景的来源。一个只有 skip 的批次不会为历史影像制造新的生产代次。

## 3. 追加、替换与恢复

追加使用新批次、`existing_item: skip`。新增场景写入新时间层；旧场景的代次与来源保持不变。已发布记录以场景身份、分辨率组和网格定位，不靠日期猜来源。

替换必须同时开启记录、设置 `existing_item: overwrite`，并使用新 `run_id`。只支持已存在的同 `collection/tile/item_id/datetime` 场景；波段、dtype、网格和索引需兼容。不支持用一个新产品 ID 偷换旧身份，也不移动旧时间索引。一次真实替换即使像元值完全相同，也生成新的 `generation_id`。

**替换会改变目标场景，没有自动影像备份。** 本轮没有对用户生产库执行替换。实际生产替换前，应保留可恢复的数据副本或确实可用的原资产。

提交顺序为：持久化意图与 pending → 原生数组及提交记录 → 写入收据 → tile/global Catalog → 元数据 release → current → 清除 pending。

| 中断位置 | 恢复行为 |
| --- | --- |
| 尚未写影像且无意图 | 修正输入后可重新执行 |
| 已有意图，缺少可靠收据 | 保留 pending，拒绝自动重写；不能自行删除 pending 后继续 |
| 已提交影像和收据，Catalog 或元数据未发布完 | 原批次恢复，只完成未完成的发布；不重下、不重写影像 |
| 当前指针已发布，尚未清 pending | 核对该次收据、版本和 Catalog 后完成收尾 |
| 外部文件、发布基准或第三方状态变化 | 明确拒绝，保留现状，不覆盖其他任务的版本 |

工作流复用数据根的生产锁；pending 同时阻止普通流程继续写。配套 Resolver 已完成 P4 的读取前后 pending／版本检查，详见 [读取说明](https://github.com/ottoKae/GRiTS-Resolver/blob/main/docs/PROVENANCE_READING_ZH.md)。生产与读取仍应分阶段进行，不能在写入或恢复期间强行读取该库。

## 4. 能回答哪些数值问题

光谱意图区分三件事：

1. STAC 声明：collection、产品 ID、采集时间、处理基线及 `raster:bands` 的 scale/offset。
2. 实际输入：本地源文件 SHA-256、字节数、Rasterio 读取到的 dtype、nodata、scale、offset。
3. 实际执行：本路径按窗口读取原始存储值并写入同 dtype 的原生 Zarr，**没有执行 scale/offset 数值校正、harmonization、重采样或 nodata 数值替换**。

记录 scale/offset 不表示应用过它。上游是否已经应用 BOA_ADD_OFFSET、做过怎样的生产校正，未取得原产品证据时仍是未知；不能从本地复制操作反推上游算法。不同产品的声明分别保留，不能把一个处理基线推广到全部年份。

输出表、来源表和关联表分别是 `outputs.parquet`、`sources.parquet`、`source_links.parquet`。仅实际使用的输入记录为 `used`。canonical Catalog 的 schema 不变，`source_assets_json` 增加观察到的资产 SHA 和写入代次，用于识别光谱更新后的旧质量绑定。

S2 的 `data_revision` 结合输出代次、提交记录摘要和数组结构／时间坐标见证；它**不是全库像元校验和**。结构见证中的 `zarr.json` 键表示规范化属性和数组布局摘要，不等同于该文件原始字节的哈希。外部直接改像元但不改结构，不能仅凭该版本号发现。

## 5. SCL 的生产记录

在**新建**独立质量任务的 YAML 中，可选加入：

```yaml
metadata:
  provenance:
    enabled: true
    dataset_id: ecuador-s2-quality
    run_id: scl-frozen-job-v1
```

仍通过 `backfill_quality` 或原 `process_pipeline` 调用。SCL 保持 `uint8`、原始类别和 20 m 网格；源文件与落盘 Zarr 的分块全像元比较通过后，才接受并释放任务缓存。新收据记录像元代次、源版本和解码像元摘要。

`companion.json` 的发布计数不是影像代次。只有重新绑定或补元数据，不应给已有 SCL 伪造新的像元代次。旧接受记录缺少 generation 时保留未知。

**旧冻结任务不能直接修改配置或源码摘要后 resume。** 旧 binding 绑定配置、代码和环境；升级代码或给旧 YAML 加字段都可能使其失配。继续旧下载任务需使用原绑定版本；对新的光谱版本复用旧 SCL，使用下面的显式配对入口，保留旧绑定不动。

## 6. 光谱更新后重新核验配对

为新的光谱版本准备独立 YAML，引用对应的冻结场景清单、资产摘要、当前光谱 Catalog 摘要及原质量根。现有 [质量模板](../config/quality_companion.example.yaml) 中所有路径和摘要占位符必须替换；它不是可直接运行的生产配置。

```powershell
s2grits rebind_quality --config config/local/scl-new-version.yaml --run-id pairing-v2 --dry-run
s2grits rebind_quality --config config/local/scl-new-version.yaml --run-id pairing-v2
```

此入口不下载影像、不覆盖旧 binding、companion、Catalog、收据或质量像元。它核验两份数据的稳定状态后，写出独立配对 release，最后更新 `provenance/pairings/current.json`。

每条关联分别保存光谱 10 m、光谱 20 m、SCL 20 m 的 store、group、time_index 和 grid_id；三个时间索引可以不同。SCL 只需与对应光谱 20 m 网格一致。

只有来源版本、源 SHA、时间、网格和当前像元完整性可确认时，旧 SCL 才可复用。新收据使用解码像元摘要；旧收据没有摘要时需保留的原始来源逐像元复核。可逐景判定的缺证、来源变化或摘要不符记录 `quality_available=false` 及原因；结构／身份契约冲突、旧来源全像元复核失败等情况会中止整次发布。两者都不会猜测为晴空或自动补下载。

**配套 GRiTS-Resolver 已支持 P4 新配对读取。** 使用 `MultiSensorResolver(..., s2_quality_root=..., s2_quality_pairing="current")` 显式选择；默认 `"legacy"` 保留旧 v1。`rebind_quality` 发布后不会自动替模型读取器切换版本，应重建相应 Resolver；不手工覆盖旧文件。用法与迁移根映射见 [读取说明](https://github.com/ottoKae/GRiTS-Resolver/blob/main/docs/PROVENANCE_READING_ZH.md)。

## 7. 交接与边界

保持数据根、各 Zarr、Catalog、`provenance/` 和 SCL 的原任务证据相对结构，连同本说明交接。发布表内使用相对 store 路径，公共来源 URL 去除临时认证参数；原任务配置／旧日志仍可能含本机路径，不能把整个工作目录无筛选上传。

公共契约使用 schema v1，与 S1 的规范模块固定摘要对应，并有一致性测试。S2 不依赖导入本机 S1 源码运行。本轮没有升级 Zarr 存储格式或增加依赖。

已验证本地小库及故障注入，尚未验证实际生产规模下的吞吐、元数据增量体积或跨网络文件系统行为。元数据模块覆盖 Windows 长路径；原生影像 writer 仍宜使用较短输出根。本地迁移读取和新 Resolver 配对消费已由后续 P4 小库验收覆盖；自动 ZIP、全量交付和其他并发生产模式仍不属于本轮生产功能。
