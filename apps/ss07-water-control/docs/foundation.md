# SS-07 基础工程 0.2.0

本文保留 0.2.0 阶段记录。后续实现及当前状态见 [原生视频](native-video.md)、[录像与回放](recording.md)、[只读时码](timecode-runtime.md) 和 [完整交付进度](delivery-progress.md)；下文的“未实现”以该历史阶段为准。

2026-10-23。在原 0.1.0 工程上补齐可独立运行和后续接入的基础代码。运行只依赖 Python 3.8+ 标准库；开发机已分别用 CPython 3.8.20 与 3.10.12 验证。目标板现有 3.8.10 尚未运行此版本，本轮不部署、不安装板端依赖。

## 工程职责

| 文件/目录 | 当前实现 |
| --- | --- |
| `src/ss07/config.py` | 配置 schema 子集校验、重复键/长度/未知字段/控制字符拒绝、稳定设备路径与地址冲突校验 |
| `src/ss07/persistence.py` | 本地配置初始化、跨进程文件锁、expected_revision 比较、临时文件+fsync+原子替换、保留上一版 |
| `src/ss07/probe.py` | 真实只读设备/媒体探测；查询命令超时和输出有界，超限时清理整个子进程组 |
| `src/ss07/devices.py` | 从探测证据提取离散视频模式，识别重复序列号、区分相机附属串口，检查请求模式是否在枚举表中 |
| `src/ss07/snapshots.py` | 本机 worker 快照契约、原子发布、大小与身份检查、单调时间年龄、过期/旧帧/错误快照拒绝 |
| `src/ss07/state.py` | 独立聚合状态、相机帧年龄、内存/温度观测、持久配置与已加载配置区别 |
| `src/ss07/storage.py` | 精确挂载点、系统同文件系统、只读挂载、符号链接路径和可写性检查；不创建录像目录 |
| `src/ss07/api.py` | 回环 HTTP、只读状态/配置、健康接口、SSE、固定静态资源路由 |
| `web/` | 无前端构建依赖的中文状态页，六路视频状态、五路测距空值、时码和存储状态，断线后显示过期 |
| `systemd/` | API unit、共享状态组 sysusers 与状态目录 tmpfiles 模板；未启用 |
| `packaging/` | 测试后生成应用包和摘要；在新目录生成 rootfs 暂存树 |
| `tests/` | 配置、并发保存/失败、快照、实际探测证据、HTTP/SSE、存储、CLI/暂存树与正常退出测试 |

硬件 producer 尚未实现；不创建模拟硬件服务。媒体采集/发布、声呐协议、时码串口与映射、SQLite 录像索引、录像内容 API 和完整系统镜像继续按需求分阶段实现。基础状态页尚无播放能力，不能视作六路监看完成。

## 运行与配置

项目根目录运行 `make validate`、`make test`、`make run`。默认回环端口 8077，浏览器打开 `http://127.0.0.1:8077/`。`SS07_PYTHON=/path/to/python ./tools/ss07 ...` 或 `make PYTHON=/path/to/python ...` 可指定解释器。

本地配置流程：

```sh
# /tmp 仅作开发示例；首次创建不会覆盖已有文件
./tools/ss07 init-config --output /tmp/ss07-system.json
./tools/ss07 serve --config /tmp/ss07-system.json
# 另一终端准备候选配置，然后先做结构校验
cp /tmp/ss07-system.json /tmp/ss07-candidate.json
./tools/ss07 validate --config /tmp/ss07-candidate.json
# 将下列值换为 GET /api/v1/config 或 init-config 返回的 revision
./tools/ss07 update-config --config /tmp/ss07-system.json \
  --candidate /tmp/ss07-candidate.json --expected-revision '<revision>'
```

