# Mobocon 网络节点 ss07-mobocom-node（0.10.9）

RK3588 以 Mobocom V2.2（Control 0x22）网络节点 `VIDEO_SONAR 0x9009` 出现在 Mobocon 设备列表。Mobocon 用组播 DISCOVER 找到板子的 UID 和当前 IP，再用这个 IP 按 [mobocon 拉流与配置接口](mobocon-video-api.md) 取六路视频和状态。协议契约以协议库文档为准：[video-sonar-node.md](../lib/mobocom_protocol/docs/ss07/video-sonar-node.md)。

节点只负责身份、在线发现、版本核对和安全待机。它不接收运动命令，不转发云台或跟焦命令，也不碰视频、录像、声呐和时码。STOP、ESTOP 或 FAULT 只改变协议层状态，画面照常可取。

## 1 组成

| 项目 | 内容 |
| --- | --- |
| 程序 | `/opt/ss07/releases/<版本>/bin/ss07-mobocom-node`，Rust，静态链接（aarch64 musl），源码在 `node/` |
| 协议库 | 随工程 vendor 的 `lib/mobocom_protocol`（SDK `2.2.0-network.1`），未修改；来源提交与摘要见 `mobocom-sdk.lock.json` |
| 服务 | `ss07-mobocom-node.service`，装在 `/etc/systemd/system`，与 `ss07-media`、`ss07-api`、`ss07.target` 没有任何 `Requires=` 关系 |
| 运行用户 | systemd 动态用户 `ss07-mobocom-node`，无视频、USB、串口设备权限，无 capability |
| 身份文件 | `/var/lib/ss07/mobocom-node.json`（0644，原子替换），只由启动前的 root 步骤写入 |
| 可选配置 | `/etc/ss07/mobocom-node.json`：`{"schema_version":1,"interface":"eth0"}`；不存在时使用默认路由所在网卡 |
| 运行状态 | `/run/ss07-mobocom-node/status.json`，每 5 秒刷新：UID、boot、网卡、IP、状态、收发计数 |
| 端口 | UDP 47821，组播 239.255.33.21，TTL=1 |

Profile 按契约第 4 节填写：类型 0x9009，profile_version 1，source_mask 0x04，一个占位资源（endpoint 1、resource 1、unit 0x9009，其余为 0），没有动作。Driver 拒绝一切目标，安全确认和故障复位恒为允许。firmware 由版本号换算：`主<<24 | 次<<16 | 修订`，0.10.9 即 `0x000a0009`。

## 2 UID 与 boot

首次启动时，UID 由 `/etc/machine-id` 经固定算法派生并写入身份文件；以后只读这个文件，machine-id 变化也不会改 UID。文件损坏时服务拒绝启动，不会悄悄换一个新 UID；文件丢失才重新生成，并在日志里告警，因为 UID 变化会让 Mobocon 把它当成新设备。0.10.8 写下的身份文件格式相同，升级后 UID 保持不变。

UID 在 JSON、日志和 API 中一律写成十进制字符串，同时给出 `0x` 十六进制形式。boot 每次服务启动取一个新的随机非零 u64。

现场贴标用：

```sh
sudo /opt/ss07/current/tools/ss07 node-info        # UID（十进制与十六进制）、固件、网卡、boot、计数
/opt/ss07/current/tools/ss07 node-info --json      # 运行状态 JSON
```

## 3 安装、启用与回滚

安装发布包（`tools/ss07-release.py install`）只把程序和 unit 样本放进 `/opt/ss07/releases/<版本>/`，不会启用节点。启用和停用都是单独的一步：

```sh
sudo /opt/ss07/current/tools/ss07 node-service install     # 写入 unit、enable 并（重新）启动；有 avahi 时同时发布 mDNS
sudo /opt/ss07/current/tools/ss07 node-service status
sudo /opt/ss07/current/tools/ss07 node-service uninstall   # 回滚：disable --now，删除 unit 与 mDNS 记录
```

