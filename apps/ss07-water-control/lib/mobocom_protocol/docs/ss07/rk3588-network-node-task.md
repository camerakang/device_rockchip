# RK3588 实施任务书：接入 Mobocom V2.2 网络节点

面向在 RK3588（LubanCat-4）上修改并部署的 AI/工程师。先读 [video-sonar-node.md](video-sonar-node.md)，那是契约；本文是工作包、文件落点、验证命令和部署步骤。

目标：板子以 `VIDEO_SONAR(0x9009)` 出现在 Mobocon 设备列表（组播 DISCOVER 可见），Mobocon 无需 Bearer 密钥即可取六路视频。**不改变现有视频、录像、声呐、时码行为。**

## 0 前置与约束

- RK 工程：`camerakang/device_rockchip`，分支 `SS-07-水上控制系统`，目录 `apps/ss07-water-control`。已有服务 `ss07-media`（MediaMTX）、`ss07-api`（HTTPS 8443 网关）等；已有文档 `docs/mobocon-video-api.md`、`docs/mobocon-development.md`。
- 协议库：`device_protocol`，分支 `feat/network-only-v22`（含本任务的 `0x9009` 登记与黄金向量；若尚未合并，使用 `feat/ss07-node-type`）。
- 不要修改 vendor 出来的协议文件；用 `python tools/package_sdk.py --install <RK项目>` 安装 SDK，再 `--verify`。
- 板子账号、密码、密钥不得写入仓库、文档、日志或提交。
- 不要触碰云台、跟焦、时码、声呐驱动、录像逻辑；不要把视频进程和 Control 节点放进同一进程。
- 先在开发分支改动并本地测试；部署前备份 `/opt/ss07/current` 指向和 `/etc/ss07*`，保留回滚。

## 1 工作包 A：Control 节点服务 `ss07-mobocom-node`

新增独立服务，**用 Rust 实现**，依赖协议库 crate `mobocom-protocol`（`mobocom_realtime/rust`，路径依赖或随 SDK 快照 vendor，禁止修改其源码）。Python `reference.py` 只是对照模型，不得作为板端实现。可在 PC 交叉编译 `aarch64-unknown-linux-gnu`，或在板上装 rustup 后本机编译；产物为单个 ARM64 可执行文件，放入 `/opt/ss07/releases/<version>/bin/`。独立 systemd unit、专属用户（无视频/USB/串口设备权限，只写 `/var/lib/ss07/mobocom-node.json` 与日志）。

1. **SDK**：`DeviceCore::new(profile, uid, boot, driver)`，`Driver` trait 实现见契约第 4 节（`validate_targets`/`apply_targets` 返回 false，`start_safe`/`safe_confirmed`/`reset_allowed` 返回 true，`flush_remote` 空），其余方法用默认实现。参考 `mobocom_realtime/rust/README.md` 与 `examples/conformance.rs`。
2. **Profile**：`device_type=DeviceType::VideoSonar as u16`（0x9009），`version=1`，`firmware` 取发布版本，`source_mask=4`，单个占位资源 `Resource{endpoint:1,resource:1,unit:0x9009,flags:0,minimum:0,maximum:0,group:0,vmax:0,amax:0}`，无动作，其余数值见契约第 4 节表。
3. **UID/boot**：按契约第 3 节。首次生成后写文件（权限 0644，原子替换）；提供 `tools/ss07 node-info` 打印 UID（十进制与 0x 十六进制）、固件、监听接口，便于现场贴标。
4. **网络适配**：
   - 用 `UdpTransport::device(控制网卡IPv4)` 建 socket（bind 0.0.0.0:47821、加入组播、TTL=1、非阻塞）；控制网卡由 `/etc/ss07/mobocom-node.json` 的 `interface` 指定，缺省取默认路由所在网卡。每 5 s 检查网卡地址，变化则重建 socket。
   - 主循环 1 ms 级轮询：`receive(&mut [0; MAX_FRAME+1])` 取到数据报后 `core.receive(&buf[..n], now)`，为 true 则立即 `core.take_reply(now)` 并 `send(reply.as_slice(), Some(源endpoint))`；每 10 ms 调一次 `core.tick(now)`。`now` 取进程启动后的单调微秒（`Instant`）。
   - 每轮最多处理 64 个数据报，防止组播洪泛饿死 tick；异常数据报由核心丢弃，服务不退出；发送失败不重试、不阻塞。
   - 日志只记录计数、UID、boot、接口集合，不记录原始载荷十六进制之外的敏感信息；限频，避免被组播洪泛刷盘。
