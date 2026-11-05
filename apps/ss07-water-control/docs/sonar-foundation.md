# 声呐公共框架与诊断历史

本文件保留诊断框架阶段记录；后续 0.10.0 已增加独立的[数据盘采样归档](sonar-archive.md)，本机运行与交付范围以该说明为准。

2026-10-23 工作区开发增量，尚未发布到应用包或 IMG。当前可运行独立的声呐进程与状态/历史 API，厂家驱动注册表为空：未配置或不认识的协议均保持 `UNAVAILABLE`，不打开串口、不产生测距值。已有 0.9.0 应用包和 0.6.1 IMG 不包含此增量。

用户已确认当前没有声呐。时码适配器的 **500000 baud 只属于独立时码接口**，不能作为声呐 UART 默认值；名义 1 TB 录像盘尚未连接。五方向沿用 front/back/left/right/down，厂家地址、单位、量程和测量时序待实物资料确定。

## 模块与接口

| 模块 | 职责与边界 |
| --- | --- |
| `sonar_driver.py` | 固定厂家/适配器 Profile 注册表。只接受源码登记的协议标识，不按配置动态导入代码。当前没有可用 Profile |
| `sonar_core.py` | 单总线一个在途请求、公平轮询、节点超时、应答匹配、过期与重连失效；纯逻辑，不含串口或厂家报文 |
| `sonar_worker.py` | 独立进程、进程锁、重连退避、受限读写/解析调用、状态发布；历史写入与采样循环分离 |
| `sonar_journal.py` | 有界异步队列、按原接收时间请求时码标签、SQLite 事务、缺口与采集进程生命周期、只读分页查询 |
| `sonar_status.py` | API 校验配置摘要、boot/instance、厂家 Profile、真实/测试标记与时间；每次请求重新计算年龄 |
| `sonar_http.py` | `/api/v1/sonars/history`，沿用录像读取权限；拒绝无 epoch 的外部时码筛选 |

`Profile.prepare(config)` 在打开设备前验证参数并返回规范化的五方向地址。规范化后仍检查重复地址；未配置节点保留 null。`Profile.codec()` 返回 `encode(Request) -> bytes`、`feed(bytes) -> list[Reply]`、`reset()`；厂家编解码器负责长度、校验、单位换算和内部缓存上限，坏帧不能输出测距 Reply。`open_transport(config)` 必须验证稳定 USB 身份、相机复合串口排除、与时码独立的物理适配器、独占访问、UART 回读以及经核验的收发方向控制。当前未实现任何实物传输适配器。

轮询核心只接收统一的 `Reply(address, request_id, valid, distance_mm, reason, quality)`。有效距离使用整数毫米，真实零距离可以是 0；无效距离必须为 null 并有 reason。厂家质量未知为 null，原始厂家测量时间尚未提供，因此 `measurement_time_us` 始终为 null。

Profile 必须提供 `Timing` 的应答超时、最晚应答窗口、总线静默、最小节点周期、过期门槛和应答关联方式；没有生产默认值。带线上请求 ID 的协议要求 ID 与地址都匹配。没有请求 ID 的协议必须具有可证明的最晚应答窗口，包含主机写入、USB 缓冲、线上发送及设备应答延迟；完整窗口结束前不发下一请求。无法证明该窗口的协议不能启用。重连立即清除旧值有效性，重开后同样等待完整窗口；部分写入关闭并重建连接，不续发残帧或立即重试当前节点。

单次读最多 1024 字节、命令最多 256 字节、一次解析最多 32 个 Reply。单节点超时后轮到其他方向，不在该节点无限重试。设备重连退避 1～30 秒。Python 只处理这些有界消息与元数据，视频字节仍由现有原生媒体进程处理。这些上限与测试通过不构成实际采样频率或实时性保证。

## 时间与历史语义

测距事件的 `received_monotonic_us` 是包含完整应答帧的主机读取时刻，`timestamp_basis=sonar_receive`，不称为声呐发射或内部测量时刻。超时、总线事件和生命周期使用 `status_observation`。每条事件保存 UUID sample_id、RK boot_id、collector_instance_id、方向、地址、请求 ID、序号、UTC、原始单调时间以及证据类型；u64 使用十进制字符串。

后台写者使用事件的原始单调时间调用既有时码服务，而不是用排队后最新的 epoch。TimeTag 的 boot、事件时间、时间依据、证据类型必须一致。关联仍为 `ESTIMATED` 或 `UNAVAILABLE`；缺时码不阻塞轮询，不回填历史。生命周期序号 0 是控制记录，实际采样与总线事件在进程内递增。

