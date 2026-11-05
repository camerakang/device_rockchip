# 录像、索引与回放 0.5.0

0.6.0 新增 HTTP 锁定权限、HTTPS 录像读取及会话撤销，见 [权限与维护](auth-maintenance.md)；0.6.1 的 ARM64 与完整 IMG 进度见 [交付跟踪](delivery-progress.md)。下文保留 0.5.0 录像/时码路径及对应验证记录。

2026-10-23。本阶段提供可运行的软件路径；数据盘按用户确认的名义 1 TB 规划，目前未连接。开发机测试使用明确标记的测试视频和独立测试目录，不能替代 RK3588 与实际数据盘验收。

## 模块与数据路径

`五路原生 H.264 + 一路 SE2017 RTSP → 监看 MediaMTX → 本机 RTSP/TCP → 独立录像 MediaMTX → 数据盘 fMP4`。

监看实例继续使用 `record=false`。录像实例只拉取压缩流，不监听 RTSP/WebRTC/管理端口；Python 不接收或解码视频帧。停止录像、缺盘或索引故障不会主动停止监看实例和相机进程。

| 模块 | 职责 |
| --- | --- |
| `volume.py` | UUID、块设备、精确挂载身份与非系统分区核验，持有录像目录 FD，安全的相对目录/文件操作 |
| `recorder.py` | 独立录像进程、六路拉流、片段通知、空间检查、异常退避、状态快照 |
| `tools/ss07-segment-hook.py` | 有界 Unix datagram 通知；通知丢失写固定故障标记，禁止静默继续 |
| `tools/ss07-record-child.py` | 原生进程 exec 前设置父进程死亡信号，避免 supervisor 意外退出后继续录像 |
| `mp4.py` | fMP4 顶层结构和边界检查；不宣称检查了每个压缩帧 |
| `catalog.py` | SQLite 索引、侧录元数据、锁定 CAS、恢复、已完成片段清理 |
| `recording_http.py` | 以 recording_id 查询和读取；单范围 Range、HEAD、导出 |
| `web/recordings.js` | 通道/本地时间查询、分页、状态、回放、导出；测试记录明确标注 |

MediaMTX 固定 v1.21.2，继续使用 `packaging/dependencies.lock.json` 的摘要。Python 3.8+ 标准库包含 SQLite 接口，索引要求 SQLite >=3.24，无新增 pip/npm 运行依赖。2026-10-23 12:54 对目标板仅作只读能力检查：Python 3.8.10、SQLite 3.31.1，内存数据库 UPSERT 验证通过；未部署应用，见 [板端 SQLite 证据](evidence/rk3588-sqlite-20261023.json)。

## 配置与运行

配置 schema 仍为 v1。旧配置缺失下列可选项时采用安全默认值：

```json
{
  "storage": {
    "recording_enabled": false,
    "filesystem_uuid": null,
    "segment_seconds": 300,
    "reserve_bytes": "50000000000",
    "retention_days": null,
    "mountpoint": "/data/ss07",
    "recordings_dir": "/data/ss07/recordings"
  }
}
```

这是 storage 片段，不是完整 system.json。当前默认不启用录像、不填虚构 UUID。300 秒是候选最小分段时长，实际边界取决于关键帧及流中断；fMP4 part 为 1 秒、单 part 上限 16 MiB。保留 50 GB 是开发默认值，非用户冻结的保留策略；最小允许 256 MiB。retention_days=null 不按年龄清理，但启用录像后仍按空间水位清理未锁定的完成片段。MediaMTX 内置自动删除关闭，清理由索引模块统一执行。

独立状态/索引页面可以直接在开发机运行，无盘时不会录像：

```sh
# 选择新的临时工作目录；命令不创建 /data/ss07。
mkdir /tmp/ss07-record-demo
./tools/ss07 init-catalog --catalog /tmp/ss07-record-demo/catalog.sqlite
./tools/ss07 serve --catalog /tmp/ss07-record-demo/catalog.sqlite
```

以下命令供数据盘身份、权限与配置准备完成，且获得实际部署指令后使用；本阶段未在设备执行：