unit 带有 `ConditionPathExists=/opt/ss07/current/bin/ss07-mobocom-node`：如果把 `current` 切回没有节点程序的旧版本，服务会被跳过，不会反复重启。

## 4 运行行为

- 绑定 `0.0.0.0:47821`，在控制网卡上加入 239.255.33.21（协议库 `UdpTransport::device`）。每 5 秒检查一次网卡和地址，DHCP 换址或网卡变化后重建 socket。
- 主循环约 1 ms 一轮，每轮最多处理 64 个数据报，避免组播洪泛饿死定时处理；每 10 ms 调一次 `tick`。时间用进程内单调时钟（微秒）。
- 每个数据报原样交给协议核心。有回复时立即单播回本次请求的源 IP 和端口；发送失败只计数，不重试，不阻塞。CRC 错、多帧拼接、尾随字节、目标 UID 不符、超长帧、组播的非 DISCOVER/STOP/ESTOP 都由核心丢弃，服务不退出。
- 日志只写启动信息、网卡变化和每 60 秒一次的计数（计数没变就不写），不写报文内容。

## 5 mDNS（可选）

板上已有 avahi 时，`node-service install` 写入 `/etc/avahi/services/ss07-mobocom.service`，发布 `_mobocom._udp`（端口 47821，实例名 `SS07-<UID 低 32 位十六进制>`，TXT 含十进制 UID 和类型）。它只用于显示名，发现不依赖 mDNS。

## 6 自检与 Mobocon 侧验证

节点程序自带只读发现工具，发一次组播 DISCOVER 并列出所有回复（含往返时间）：

```sh
/opt/ss07/current/bin/ss07-mobocom-node discover                       # 用默认路由网卡
/opt/ss07/current/bin/ss07-mobocom-node discover --interface-ip 192.168.3.233 --timeout-ms 500
```

另一台同网段电脑可以用协议库示例（在 `lib/mobocom_protocol/mobocom_realtime/rust` 下）：

```sh
cargo run --example discover -- <本机控制网卡IPv4> <任意非零u64会话号>
```

`tools/test-mobocom-node.py` 用协议库的 Python 编解码，从任意主机对节点做 DISCOVER、INFO、CAPS、STATUS、组播 STOP/ESTOP、单播 RESET_FAULT 和异常帧检查，只发协议允许的安全命令。

## 7 故障排查

| 现象 | 检查 |
| --- | --- |
| 板上 `discover` 收不到自己 | `systemctl status ss07-mobocom-node`；`ss -ulpn \| grep 47821`；`ip maddr show dev eth0` 里应有 239.255.33.21 |
| 同网段电脑收不到回复 | 防火墙放行入站 UDP 47821（含组播）；交换机开了 IGMP snooping 但没有查询器时组播会被丢，需开启 IGMP 查询器或关闭 snooping；确认电脑和板子在同一二层网段（TTL=1，不跨路由） |
| 多网卡时回复走错口 | 在 `/etc/ss07/mobocom-node.json` 指定 `interface`；`rp_filter` 为 1（严格）时检查反向路由，2（宽松）一般没问题 |
| UID 变了 | 查看身份文件是否被删除；日志里会有 `WARNING created new UID` |
| 换 IP 后 Mobocon 找不到 | 节点 5 秒内会重新加入组播；Mobocon 需要重新 DISCOVER，并按新 IP 重建 WHEP 会话 |
| 节点显示“已急停” | 协议行为：组播 ESTOP 后为 FAULT/fault=7，需 Mobocon 单播 RESET_FAULT；画面不受影响 |

## 8 构建

```sh
cd node
cargo test --offline --locked                     # 含 5 条黄金向量逐字节比对、急停/复位、异常帧
cargo build --release --locked --target aarch64-unknown-linux-musl
```

静态 musl 构建不依赖板上 glibc（板子为 2.31，SDK 交叉工具链的 glibc 更新，动态链接的程序在板上无法运行）。`packaging/package-arm64.py --node-binary <路径>` 把程序、unit 样本和 SDK 锁文件放进发布包，并在 `SS07-MANIFEST.json` 的 `mobocom_node` 中记录程序摘要、协议库提交与 SDK 摘要。`node/Cargo.toml` 的版本必须与应用版本一致，否则打包失败。