退出码 0=成功，2=输入/IO 失败，3=版本冲突。保存前持有配置锁，冲突不覆盖，原值保存在同目录 `.previous` 文件。新文件模式默认 0600，更新保留已有文件读写权限；用于 systemd API 的无密码配置必须保持该服务可读（暂存树默认为 0644）。同目录必须可信，维护者之外不能写入。写入失败保留可读的旧文件；实际存储硬件掉电持久性仍需现场测试。

保存配置只改变持久值，当前进程不自动重载，更不应用硬件。API 返回 persisted/revision、loaded_revision、runtime、pending_restart 和 apply_result；用户显式重启后加载新值。无效磁盘配置不会替换正在使用的内存配置。需要恢复时，将 `.previous` 复制成候选并按当前 revision 通过 update-config 保存。HTTP 维护写入仍返回 405，鉴权/跨服务应用/回滚属于后续功能。

配置 schema 保持 v1，已存在的 0.1 配置可读取。默认不绑定相机方向、不猜视频模式、SE2017 URL、声呐协议或时码参数。schema 使用的关键字必须由内置校验器显式支持；不支持的关键字报错。

## 设备证据与视频接入前检查

```sh
./tools/ss07 probe > /tmp/ss07-probe.json
./tools/ss07 inventory --probe /tmp/ss07-probe.json
./tools/ss07 check-cameras --config /tmp/ss07-system.json --probe /tmp/ss07-probe.json
```

读取原始探测上限 8 MiB；其他 JSON 配置/快照上限 64 KiB。模式解析只支持当前实际设备使用的 Discrete 格式/尺寸/帧率，连续或 stepwise 范围不自行外推。V4L2 文本 fps 有三位小数舍入，检查容差 0.001fps；打开设备后仍须核验实际有理数帧间隔。

输出 ENUMERATED 仅表示请求出现在保存的枚举证据中，始终 hardware_verified=false；报告带原观测时间，不把历史探测当成当前连接状态。真正打开设备前必须重查身份/能力。状态包括 UNCONFIGURED、ABSENT_OR_AMBIGUOUS、AMBIGUOUS_IDENTITY、CAPABILITIES_UNAVAILABLE、UNSUPPORTED_MODE。

现有板端证据中五台摄像头序列号都是 00000000，因此推荐 by-path。它们的 ttyACM 复合接口标成 camera_interface，其他串口一律 unassigned，不自动认定为声呐或时码口。模板配置仍等待现场方向对应表。

## 本机状态快照契约 v1

采用每服务一个原子 JSON 文件作为基础 IPC，便于独立重启、检查和最小依赖接入，替代早期文档里尚未实现的 Unix socket 候选。仅传有界状态；录像事件/时码映射日志不通过会覆盖的最新快照传输，后续另建持久化路径。

API 启动参数 `--state-dir /run/ss07`。允许文件名固定为 camera.front/back/left/right/down.json、media.json、sonar.json、timecode.json、storage.json。写者调用 `ss07.snapshots.publish`；每服务只能有一个实际 producer。文件 0640，/run/ss07 使用 setgid 目录和共享 ss07-state 组；API 仅读取，部署时将真实 producer 赋予相应权限。

| 字段 | 约束 |
| --- | --- |
| schema_version | 整数 1 |
| service | 与固定文件名匹配 |
| instance_id | 每次 worker 启动新的非空身份，不能沿用旧进程值 |
| boot_id | 与 API 当前 Linux boot_id 相同 |
| seq | 本服务实例递增 u64 十进制字符串 |
| observed_monotonic_us | 该次实际观测的本机单调微秒，u64 字符串 |
| state | STARTING / RUNNING / UNAVAILABLE / ERROR / STOPPED |
| evidence_kind | device_observation；模拟标记拒绝 |
| payload | 服务字段对象，整个文件不超过 64 KiB |

读取拒绝符号链接、非普通文件、损坏 JSON、启动标识不符、未来/倒退时间、倒退序号、相同序号但内容改变的快照。同内容重复读取不刷新年龄。新 instance 必须有更新的观测时刻；3 秒无更新即 STALE，过期时隐藏 payload。系统重启后的旧快照因 boot_id 不同不可使用。文件目录由受信任本机服务维护；evidence_kind 是契约约束，不是硬件真实性的密码学证明。