```sh
./tools/ss07 recording-worker --config /etc/ss07/system.json \
  --state-dir /run/ss07 --catalog /var/lib/ss07/catalog.sqlite \
  --binary /opt/ss07/current/bin/mediamtx
./tools/ss07 serve --config /etc/ss07/system.json \
  --state-dir /run/ss07 --catalog /var/lib/ss07/catalog.sqlite
```

`systemd/ss07-storage.service` 使用固定 ss07-storage 用户和 ss07-recordings 组。tmpfiles 只准备状态和索引目录；不创建数据盘录像目录。已核验的 `/data/ss07` 分区根目录需由 ss07-storage:ss07-recordings 持有并提供 setgid 目录权限，例如 2750。API 加入读取组，systemd 中数据盘及索引为只读；不授予原始块设备读写。服务需要 stat `/dev/disk/by-uuid` 核验身份，因此 API 不使用隐藏这些链接的 PrivateDevices。

挂载权限判断区分 API 自身 observer_writable 与录像 worker 的实际写入许可。新鲜 worker 快照、启用配置和同 UUID 的当前只读挂载核验同时满足时，API 才报告 recording_enabled。recording_active 另需正在写入的文件字节增长；3 秒不增长即失效，不能用进程存活或打开文件冒充录像有效。

## 数据盘保护与异常

启用时检查指定 UUID 对应真实块设备、精确挂载点、非系统分区、挂载根为分区根、可写属性和目录身份；系统盘、bind 子目录及符号链接不能绕过。仅核验成功后才创建 recordings。会话目录、文件、侧录和清理操作使用目录 FD 的相对寻址，拒绝路径穿越、符号链接、硬链接、FIFO 与嵌套挂载。

录像进程继承固定目录 FD，原生写入路径为 `/proc/self/fd/<fd>/<session>/<channel>/...`。即使原挂载从路径移除或被同名目录替换，已打开 FD 仍指向原文件系统，不会重新解析到系统盘。supervisor 周期检查当前命名空间的挂载身份；失效停止录像，保留告警和中断文件。systemd 文件系统隔离可能保留服务自己看到的旧挂载；这不构成 host 命名空间卸载的即时检测承诺，实际热拔盘/重挂载仍须实机验证。

每秒检查空间并尝试清理至 reserve + 128 MiB。空间仍低于 reserve 时停止录像，最长 30 秒退避，期间持续发布 BLOCKED 状态。只清理此 UUID、已完成、未锁定且文件与侧录身份完全一致的片段。每批最多 64 个；不安全条目进入需恢复的索引状态，不能长期阻挡其他可清理条目。未完成和锁定片段永不自动删除；锁定占满空间时停止录像。

录像完成通知包含 session、channel、内部路径、duration 和通知发生时的单调微秒；单条 2048 字节。只接受本次会话、合法路径和时间。MediaMTX 的浮点秒文本可能包含超过九位小数，按有界 Decimal 解析并四舍五入至毫秒；拒绝负数、非有限值和范围外时长，不因浮点尾数误停录像。接收完成通知后检查 fMP4 结构、fsync 文件，先持久化 JSON 侧录，再提交 SQLite。通知丢失、容器截断、数据库写入失败不能被报告为成功完成。

SQLite 使用 DELETE journal、synchronous=FULL；写连接各自设置 1 GiB 文件上限，索引记录最多 200000 条，中断事件和删除墓碑各保留最多 10000 条。写事务由进程间锁串行化，读取接口使用只读连接。未知数据库 schema 拒绝启动，不自动覆盖或降级。

## 索引、锁定与恢复

片段元数据包含 recording_id、通道、UTC 范围、时长、大小、inode/mtime、会话与 boot_id、完整性、锁定版本、证据类型和通知单调时间。recording_id 由卷身份与内部相对路径确定；API 不返回内部路径。UTC 起点来自录像文件名，结束由媒体时长计算；`timestamp_basis=segment_notification_not_exposure` 明确通知时间不是曝光时刻。

0.5.0 为新片段保存通知发生时的 `time_tags.start/end`：USB 接收未校准，因此只能是 ESTIMATED 或 UNAVAILABLE，不代表曝光时刻。旧片段和没有完成通知的崩溃残留仍保留 null，不从当前时码回填。新增 epoch/外部时码查询；同一连续映射才提供通知区间估计，跨失时或 epoch 仅保留端点。完整语义、字段和数据库 schema 1→2 的备份迁移见 [只读时码说明](timecode-runtime.md)。

