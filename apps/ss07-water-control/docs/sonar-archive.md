# 声呐采样归档 0.10.0 开发增量

2026-10-23 归档增量，后续接入情况见[迁移与恢复](sonar-migration.md)。提供与滚动诊断历史独立的数据盘采样文件、可重建索引、恢复与清理、受权限限制的查询/下载以及网页入口。0.10.0 为本地候选，尚无该版本完整 IMG；厂家协议和实物串口驱动仍为空。本文的软件验证不替代真实声呐、1 TB 数据盘、掉电或采样频率验收。

## 路径与数据语义

```text
轮询核心 ──┬── 有界诊断历史（可淘汰旧事件）
           └── 独立有界归档队列 → 原事件时刻的 TimeTag → 已核验数据盘 JSONL 分段
                                                       └→ 侧录 + 本地 SQLite 索引
```

`sonar_archive_worker.py` 的队列与诊断写者相互独立。诊断历史满、淘汰或写入失败不会决定归档保留范围。归档库也不存视频帧；原生视频路径不变。轮询只提交有界元数据，慢盘、缺时码和索引写入不会同步堵住串口轮询。

生产数据路径位于已核验的 `/data/ss07/recordings/sonar/<collector_instance>/<part>.jsonl`，邻接 `<part>.jsonl.json` 为侧录；本地索引建议 `/var/lib/ss07/sonar/archives.sqlite`。卷 UUID、非系统分区、精确挂载、可写和目录身份由既有 `MountLease` 核验，随后使用固定目录/文件 FD。未取得真实卷前不创建采样目录；拒绝路径穿越、符号链接、硬链接和嵌套挂载。

每个 JSONL 文件首先保存版本、归档 ID、卷、RK boot、采集实例、证据类型、epoch 和 mapping 身份，后续每行是一条带 TimeTag 的原事件。接收时间、UTC、距离有效性与原因、样本/请求 ID 和 u64 字符串均保留，不从最新时码回填旧样本。时码无法关联时保存 `UNAVAILABLE`，仍保留原始测距事件。主机接收时刻不等于声呐发射或内部测量时刻。

一个分段只对应同一 boot、采集实例、epoch 和 mapping。任一身份切换、配置分段时长到达、8192 条事件或 8 MiB 文件上限均切段。每批最多 32 条并 fsync；完成时流式校验每行与 SHA256，先持久化侧录，再提交索引。`state=COMPLETE` 仅证明该文件完成关闭和校验；`contains_gap`、`known_lost_events` 单独说明采集缺口，不能由“文件已完成”推断全场无丢样。

归档队列最多 256 条。排队溢出或某批无法确认落盘会增加 `dropped_events`，恢复后持久化 `HISTORY_GAP`。无法确认的批次可能仍有部分字节留在中断文件中，计数不构成精确的物理丢样测量。写入阻塞超过 1 秒报告异常，已写字节和中断文件保留，进程恢复时处理；未提供永久无丢失保证。

## 恢复、锁定和保留

`sonar_archive.py` 实现索引与卷侧录核对、逐条解析、文件身份和摘要检查。API 和清理均只接受不含路径的 64 位十六进制归档 ID。列表返回已登记且尚未回读校验的状态，读取/导出时重新校验整个文件；索引中不向客户端暴露内部路径或 inode。

- 丢失本地索引后可从该卷的有效侧录重建，保留锁定版本和原 TimeTag，不重新关联历史时码。
- 缺少完成侧录的文件，包括只有有效前缀或尾部截断的文件，一律登记为 `INCOMPLETE` 并默认锁定。不会因为 JSON 能解析而自动补成完整文件。
- 恢复发现内容或侧录不一致时登记 `INVALID`，文件缺失登记 `MISSING`。失败数可在归档状态与告警中查到。
- 锁定使用 `expected_revision`，冲突不覆盖。当前通过离线本地命令操作，网页只显示锁定状态。维护命令同时持有声呐进程锁和归档 owner 锁；另选一个 state-dir 也不能绕过正在运行的归档写者。
- 清理只处理本卷、已完成、未锁定并通过整文件核验的分段，每批最多 64 个；可按保留天数清理，空间不足时清理至 reserve 加一个分段的余量。损坏条目标记异常，避免长期占住候选队列。
- 删除前先将删除意图写入卷侧录，再更新索引并删除文件/侧录。中途重启可继续完成删除，不把半删除记录重新登记为完成。未完成与锁定文件不自动清理；无可清理空间时停止归档并报告告警。

索引上限 200000 条、数据库文件上限 256 MiB，删除墓碑最多 10000 条；达到不可回收容量时告警并停止，不能无限增长或覆盖未过保留条件的数据。这些是软件上限，不保证标称 1 TB 盘的可用空间、吞吐或实际保留天数。录像和声呐各自检查同一卷空间，实际并发写入与满盘竞争仍待实盘验收。

## 配置和运行入口

系统 JSON envelope 仍为 `schema_version=1`，新增可选项，缺省为 false：

```json
{"storage":{"sonar_recording_enabled":false}}
```