5. **systemd**：`ss07-mobocom-node.service`，`Restart=always`、`RestartSec=1`、`After=network-online.target`；`NoNewPrivileges`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/ss07`；**不得**与 `ss07-media`/`ss07-api` 建立 `Requires=` 依赖，也不被它们依赖。
6. **防火墙**：放行入站 UDP 47821（含组播）。检查 `rp_filter` 不丢组播回复，且多网口时回复源地址正确。
7. **可选 mDNS**：若系统已有 avahi，则发布 `_mobocom._udp`（见契约第 6 节）；没有则跳过，不引入新依赖。

### 完成标准

- 单元测试：逐字节复现 [ss07_node_vectors.json](ss07_node_vectors.json) 中全部 5 条 response（UID/boot/firmware 取文件值，now 从 1000000 起每条 +1000）。协议库侧已用 Rust 核心验证 5/5 一致，RK 侧不一致说明 Profile 或 Driver 填错。
- 急停测试：组播 ESTOP 后 STATUS 为 FAULT/fault=7，单播 RESET_FAULT 后回 SAFE/fault=0；全过程视频不受影响。
- 异常测试：CRC 错、多帧拼接、尾随字节、dst 不匹配、超长帧、组播非 DISCOVER/STOP/ESTOP，均无回复且服务不退出。
- 本机回环测试：脚本自发组播 DISCOVER，收到 26B 回复，UID/类型正确。

## 2 工作包 B：免密钥局域网监看（lan-monitor）

在现有 API 网关/外部 RTSP relay 中实现契约第 7 节。先读 `docs/mobocon-development.md` 与网关源码，找到 Bearer 解析和 RTSP 鉴权回调的位置，再改。

1. 读取 `/etc/ss07-gateway/lan-monitor.json`；不存在/无效/`enabled=false`/`allow_cidrs` 为空/CIDR 含 `0.0.0.0/0` 均视为关闭（启动时记录原因，不崩溃）。
2. HTTP：未带 `Authorization` 的请求，若源 IP 在白名单，则授予固定的受限 `monitor` 角色。该角色的路由白名单为：`GET /api/v1/status|streams|sonars|timecode|events`、`POST /media/<path>/whep`、`DELETE` 自己创建的 WHEP 会话。其余路由（录像、config、logs、api-clients）即使白名单源也返回 401/403。**必须以服务端路由白名单实现，不能靠前端隐藏。**
3. WHEP 会话归属：无密钥会话以来源 IP 为属主，只能删除自己创建的；每来源 IP ≤12 个；总数 ≤48（沿用现有计数）。
4. RTSP：外部 relay 的鉴权回调在白名单源且无用户名时放行 `front/back/left/right/down/sdi` 读；写、publish 一律拒绝。RTSPS 同理。
5. 带 Bearer 的请求保持原行为；带 Bearer 与 Cookie 同时出现的现有 401 规则不变。
6. `GET /api/v1/streams` 在 lan-monitor 请求下返回的 `rtsp_url` 不含用户名和密钥。
7. 日志：模式启用状态、被拒来源（限频）、无密钥会话建立/关闭；不记录凭据。
8. 文档：更新 `docs/mobocon-video-api.md`，增加 lan-monitor 配置、权限矩阵和关闭方法；`ss07 api-client` 的现有命令不变。

### 完成标准

- 白名单源无密钥：六路 WHEP 建立成功，RTSP 明文读取成功，`/api/v1/streams` 可读。
- 同一源访问 `/api/v1/config`、`/api/v1/logs`、录像内容、`PUT/POST` 类接口均 401/403。
- 非白名单源无密钥访问任意受保护路由 401；带有效 Bearer 的现有流程不变。
- 删除配置文件后行为恢复为原状（回归现有 Bearer 测试）。
- 增加自动化测试覆盖上述矩阵，并保证现有 API/媒体测试全通过。

## 3 工作包 C：文档与打包

- 把 `ss07-mobocom-node`、lan-monitor 纳入打包脚本/镜像清单（systemd unit、配置样例 `lan-monitor.json.example`、默认关闭）；发布清单记录协议库提交与 SDK 摘要。
- 在 RK 工程 `docs/` 新增或更新“Mobocon 网络节点”说明，指向协议库的 `video-sonar-node.md`；注明 UID 获取方式、端口、故障排查（组播、防火墙、IGMP snooping、多网卡）。
- 不要把现场 IP、账号写进样例；示例 CIDR 用 `192.168.3.0/24` 注释说明。

## 4 板上部署与验证顺序

1. 备份：记录 `readlink /opt/ss07/current`、`/etc/ss07*`、`systemctl list-units 'ss07*'` 状态。
2. 部署新版本到 `/opt/ss07/releases/<version>`，切换 `current`，重启 `ss07-api`、`ss07-media`（短暂中断视频，应在现场允许时进行）。
3. 启用节点服务：`systemctl enable --now ss07-mobocom-node`。
4. 在板上：`ss07 node-info`；`ss -ulpn | grep 47821`；`ip maddr`（确认已加入 239.255.33.21）。
5. 在另一台同网段电脑（Windows/Linux）用脚本发组播 DISCOVER，确认 100 ms 内回复；可用协议库只读示例 `cargo run --example discover -- <本机控制网卡IPv4> <任意非零u64会话号>`（在 `mobocom_realtime/rust` 下）。
6. 创建 `lan-monitor.json`（只含 Mobocon 所在网段），重启 `ss07-api`/relay；用无密钥 curl/ffprobe 验证 WHEP/RTSP；验证 config 接口被拒。
7. 故障演练：重启 `ss07-mobocom-node`（视频不中断）；重启 `ss07-media`（Control 不中断）；DHCP 换址后重新发现。
8. 记录证据：命令、输出、UID、固件、网卡、时间；明确区分“已验证”和“未验证”。

## 5 回滚

停用并禁用 `ss07-mobocom-node`；删除或改名 `/etc/ss07-gateway/lan-monitor.json` 并重启 API/relay；`current` 指回旧版本。回滚后原 Bearer 流程必须仍可用。

## 6 不在本任务内

- 不实现任何运动、使能、WRITE_CONFIG、时间轴或节目下载；不修改时码接收；不新增 Control 之外的 UDP 控制端口。
- 不为 SS-07 扩展公共协议字段（流地址不放进 CAPS/STATUS）。若以后需要由设备自报流描述，应走协议库的类别扩展提案，不在 RK 私改。
- 不使用 `0x20/0x21` 旧协议或串口 Control。

## 7 交付物清单

RK 代码与测试、systemd unit、配置样例、更新后的 `mobocon-video-api.md`、部署与验收记录、协议库提交号与 SDK 摘要。完成后在记录中列出：向量通过、实网发现、免密钥取流、回滚演练是否完成，未完成项写明原因。