相机 payload 当前支持 capture_state、publish_state、format、width、height、fps_observed、bitrate_observed、last_frame_monotonic_us、restart_count、error。STREAMING 必须提供真实最后帧时间；即便 worker 心跳持续，3 秒没有新帧也显示 STALE。API 不根据进程存活推断视频正常，发布状态也由实际 producer 报告。3 秒目前是基础故障门槛，后续随真实帧率/超时预算配置化。

其他服务文件当前仅提供生命周期诊断；声呐样本、时码映射、SE2017 媒体状态和录像遥测适配尚未实现，因此对应业务值继续不可用，不以 RUNNING 生命周期冒充有效数据。未指定 state-dir 时，worker 保持 NOT_IMPLEMENTED。

## HTTP 与页面

原 GET /api/v1/status、streams、sonars、timecode、config 保留；新增 GET /healthz、GET /api/v1/events 与根路径状态页。接口仍采用 data/error + request_id。/healthz 仅证明 API 可运行，hardware_ready=false；不是整机验收标志。

SSE 事件名 status，连接后立即发送完整状态，随后约 1Hz 更新；ID 由服务实例与 seq 组成。最多 4 个 SSE 客户端、16 个 HTTP 请求线程，写超时 3 秒；慢客户端断开后重新取完整快照，不积累历史队列。暂不提供独立即时告警通道，告警包含在每次快照。HTTP 修改请求 405，未实现录像接口 404。静态文件只有固定白名单路由，不将请求路径拼接为本地文件路径。

页面显示状态与测距，不放入模拟视频/距离。EventSource 重连或超过 4 秒无更新显示过期并清除测距值；所有服务数据用 textContent 渲染。界面目前为基础诊断页面，生产网络入口、角色鉴权、视频播放器及 TypeScript 工程留待后续。服务构造和配置均限制 127.0.0.1，不开放维护写端口。

## 存储与启动

storage 模块只检查 `/data/ss07` 的精确挂载项；拒绝同系统根分区的 bind 挂载、只读文件系统及符号链接目录。返回 gate、mount_present、writable、free_bytes；即使 gate=ELIGIBLE，recording_enabled 仍为 false，因为录像 writer 未实现。

这些是接入前检查，不是持续持有数据盘的写入许可。实际录像还需核验设备 UUID/容量/文件系统，并在打开和写入中应对拔盘、重挂载、权限变化；不能仅依据一次 statvfs 结果启用录像。

systemd 模板使用 DynamicUser、PrivateDevices、只读系统目录和共享状态组；API 无设备访问职责。sysusers 创建 ss07-state 组，tmpfiles 准备 /run/ss07；暂存树不启用服务。SIGTERM 能结束 SSE 和 API，服务停止不触碰任何采集设备。

## 构建与验证

```sh
make test
make package OUTPUT=/tmp/ss07-0.2-package
make stage DESTDIR=/tmp/ss07-0.2-rootfs
```

两个输出目录必须不存在。应用包包含版本 manifest 与逐文件 SHA256；构建入口使用执行该脚本的同一 Python 进行校验和测试。stage 仅生成 `/opt/ss07/releases/0.2.0`、current 相对链接、配置及 systemd/sysusers/tmpfiles 模板；不写主机 /etc，不创建录像目录，不安装/启动服务，不生成 IMG。

当前验证包括 28 个测试在 x86_64 CPython 3.8.20 / 3.10.12 下通过。范围包括并发配置冲突、写入失败保留原值、损坏配置、快照过期/启动隔离/重放、真实保存探测数据解析、HTTP/SSE、存储 gate、暂存应用独立启动及 SIGTERM 正常退出。浏览器检查、包校验和日志随本次 `output/ss07/` 交付产物提供。Python 3.8.10、ARM64 与实物采集未验证，不能由这些软件测试替代。
