# 配置应用与故障恢复（0.7.0）

配置现在分为编辑内容、已保存候选、运行配置和逐服务观测结果。维护者先保存，再检查影响并应用；保存不会立即改变正在录像的进程。应用失败会恢复先前运行配置，候选修改继续保留，便于修改后重试。

## 文件和服务边界

| 位置/服务 | 职责 |
| --- | --- |
| `/etc/ss07/system.json` | 已保存候选；维护服务按 revision CAS 原子保存 |
| `/etc/ss07-active/system.json` | 各消费者启动时读取的运行副本；root 写入，其他服务只读 |
| `/var/lib/ss07-apply/` | 上一次运行配置、候选副本、事务记录和有界审计库；事务文件仅协调器读取 |
| `ss07-maintenance.service` | 校验 API 对端身份、保存候选、按已加载的数据盘配置锁定录像、转交应用请求 |
| `ss07-application.service` | 独立 root 协调器，只接受 plan/apply/recover 三种配置操作；固定服务名单和参数调用 systemctl |
| `ss07-config-recover.service` | 启动前恢复未完成事务的旧配置；消费者均 Requires/After 此 oneshot |

API 不调用 systemctl，也不获取 root 权限。API→维护→协调器两段 SOCK_SEQPACKET 都校验 SO_PEERCRED；请求/响应有大小、连接数和时间上限，同一个操作 ID 贯穿两个 RPC 和审计记录。协调器不接收命令、unit 名称、文件路径或运动指令。HTTPS/ICE、账号和证书仍由本机维护者单独准备，不能通过设备配置编辑器修改。

运行文件通过同目录临时文件、fsync、原子替换更新。事务写入先于停止服务；损坏记录、备份摘要不符或运行文件被外部修改时拒绝继续应用，避免覆盖无法解释的状态。只有一个应用任务可以执行；任务开始后再次保存的新候选不会改写正在执行的副本，而是继续显示为待应用。

## 影响范围与确认条件

| 修改 | 受影响的进程 |
| --- | --- |
| 设备名称 | API；媒体和录像保持运行 |
| API 本机监听端口 | API、HTTPS 网关；媒体和录像保持运行 |
| 某路相机绑定或模式 | 该路相机、API |
| RTP 输入端口范围 | 五路相机、媒体、API |
| 媒体 RTSP 端口 | 媒体、录像、API |
| 录像配置 | 录像、维护、API |
| 时码配置 | 时码、API |

完整结果由两个配置的逐服务依赖摘要计算，网页预览实际重启名单。API 重启会撤销登录和监看会话，用户需重新登录；这不停止独立录像。媒体或采集配置变化可能产生对应视频的录像缺口，跨服务切换不承诺无缝或原子生效。

每个服务快照携带完整配置摘要及其依赖部分的摘要。协调器要求当前 boot、启动后的观测、匹配摘要、同一实例持续至少 2 秒并有不同心跳序号，再确认生效；默认健康等待上限为 25 秒。旧快照、失效进程和只有文件写入成功均不能使应用通过。

已配置相机需采集与发布正常；已配置 SDI 需媒体服务观测到可用 H.264；录像启用需通过真实数据盘 gate；已配置时码需串口接收服务运行。时码串口打开不代表已收到有效时码或完成物理零 TX 验收。未配置相机/时码可以确认“已加载（设备未配置）”，录像关闭可以确认“已加载（功能未启用）”；这些结果始终不表示硬件就绪。

当前只实现原生 H.264，其他相机格式的应用会被拒绝。声呐配置保存仍可用于整理参数，但在厂家驱动缺失时拒绝将声呐修改标为已应用，不生成报文或测距值。

## HTTP 与网页流程

三个新增 POST 接口都要求 maintainer、有效会话、精确 Origin 与 CSRF。配置读取同时返回 `revision`、`active_revision`、`pending_apply`、API 自己的 `loaded_revision/runtime`、逐服务 `apply_result` 和协调器 `application`。

| 接口 | JSON 请求 | 成功含义 |
| --- | --- | --- |
| `POST /api/v1/config/plan` | `expected_revision`、`expected_active_revision` | 200，返回实际服务名单和录像/会话影响；不停止服务 |
| `POST /api/v1/config/apply` | 同上 | 202，仅表示任务受理；完成状态需随后读取 |
| `POST /api/v1/config/recover` | `expected_operation_id` | 202，重试未完成的恢复；仍需读取结果 |