开启声呐归档与视频的 `recording_enabled` 独立，均要求明确的文件系统 UUID。声呐沿用 storage 的 `segment_seconds`、`reserve_bytes` 和 `retention_days`。当前用户尚未接入数据盘，默认保持关闭，UUID 为空。时码的 500000 baud 不作为声呐串口参数。

厂家 Profile、设备绑定和数据盘都准备好且获得实际部署指令后，进程入口为：

```sh
./tools/ss07 sonar-worker --config /etc/ss07-active/system.json \
  --state-dir /run/ss07 --sonar-journal /var/lib/ss07/sonar/samples.sqlite \
  --sonar-catalog /var/lib/ss07/sonar/archives.sqlite
./tools/ss07 serve --config /etc/ss07-active/system.json \
  --state-dir /run/ss07 --sonar-catalog /var/lib/ss07/sonar/archives.sqlite
```

以上是两个独立前台进程，需分别运行；本次未在实际设备执行。厂家 Profile 未配置/不可用时，声呐进程报告不可用，不启动归档写者或打开串口。后续开发增量已增加[systemd 服务配方和恢复接入](sonar-services.md)，配置协调器仍拒绝厂家驱动缺失时的声呐变更与归档开启。

停止声呐进程后，可按当前配置离线恢复或锁定：

```sh
./tools/ss07 recover-sonar-archives --config /etc/ss07-active/system.json \
  --state-dir /run/ss07 --sonar-catalog /var/lib/ss07/sonar/archives.sqlite
./tools/ss07 lock-sonar-archive --config /etc/ss07-active/system.json \
  --state-dir /run/ss07 --sonar-catalog /var/lib/ss07/sonar/archives.sqlite \
  --archive-id '<archive_id>' --locked true --expected-revision '<lock_revision>'
```

服务状态与索引目录已有安装配方，真实卷仍需按[声呐服务说明](sonar-services.md)准备独立写入子目录及所有者。本次未授予板端新用户或修改系统服务。

## API 与网页

| 接口 | 行为 |
| --- | --- |
| `GET /api/v1/sonars/archives` | 分段列表，方向、epoch/外部时码、分页；盘离线仍可读本地索引 |
| `GET /api/v1/sonars/archives/{id}/samples` | 校验整段后分页返回原始事件和标签，按方向/epoch/外部时码筛选 |
| `GET/HEAD /api/v1/sonars/archives/{id}/content` | 校验整段后读取 JSONL；Range、If-Range、摘要 ETag、`download=1`；下载中检查读取权限 |
| `GET /api/v1/status` 中 `sonar_service.archive` | 归档启用、状态、原因、有界队列、未确认写入数、已写数、最后提交与当前卷 |

列表和样本查询支持 `limit` 1～200、`cursor`、`direction`、`source_epoch`、`start_us`、`end_us`。任何外部时间范围都必须显式提供非零 epoch，端点均含；所有 u64 按字符串处理。两个端点的游标类型不同，只使用各自响应的 `next_cursor`，不要混用。

monitor 无权读取采样历史；reader/maintainer 可查询和下载，维护写入暂只提供上述本地离线命令。网页“声呐采样记录”提供筛选、前 20 条匹配记录预览和分段导出，标明测试来源、未完成、缺口和锁定。当前实时区继续保持无实物距离；不能用归档测试数据填充当前设备状态。

## 验证和交付边界

软件测试覆盖分段完整回读、独立诊断淘汰、旧 epoch/u64 精度、慢盘与缺盘、重建索引、损坏与缺失、锁定及保留、删除中断、FD 固定、路径/硬链接拒绝、HTTP Range/HEAD、权限、离线维护互斥和旧部署契约拒绝。全量 216 项测试分别在开发机 CPython 3.8.20 和 ARM64 Python 3.8.10 / SQLite 3.31.1 隔离用户态通过。真实 CLI/API 进程联测确认未配置与未知协议均无设备打开尝试、无测距事件、正常退出；这不是电气层的零 TX 测量。日志和摘要见 [验证汇总](evidence/sonar-archive-validation.json)。

可在开发机重新运行实际浏览器联测，输出目录必须不存在，Node 与 Chromium 仅为开发测试依赖：

```sh
python3 tools/test-sonar-archive.py --output /tmp/ss07-sonar-archive-test \
  --chromium /home/forlinx/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell
```

该工具用明确标注 `synthetic_test` 的隔离卷写入 20 条事件并跨两个大整数 epoch，浏览器校验查询值未舍入、预览、真实下载文件的摘要与条数、实时距离仍为空。它不打开声呐或时码串口，不对外发布服务，不写主机数据盘。

0.10.0 的部署数据兼容契约将 config 升为 2，并登记声呐诊断、归档索引及侧录 schema 1。该 config 数字是兼容修订，与 JSON envelope 版本分开：旧程序不认识新增归档字段，不能假称仍满足 0.9.0 的同契约更新/回滚。后续已接入服务配方和[明确的双向配置迁移](sonar-migration.md)，仅接受两套精确认识的契约；未知契约和 schema 仍拒绝。降级先关闭归档，保留声呐数据及可恢复的配置历史备份。本文 216 项测试保留为归档阶段证据；后续验证见迁移记录，尚无板端实际权限证据。现有 0.9.0 应用包和 0.6.1 IMG 继续保留。
