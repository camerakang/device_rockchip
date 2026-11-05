# 只读时码接收与片段关联 0.5.0

下文保留 0.5.0 阶段的实现与验证记录。后续权限维护、ARM64 和完整 IMG 的进度见 [交付跟踪](delivery-progress.md)；实物时码与声呐验收仍未完成。

2026-10-23。本版本实现 USB-RS485 只读接收、0x12 解码、历史映射、录像片段通知标签、按 epoch 查询和有界审计记录。软件测试通过不等于真实适配器接入；当前绑定和部分 UART 参数未确认，生产配置保持不可用。用户确认的速率为 **500000 baud**。声呐实物与厂家协议尚缺，`sonar_receive` 标签接口已定义，声呐采集及持久记录尚未实现。

## 协议依据与模块

对照协议库 `timecode/README.md`、`timecode/reference.py` 及 `mobocom_realtime/PROTOCOL.md`。本地设计基线为 `c6f23e003cf1c3aeb27a488d152b73327b1827f0`，本次核对的远端设计版本为 `03b33695c2e3e9e81d4faa6213fbe1c9fb3f7697`；0x12 时码参考实现一致，其 SHA256 为 `53bf59b40db120516808c8f6ed141f62a1c6ebd487fa10292ab47b764d5ad52c`。六组参考实现生成的测试向量及来源保存在 `tests/fixtures/timecode-golden.json`。未修改协议仓库，也未引入控制或时间轴播放逻辑。

0x12 帧固定 26 字节：D5 7C、版本 12、flags=0、非零 epoch u64、小端 seq u32、time_us u64、CRC16/CCITT-FALSE。CRC 覆盖字节 2～23，尾部校验值为小端。发生器周期 10000µs；同 epoch 的前进步数 k 必须在 1～2^31−1，时间增量为 9950k～10050k。seq 可以按 u32 回绕，epoch 和时间均不能据此回绕或混用。坏帧、重复、旧序号、错误时间步长不会刷新有效年龄。

| 模块 | 职责与边界 |
| --- | --- |
| `serial_receive.py` | 核验稳定 USB 串口身份、独占只读打开、显式 termios 参数；没有发送接口 |
| `timecode_wire.py` | 有界分片拼接、CRC、格式校验和重新同步；不包含控制帧或编码器 |
| `timecode_model.py` | boot/receiver/epoch/mapping 隔离，30ms 失时、历史锚点与通知时间标签 |
| `timecode_worker.py` | 独立串口进程、重连退避、状态发布、只读 RPC；日志写入与串口循环分离 |
| `timecode_ipc.py` | Unix SOCK_SEQPACKET，只有 status/tag；录像进程按原通知单调时间请求标签 |
| `timecode_journal.py` | SQLite 映射边界、检查点和丢日志事件；异步且容量有界 |
| `timecode_status.py` / `timecode_http.py` | 当前质量、实际源年龄及只读历史 HTTP 接口 |
| `recorder.py` / `catalog.py` | 持久化片段首尾标签、u64 精度索引、epoch 范围查询和旧库迁移 |

## 串口绑定与只读边界

仅接受 `/dev/serial/by-id/…` 或 `/dev/serial/by-path/…`，并核验最终字符设备为 ttyUSB/ttyACM。by-id 必须有真实、非全零、未重复的 USB 序列号；否则使用固定 by-path。拒绝与声呐共享同一设备/USB 物理设备，也拒绝相机的复合串口接口，不能把原五台相机的 ttyACM 接口当成时码。

baud/data_bits/parity/stop_bits 必须全部显式配置后才尝试打开。二进制协议要求 8 数据位；校验位和停止位以发生器资料或实测为准，工程不会自动补成 8N1。测试夹具里的 500000/8N1 只用于 PTY 测试，不构成实际设备参数。

打开使用 O_RDONLY、O_NOCTTY、O_NONBLOCK、O_NOFOLLOW；打开前后复核绑定与设备身份，再用 flock/TIOCEXCL 排他。关闭回显、规范行处理、软件和硬件流控，保留原始字节；显式读取核对速率和帧参数。仅清除输入缓存，没有轮询、ACK、写调用、主动 RTS/DE 或 break 控制。生产入口拒绝 PTY，不提供绕过真实 USB 身份检查的选项。