当前 SQLite 是**最近诊断历史**，不是全程声呐记录归档：

- 队列最多 256 条；每事务最多 32 条；正常情况下用约 100 ms 窗口合批。
- 最多保留 65536 条事件，数据库文件上限 128 MiB；达到空间水位会提前淘汰旧事件。SQLite 事务日志可能另外占用临时空间。
- 返回 `complete_history=false`、`data_disk_archive=false`、`evicted_events` 和 `known_lost_events`。淘汰与已知丢失分开统计，不承诺固定保留时长。
- 写入阻塞不拖住轮询；队列溢出或写失败累积丢失计数，恢复后写入 `HISTORY_GAP`。状态显示写失败或写入阻塞。
- 启动事务记录未关闭的采集实例，正常退出清除。异常退出后下一实例写 `unclean_previous_session`；无法计数的掉电损失不会伪造为精确条数。

诊断库路径由本地 CLI 明确指定，应位于可信目录。API 只读打开，不创建缺失的数据库。0.10.0 的数据盘归档另走独立队列和文件路径，见后续说明；不能将此滚动窗口替代数据盘采样记录。

## 本地运行

在项目目录直接验证两个实际进程，输出目录必须不存在：

```sh
cd /home/forlinx/workspace/LubanCat_SDK/device/rockchip/apps/ss07-water-control
python3 tools/test-sonar-foundation.py --output /tmp/ss07-sonar-smoke
```

工具在临时回环端口启动 API 和声呐 CLI，分别验证未配置与未知协议，读取五方向状态和历史、正常停止进程，并保存 Python 设备 open 审计。测试没有模拟设备驱动，不修改主机 systemd 或板端。open 审计不是电气零 TX 测量。

手动运行时，两个终端使用相同的开发目录。第一终端：

```sh
SS07_SONAR_DEMO=$(mktemp -d /tmp/ss07-sonar.XXXXXX)
printf '%s\n' "$SS07_SONAR_DEMO"
./tools/ss07 sonar-worker --state-dir "$SS07_SONAR_DEMO/state" \
  --sonar-journal "$SS07_SONAR_DEMO/history.sqlite"
```

第二终端将 `SS07_SONAR_DEMO` 设为上面打印的目录，再运行：

```sh
./tools/ss07 serve --state-dir "$SS07_SONAR_DEMO/state" \
  --sonar-journal "$SS07_SONAR_DEMO/history.sqlite"
```

使用默认未配置参数时，五个距离均为 null，原因 `sonar_not_configured`；历史只包含真实进程生命周期，不包含虚构测量。两终端分别 Ctrl-C 停止。启用账号时，监看角色能看当前状态，reader/maintainer 才可读取诊断历史。

历史 API 支持 `limit`（1～200，默认 50）、`cursor`（返回的 next_cursor）、`direction`、`source_epoch`、`start_us`、`end_us`。时间范围必须带非零 epoch；按插入 ID 倒序分页。非法查询返回 422，库缺失/损坏返回 503。跨越重启或 epoch 的查询保持原标签，不将历史测试数据变为当前设备状态。

```sh
curl 'http://127.0.0.1:8077/api/v1/sonars'
curl 'http://127.0.0.1:8077/api/v1/sonars/history?limit=20&direction=front'
curl 'http://127.0.0.1:8077/api/v1/sonars/history?source_epoch=11&start_us=9000&end_us=10000'
```

## 验证与后续接入

新增 28 项声呐与权限测试，完整 194 项在开发机 CPython 3.8.20 和隔离 ARM64 CPython 3.8.10 / SQLite 3.31.1 通过。覆盖公平轮询、迟到/重复/错地址、部分写入、失效年龄、重连、诊断库容量、事务回滚、慢写缺口、异常退出标记、旧 epoch 关联、u64 精度、HTTP 参数与角色权限、未知协议和 CLI 正常退出。测试测量明确标记 `synthetic_test`；专门构造的生产格式测试对象仅用于校验聚合契约，不作为设备证据。验证汇总见 [声呐框架证据](evidence/sonar-foundation-validation.json)。

现有 systemd target、安装/升级数据契约和配置应用清单尚不包含声呐进程；声呐配置应用仍明确拒绝 `sonar_driver_unavailable`，不能显示为已生效。本增量提供手动运行与验证路径，未生成新发布包。继续接入需完成厂家协议及 USB-RS485 型号/参数核验、真实驱动与独立串口绑定、数据盘实际验收、声呐服务权限与升级迁移/恢复契约，以及五节点实测和声学互扰验收。