## 9 已知限制

- 协议库快照记录的来源提交 `ac6e6dde56001c8f6b9f27530d0cd47065191eac` 目前不在 codeup 远端，只能凭 vendor 目录和 `content_sha256` 追溯，需要协议库维护者推送。
- 负载验收（六路满载下 DISCOVER 成功率与延迟）、Windows 主机发现和 DHCP 实际换址需要现场同网段电脑完成。

## 10 板上验收记录（2026-11-06，192.168.3.233）

发布包 `ss07-0.10.9-linux-arm64.tar.gz`（SHA-256 `a24f2155…a498`），节点程序 SHA-256 `60a4d4bd…032a`，SDK `2.2.0-network.1`，`content_sha256` `56575ddc…5c2e`。安装前备份在板上 `/var/backups/ss07-pre-0.10.9-20261106110046`（`current` 指向、`/etc/ss07*`、unit 列表、身份文件）；安装前后 `system.json`、`network.json`、`rtsp.json`、`lan-monitor.json` 哈希不变。

| 项 | 结果 |
| --- | --- |
| 黄金向量 | `cargo test` 5/5 逐字节一致；急停/复位、写目标拒绝、10 类异常帧无回复均通过 |
| 节点身份 | UID `8455818509601193611`（`0x75591a2235f3be8b`，沿用 0.10.8 身份文件），firmware `0x000a0009`，eth0 192.168.3.233，已加入 239.255.33.21，动态用户运行 |
| 板上组播 DISCOVER | 26B 回复，0.2–0.9 ms；同网段另有 0x9006 设备（192.168.3.78）应答，说明组播路径正常 |
| 协议检查 `test-mobocom-node.py` | 板上与开发机均 11/11 通过：INFO/CAPS/STATUS、组播 STOP 无回复仍 SAFE、组播 ESTOP 后 FAULT/7、单播 RESET_FAULT 回 SAFE 且 generation+1、异常帧无回复、服务不退出；期间 `ss07-media` 未重启 |
| 服务隔离 | 重启节点：boot 变化，`ss07-media` 未重启；重启 `ss07-media`：节点 PID 与 boot 不变；六路随后恢复 |
| 免密钥（来源 192.168.3.20，白名单 192.168.3.0/24） | status/streams/sonars/timecode 200；config、logs、recordings、timecode/history、PUT config、POST plan、api-clients 403；六路 WHEP 201 并解码（5×1280×720，sdi 1920×1080），DELETE 200；同源第 13 个会话 503；`rtsp_url` 不含凭据；RTSP 8555 六路无密钥可读；RTSPS 8322 无密钥 DESCRIBE 返回 200 和 SDP（front/back/sdi） |
| 非白名单 | 白名单收窄为单个其他地址后：无密钥 API、WHEP 401，伪造 `X-SS07-Peer` 仍 401，无密钥 RTSP 被拒，Bearer 200；板上 127.0.0.1 回环来源无密钥被拒 |
| 删除配置 | 无密钥 401、RTSP 被拒、Bearer 200；恢复文件后 6 秒内恢复 200 |
| Bearer 回归 | streams/config 200，错误密钥 401，Bearer+Cookie 401 |
| 节点回滚演练 | `node-service uninstall` 后 47821 无监听、无 DISCOVER 回复、媒体未重启；`install` 后同 UID 恢复 |

未验证：Windows 或另一台现场电脑的组播发现（开发机经 NAT，无法收发现场组播）、DHCP 实际换址、六路满载下的 DISCOVER 成功率统计、`current` 切回 0.10.8 的整版回滚（本机没有 0.10.8 安装包）、RTSPS 的完整拉流（开发机 GStreamer 缺少 TLS 插件，只验证到 DESCRIBE 鉴权）。