PTY 验证了 0～255 全字节无改写、无回显/软件流控发送，以及写只读 FD 返回 EBADF。`software_tx_bytes=0` 表达本软件路径的零发送约束，**不是 RS485 电气零发送测量**。USB 驱动开关设备的 DTR/RTS 行为、自动方向控制、发生器接线和示波器/逻辑分析仪零 TX 验证仍须实物完成。2026-10-23 对板端仅核验 Python 3.8.10 的 B500000/TIOCEXCL、EpollSelector、SOCK_SEQPACKET 能力，未打开任何串口，见 [能力证据](evidence/rk3588-timecode-capability-20261023.json)。

## 时间语义与失时

协议中的 time_us 是发生器 SOF 时刻。程序知道的是 USB 读取时间，两者尚未校准，因此所有可用关联均为 **ESTIMATED**，`origin_basis=usb_receive_uncalibrated`，`origin_monotonic_us=null`、`uncertainty_us=null`，不生成 VALID 或虚构微秒误差。

`source_valid=true` 仅表示映射连续且最后合格帧未满 30000µs；它不表示曝光同步或已校准时间。满 30ms 关闭映射，最多保留 5 秒失时外推并明确 `source_valid=false`、reason=source_lost_holdover；之后 UNAVAILABLE。恢复、串口重连、epoch 改变均建立新的 mapping_id。进程重启生成新的 receiver_instance_id；系统重启按 boot_id 隔离。不调整 Linux 时间，也不修改视频 PTS/DTS。

状态文件约 0.5 秒更新，只用于服务存活诊断。当前时码通过本机 RPC 取样，API 响应时再检查实际源年龄，不能让新心跳延长 30ms 时码有效期。页面/SSE 刷新频率仍约 1Hz，不是微秒实时显示。

解码每次读取最多 1024 字节，残留最多 25 字节；不完整帧超过 30ms 丢弃。接收后的处理积压达到 30ms 不建立有效映射。USB 合包可能给多帧相同的读取时间，程序不会伪造各自的 SOF。曾关闭的映射不被迟到帧向前扩张。

历史只保留最近 60 秒且最多 6500 个锚点。关联选取事件发生之前最近的合格样本，不能使用后来收到的帧；晚于当前时间、超过历史范围或 boot 不匹配均不可用。已退休 epoch 不接受返回，单进程最多记忆 4096 个 epoch，达到上限拒绝新的 epoch 并保留错误原因。所有 u64 JSON 值均为十进制字符串。

## 录像标签与查询

片段创建通知一到达就按通知携带的原始单调时间获取并缓存 start 标签；完成通知同样获取 end。保存到 JSON 侧录及 SQLite，后续时码变更或恢复不会重写既有标签。服务不可用、缺失创建通知或历史过期时保存显式 UNAVAILABLE 原因。旧版本片段及没有完成通知的崩溃残留仍保留 null，不用当前时码补填。

标签包含 boot/receiver/mapping/epoch、原通知时间、所选源帧 seq/time/接收时间、源年龄、估计时间、有效边界和未校准来源。**这些是片段通知时刻的关联，不是文件每一帧或摄像机曝光的外部时码。** UTC 范围仍来自录像文件名及媒体时长。

只有首尾均为新鲜估计、同 boot/receiver/mapping/epoch，且时间正向时，索引才提供 `continuous_mapping_estimate` 区间。换 epoch、重连或中间失时产生不同 mapping，只保存 `endpoint_estimate` / `holdover_endpoint`，不跨缺口插值。声呐未来可用 `sonar_receive` 调用同一标签接口，当前没有虚构声呐样本。

```text
GET /api/v1/timecode
GET /api/v1/timecode/history?limit=50&cursor=<event_id>
GET /api/v1/recordings?channel=front&epoch=<u64>&time_start_us=<u64>&time_end_us=<u64>
```

history 最多返回 200 条并使用 cursor 分页，未知/损坏历史库为 503，非法查询为 422。保留的是有界审计历史，`complete_history=false`，不是所有 100Hz 原始帧的永久档案。录像外部时码范围必须带 epoch；起点包含、终点不包含，筛选通知估计区间或端点。SQLite 使用 20 位补零 TEXT 比较 u64，不转成有符号 64 位数；网页输入不经 JavaScript Number。原有 UTC、通道、分页查询仍可使用。

## 有界日志与故障隔离