网页保留未保存编辑和本地 revision；后台状态刷新不会悄悄接受其他维护者的修改。保存后才能预览，预览与候选、运行 revision 均一致才允许应用。应用后轮询状态，遇到 API 重启则重新登录并读取事务结果。网络超时只表示结果未确认，不能推断没有执行，也不自动重复提交。

主要事务状态：`PREPARED → STOPPING → SWITCHING → STARTING → VERIFYING → APPLIED`。失败进入 `ROLLING_BACK`，先恢复旧运行文件，再检查受影响服务；全部确认才标为 `ROLLED_BACK`。`configuration_restored=true` 只证明文件已恢复，必须同时看终态及逐服务结果。`ROLLBACK_FAILED` 保留备份和故障状态，允许明确重试恢复。掉电、SIGTERM 或协调器中断留下的事务先由启动 oneshot 恢复旧文件，再由协调器确认服务；主机关机过程中不会为了回滚重新启动业务服务。

`GET /api/v1/logs?source=application` 提供应用审计，支持现有分页和导出。协调器记录 `OPERATION_STARTED`、`CONFIG_APPLY`、`CONFIG_ROLLBACK`，不记录候选请求体或密码。库最多保留 4096 条事件/16 MiB；它不是无限期审计档案。

## 初始化与构建

暂存工具同时生成默认候选和运行文件、9 个 service 模板和 `ss07.target`，不会启用服务。完整镜像配方继续要求 `/etc/ss07-provisioned`，新协调器和恢复 oneshot 也有此条件。实际设备安装/升级仍需明确部署指令。

已有运行环境接入双配置文件前，必须停止消费者并核对当时实际运行值，不能直接把未应用的候选当作旧运行值。确认后的首次副本可使用下列本机命令初始化；目标必须不存在，不覆盖现有文件：

```sh
./tools/ss07 init-active-config --config /path/to/confirmed-system.json \
  --output /etc/ss07-active/system.json
```

生产由 sysusers/tmpfiles 设置目录和锁文件权限；运行副本 root:root 0644、目录 0755，事务目录 root:ss07-state 2750。账号、TLS、网关、串口和录像盘仍须按实物准备。配置恢复不是应用版本回滚，也不代替 SQLite schema 迁移备份或完整安装/升级流程。

## 验证与限制

复现本机联测：

```sh
make test
python3 tools/test-configuration-application.py \
  --camera-test /path/ss07-camera-test --mediamtx /path/verified/mediamtx \
  --caddy /path/verified/caddy --chromium /path/chrome-headless-shell \
  --output /new/configuration-test
python3 tools/test-arm64-management.py --sysroot /path/locked-arm64-sysroot \
  --output /new/arm64-management-test
python3 tools/verify-service-staging.py --stage /path/arm64-application-stage \
  --system-rootfs /path/complete-arm64-rootfs --output /new/systemd-check
```

联测使用六路 C++/GStreamer H.264 测试源、实际 MediaMTX/Caddy/Python 进程、Chromium HTTPS 页面与 WebRTC 解码；只有测试启动器替换数据盘来源并显式标记 synthetic_test，生产没有绕过挂载保护的选项。验证设备名称修改的选择性重启、API 端口被占用后的自动恢复、重新登录、候选保留、审计关联和六路录像进程/文件持续性。

故障测试覆盖配置 CAS、并发任务、后续候选编辑、过期确认、原子写失败、损坏备份/事务、恢复失败重试、中断后启动前恢复、外部运行文件漂移、受限 RPC。ARM64 QEMU 验证 Python 3.8.10 兼容性；systemd 245 静态解析使用原始生产路径，但没有启动 unit。真实 systemd 身份/权限隔离、新内核启动、USB/SE2017、声呐、500000 baud 实物串口、1 TB 数据盘和 8 小时验收仍待现场验证。

证据见 [配置联测](evidence/configuration-integration.json) 和 [验证汇总](evidence/configuration-validation.json)。已交付的 0.6.1 IMG 保留原摘要；新增 0.7.0 功能不能视为已包含在该旧镜像中。