锁定只提供本地命令，expected-revision 来自查询结果；冲突退出码 3，其他输入/IO 错误为 2：

```sh
./tools/ss07 lock-recording --config /etc/ss07/system.json \
  --catalog /var/lib/ss07/catalog.sqlite --recording-id '<id>' \
  --locked true --expected-revision '<lock_revision>'
# 解锁使用 --locked false 和最新 revision。
```

锁定先写侧录后写索引；后一步失败时，清理因二者不一致而拒绝删除。恢复会采用持久侧录，保留锁定。恢复与运行中的录像 worker 通过同一 storage 锁互斥：

```sh
./tools/ss07 recover-recordings --config /etc/ss07/system.json \
  --catalog /var/lib/ss07/catalog.sqlite --state-dir /run/ss07
```

缺失的数据库可从当前卷的有效侧录重建；已存在但损坏或 schema 不支持的数据库需先保留现场，再另建索引恢复，不自动删除原库。没有完成侧录的文件一律 INCOMPLETE 且默认锁定，即使能解析部分 MP4 也不能推断已完成。完成文件消失标 MISSING，身份/内容结构变化标 INVALID。恢复不将另一块离线盘的记录标为丢失。

## HTTP 与网页

| 请求 | 行为 |
| --- | --- |
| GET /api/v1/recordings | channel、start/end（含时区）、epoch/time_start_us/time_end_us、limit 1～200、cursor；返回 items、next_cursor、最近中断 events 和 disk_online |
| GET /api/v1/recordings/{id}/content | 只读取完成片段；支持单个 bytes Range 和 If-Range，200/206；多范围或越界为 416 |
| HEAD /api/v1/recordings/{id}/content | 相同文件核验与响应头，不发送正文 |
| GET .../content?download=1 | 同一内容，附安全的固定下载文件名 |

未知 ID 为 404；无可用索引为 503；数据盘/片段不满足读取条件为 409 或 503。未挂盘仍可查询已存在索引，但文件 availability 明确为 disk_offline，网页不提供可点击的回放/导出。HTTP 锁定和维护写入仍为 405，等待角色鉴权实现。所有服务继续仅监听回环地址，未开放生产 LAN 入口。

网页提供通道、UTC/外部时码过滤和分页；本地时间输入转换为带时区的 UTC 查询，外部范围必须同时填 epoch，u64 字段始终按字符串传递。回放通过浏览器原生 video 和 Range，不在 Python 中转码。记录已完成不等于任意客户端都支持其编码，真实摄像头 profile/GOP 仍需兼容性验证。

## 可重现验证与交付限制

```sh
make validate test
unshare --user --map-root-user --mount python3 tools/test-volume-unmount.py
python3 tools/test-recording.py \
  --camera-test build/native/ss07-camera-test \
  --mediamtx /path/to/verified/mediamtx --output /tmp/ss07-recording-test \
  --chromium /path/to/chromium
```

原生测试程序的编译和开发机插件环境见 [原生视频说明](native-video.md)。集成测试选择 640×360/25 fps 和 2 秒片段，启动六个真实编码进程、两个监看媒体实例及独立录像进程。测试目录只能由专用测试脚本注入；生产 CLI 没有使用系统目录替代数据盘的选项。

覆盖六路分段、单路断源/恢复、录像停止不影响监看、强制终止后的未完成文件隔离、真实浏览器解码/seek、Range、完整导出一致性，以及锁定 CAS/侧录先写后数据库失败/丢库重建/容量上限/清理边界。卸载测试仅在新 user/mount namespace 的 tmpfs 上执行，验证挂载移除后的 FD 写入不落到系统目录。证据入口见 [交付进度](delivery-progress.md)。

尚未验证 1 TB 实盘、USB 热拔盘、掉电持久性、ARM64 本机依赖及长期吞吐；真实时码、声呐采集/关联、HTTP 维护权限、日志导出、完整 SDK IMG 与六路 8 小时整机验收仍未完成。服务模板静态通过也不等于已在目标板运行。当前不提交、推送、烧写或部署到实际设备。