串口/RPC 循环不等待日志文件。日志线程队列最多 128 条，每批最多 32 条，SQLite 保留最多 8192 个事件，写连接限制 64 MiB；只读查询不获得写连接。SQLite 初始化先在临时库提交，再发布文件，初始化失败不留下伪有效空库。首次不可用按 1～30 秒退避重试，写入错误最多每秒重试，坏 schema 不自动覆盖。

队列满或写入失败增加 dropped_events，恢复后提交 JOURNAL_GAP；写入超过 1 秒报告 journal_write_stalled。API 报告日志异常/缺口，录像片段已有的自包含标签仍可读取。磁盘故障、库达到容量或损坏时不能宣称历史完整。状态快照也由独立线程写入。RPC 最多 8 个客户、100ms 超时，单请求/响应分别最多 4096/16384 字节；客户端失败只导致该次关联不可用，不阻止视频进程运行。

## 运行与服务模板

开发机可使用未配置设备的示例启动真实服务流程，返回 UNAVAILABLE：

```sh
# 选择尚不存在的临时目录；两个命令分别在两个终端运行。
mkdir /tmp/ss07-timecode-demo
./tools/ss07 timecode-worker --config config/board-rk3588.example.json \
  --state-dir /tmp/ss07-timecode-demo/state \
  --timecode-journal /tmp/ss07-timecode-demo/journal.sqlite
./tools/ss07 serve --config config/board-rk3588.example.json \
  --state-dir /tmp/ss07-timecode-demo/state \
  --timecode-journal /tmp/ss07-timecode-demo/journal.sqlite
```

生产模板 `ss07-timecode.service` 使用独立静态用户，仅有 ttyUSB/ttyACM 读设备权限、dialout/ss07-state 组、AF_UNIX，以及 `/run/ss07`、`/var/lib/ss07/timecode` 写路径。API 只读历史库并通过 socket 查询。tmpfiles 准备相应索引目录；stage 不启用服务。启动有独占锁，第二个接收器拒绝启动；SIGTERM 清理 socket 并发布 STOPPED。模板和开发机运行通过仍须目标 systemd、USB 驱动及实际权限验证。

## 录像索引升级

配置 schema 和片段侧录 schema 仍为 1，录像数据库升级为 2，新增 time_index。新程序可只读旧库；写入旧库要求显式迁移，旧程序不兼容 schema 2。升级命令同时取得 storage worker 锁及 catalog 写锁，先创建不存在的备份并 integrity_check/fsync，再在一个事务中建立索引和更新 schema。任何事务失败回滚，备份不能覆盖。

```sh
# 供升级维护时使用；先停止实际录像服务，保留所有录像和侧录。
./tools/ss07 migrate-catalog --catalog /var/lib/ss07/catalog.sqlite \
  --backup /var/lib/ss07/catalog-before-0.5.sqlite --state-dir /run/ss07
```

本次只迁移临时测试库，未改设备上的数据。测试覆盖失败回滚、重复备份拒绝、旧片段和锁定版本保留。回滚不能只换 current 链接：0.4 不理解非空新时码侧录，也不能打开 schema 2。自动跨版本回滚尚未实现，升级后的库、侧录和备份均需保留；不可用恢复备份的方式悄悄丢掉升级期间的记录。

## 验证与待接入项

```sh
make validate test
python3 tools/test-recording.py --camera-test build/native/ss07-camera-test \
  --mediamtx /path/to/verified/mediamtx --output /tmp/ss07-timecode-recording-test \
  --timecode-pty --chromium /path/to/chromium
```

集成工具用六路明确标记的编码测试视频、PTY 和独立测试目录；生产状态 API 仍报告实物不可用。验证时码丢失期间六路持续录像、换 epoch 后历史标签不变、浏览器无损传递大于 2^63 的查询值、回放/seek/Range/导出及录像进程强退后的中断片段保护。证据见 [软件联测](evidence/timecode-recording-integration.json) 和 [完整交付进度](delivery-progress.md)。

未接入：真实 USB-RS485/发生器、物理零 TX/延迟校准、声呐厂家驱动与样本侧录、真实 1TB 数据盘及六路满载 8 小时测试。尚未完成 LAN 权限维护、ARM64 应用构建和完整 SDK IMG。提交、推送、烧写和实际设备部署继续等待明确指令。
