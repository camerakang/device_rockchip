# HTTPS、权限与维护（0.7.0）

## 2026-11-04：网页直接进入

按用户要求，工作区标准 API unit 已加入 `--direct-access`。打开网页时，`GET /api/v1/session` 自动签发浏览器独立的 Cookie 和 CSRF token，直接显示监看、录像与配置界面，不显示登录/退出操作。所有可访问该 HTTPS 入口的用户都具有 `maintainer` 权限，审计身份统一为 `local-operator`；这不代表已验证个人身份。会话仍有过期时间和 32 个上限，已有 Cookie 的刷新复用原会话，过期后页面自动获取新会话，不自动重放失败的写请求。

直接进入模式不读取账号文件；必须配置准确的 HTTPS origin 和访问审计路径，维护操作仍通过独立维护进程执行。原账号模式仍可通过去掉 `--direct-access` 使用；下文三档角色、登录和账号撤权描述适用于该模式。原生 RTSPS 的独立账号鉴权不受此开关影响。

本变更已作为 0.10.3 部署到 192.168.3.233，事务 `b5a0cc96-5205-4dbc-8d5f-61e636c579b1` 已 COMMITTED；保留旧版本与现场配置。开发检查使用 `make test`（Python 3 与带全局 `Headers` 的 Node.js）；新增测试覆盖免密码进入、配置操作权限、跨浏览器会话隔离、CSRF/Origin、过期续建和前端入口，不替代真实浏览器 WebRTC 验收。

2026-10-23。提供可运行的 HTTPS 登录、三档权限、独立维护进程、配置持久化、录像锁定与结构化日志导出。0.7.0 增加 [配置应用与恢复](configuration-application.md)。Python 负责有界状态与管理请求；视频采集、编解码、WebRTC、录像及 HTTPS 加密由原生程序处理。软件测试使用六路原生编码测试源和明确标注的临时数据目录，不能代替 RK3588、LAN、真实数据盘或实物 8 小时验收。

## 入口与角色

| 角色 | 状态、监看、时码状态 | 录像查询/回放/导出、时码历史 | 配置、录像锁定、维护日志 |
| --- | --- | --- | --- |
| monitor | 允许 | 拒绝 | 拒绝 |
| reader | 允许 | 允许 | 拒绝 |
| maintainer | 允许 | 允许 | 允许 |

固定静态页面、`GET /healthz`、`GET /api/v1/session` 可以在登录前访问；其余业务数据和视频授权需要会话。没有账号文件或网关配置时，生产 unit 启动失败，不降级为匿名服务。`make run` 仍提供只绑定 127.0.0.1 的只读开发方式，配置和锁定写入返回 405，页面标注本机只读开发。

浏览器仅访问 Caddy HTTPS 入口。API、MediaMTX 管理接口、WHEP HTTP 和 RTSP 监听回环地址。只有明确配置的 WebRTC ICE UDP 地址对局域网提供媒体传输，浏览器先经过登录授权取得 SDP，媒体使用 WebRTC 自身的 DTLS/SRTP。当前不开放局域网匿名 RTSP；外部原生 RTSP 客户端的授权方案仍待实现。

网关配置独立保存在 `/etc/ss07-gateway/network.json`，网页维护不能修改。字段示例见 `config/gateway.example.json`：HTTPS origin 必须精确到主机和端口，端口 1024～65535，`listen_ip` 与 `ice_ip` 为已确认的本机 IPv4 地址，不接受通配地址；证书必须包含 origin 主机对应的 SAN。示例 127.0.0.1 仅用于本机，实际 LAN 需按板端地址和证书重新配置。用户提供过 RK `.233` 与 SE2017 候选 `.47`，不能将编码器地址作为 RK 监听地址。

网关固定 Caddy v2.11.7，关闭管理接口、自动 HTTPS/ACME、配置自动保存和 HTTP/3。HTTPS 使用已有证书，最低 TLS 1.2；请求体 64 KiB、头部 16 KiB、读头 3 秒、读体 5 秒。未配置证书时不创建临时生产证书，也不开放 HTTP 登录。反向代理保留 Host，API 再做精确校验。WebRTC 创建/删除和维护写入验证 Origin 与 CSRF；不接受请求指定代理目标或本地文件路径。

## 账户和会话

账户文件 `/etc/ss07-credentials/accounts.json` 与可写设备配置目录分离。使用本机管理命令初始化及更新，没有默认账号/密码，没有网页创建账号接口：

```sh
# 以下是后续部署/本机演练命令；本轮未在设备上执行。
# 生产目录由 sysusers/tmpfiles 准备，配置凭据的操作者使用 root。
./tools/ss07 init-accounts --accounts /etc/ss07-credentials/accounts.json --username admin
./tools/ss07 list-accounts --accounts /etc/ss07-credentials/accounts.json
./tools/ss07 set-account --accounts /etc/ss07-credentials/accounts.json \
  --username viewer --role monitor --enabled true --expected-revision '<list-accounts 的 revision>' --new-password
```

密码通过终端无回显输入两次；受控非交互环境可显式使用 `--password-stdin` 输入一行，不提供密码命令行参数。长度 12～128 字符，无控制字符。账号最多 16 个，至少保留一个启用的 maintainer。更新使用 revision CAS；文件必须是 root 或当前服务用户拥有的普通文件，禁止其他用户权限和组写入，生产为 root:ss07-web 0640，目录 root:ss07-web 2750；更新保留文件组。

口令采用 scrypt N=32768/r=8/p=3、随机 16 字节盐、32 字节结果，单次最大内存 64 MiB，最多一个并发登录校验。该选择对应 OWASP 的一档 scrypt 参数。板端只读能力测试确认 Python 3.8.10/OpenSSL 1.1.1f 支持这些参数；一次校验测得约 0.45 秒，此数字仅说明一次口令计算，不是多路视频性能指标。

每用户每分钟 5 次、全局每分钟 30 次登录尝试，未知用户名共享有界计数。会话随机 256 bit，服务器内存只以摘要索引，最多 32 个、每账号 4 个。Cookie 为 `__Host-SS07; Secure; HttpOnly; SameSite=Strict; Path=/`，不写 localStorage；CSRF token 单独保存在页面内存。空闲 30 分钟、绝对 12 小时到期；持续状态订阅刷新空闲时间，绝对期限始终生效。

账户文件 revision 改变或文件不可读时撤销已有会话。退出后立即移除服务器会话，即使退出日志写入失败也不继续授权。日志失败时不能颁发新会话。口令计算和登录日志不持有会话锁，避免影响已登录监看者。

WHEP 会话与登录会话绑定，最多每会话 12 路、全 API 48 路；退出、到期和账户撤销后由后台关闭对应原生媒体会话。通过请求标记关联 WHEP 私有资源 ID 与原生会话，清除已结束/媒体重启遗留的额度。API 启动前清理孤立 WebRTC 会话；systemd 的 ExecStopPost 也执行原生清理。停止钩子使用 `/run/ss07-api/media.json` 保存的已加载管理端口，不读取尚未应用的新端口。清理仅针对监看会话，保留源与录像。SSE 和下载过程中复核会话；下载每次 sendfile 上限 1 MiB，最多 4 个并发内容请求。

## 接口与独立维护进程

| 接口 | 权限与结果 |
| --- | --- |
| `GET /api/v1/session` | 登录需求、当前角色、权限及 CSRF token，不返回 Cookie token |
| `POST /api/v1/login` | JSON username/password，验证 Origin；成功设置会话 Cookie |
| `POST /api/v1/logout` | JSON 空对象，验证会话/Origin/CSRF |
| `GET /api/v1/config` | maintainer；候选/运行 revision、待应用标志、API 已加载值、逐服务确认及应用事务状态 |
| `PUT /api/v1/config` | maintainer；candidate/expected_revision，成功 persisted=true、hardware_applied=false、restart_required=true |
| `POST /api/v1/config/plan`、`apply`、`recover` | maintainer；预览影响、明确应用、重试恢复；apply/recover 的 202 只表示已受理 |
| `POST /api/v1/recordings/{id}/lock` | maintainer；locked 布尔值、expected_revision 十进制字符串；数据盘 gate 和 lock_revision CAS |
| `GET /api/v1/logs` | maintainer；source=access/maintenance/application/recording/timecode，limit=1～100，cursor 可选，download=1 导出本页 JSON |

未登录 401，权限/Origin/CSRF 拒绝 403，revision 冲突 409，参数错误 422，服务未就绪或结果不确定 503。配置读取失败时保持当前运行副本。配置写入保留 `.previous`，使用文件锁、fsync、原子替换。**保存后尚不自动应用**：0.7.0 由维护者另行预览和应用，通过协调器执行选择性重启、逐服务确认及失败恢复。API 的 loaded_revision 仍仅说明 API 自己已加载的配置，其他 worker 的生效结果读取各自新鲜快照。

`ss07-maintenance` 独立进程处理 `config.update` 与 `recording.lock`，并把 plan/apply/recover 交给独立 root 协调器，通过本机 SOCK_SEQPACKET 收发有界 JSON。服务校验 SO_PEERCRED 中的 API UID，客户端也校验维护 UID；最多两个并发连接、每连接 3 秒期限。维护服务本身没有 shell、任意文件、运动控制或 systemctl 操作。锁定使用启动时运行副本对应的数据盘，不能把待应用配置作为正在使用的数据盘身份。

权限划分：API 为 ss07-api，能读状态、录像和账号，写自己的访问日志、私有运行目录及共享心跳；维护进程为 ss07-storage，可写候选配置和录像侧录；网关为 ss07-gateway，可读 TLS 私钥。root 配置协调器写运行副本并调用固定 unit 白名单。API 无摄像头/串口权限，维护不访问设备，时码服务仍单独只读。

维护前先记录 OPERATION_STARTED，成功后记录 CONFIG_UPDATE 或 RECORD_LOCK。日志只接收白名单字段，不保存请求体、密码、Cookie 或 RTSP URL。两类审计库分别最多 4096 条/16 MiB，首次建库完成后才原子发布。写后日志失败或 RPC 应答丢失返回 `MAINTENANCE_RESULT_UNCERTAIN`：页面提示重新读取配置/录像状态并核对日志，不能把超时直接解释为未执行，也不自动重试修改。

## 构建、暂存和本机验证

```sh
python3 packaging/fetch-media.py --component caddy --arch arm64 --output /tmp/ss07-caddy-arm64
# 可加 --archive /path/caddy_2.11.7_linux_arm64.tar.gz 离线校验；必须与锁文件摘要相符。
python3 packaging/stage-rootfs.py --destdir /tmp/ss07-0.7-stage \
  --native-dir /path/arm64-native --media-dir /path/verified-arm64-mediamtx \
  --gateway-dir /tmp/ss07-caddy-arm64
```

stage 校验架构一致、归档摘要、二进制摘要和版本；拒绝已有目标目录。它不启用服务、不生成账户/证书、不创建数据盘目录。仅准备 `/opt/ss07/releases/0.7.0`、current 相对链接、候选及运行配置、systemd 模板和 target。ARM64 原生依赖与 ELF ABI 检查见独立构建说明，实物运行仍需设备验收。

生产准备顺序为：确认配置/接口地址和存储身份 → 准备 sysusers/tmpfiles 与文件权限 → 提供证书和账户 → 校验配置 → 启动维护、API、媒体及 HTTPS 服务。`network.json` 在此过程中由本机维护者按实际地址创建；不把默认 loopback 示例自动安装为 LAN 配置。当前用户明确要求提交、推送、烧写和设备部署等待后续指令，本轮只验证暂存树。

复现软件验证：

```sh
make test
python3 tools/test-auth-integration.py --camera-test /path/ss07-camera-test \
  --mediamtx /path/verified/mediamtx --caddy /path/verified/caddy \
  --chromium /path/chrome-headless-shell --output /new/auth-test-directory
```

联测需要已验证的原生测试源构建、GStreamer 测试插件、Node 支持 WebSocket 的版本、OpenSSL 及 Chromium。仅测试脚本可以替换数据盘 gate；生产没有允许写系统盘的开关。测试创建一次性证书、随机账户和 Chromium 临时配置，不安装系统信任。输入始终为 synthetic_test，业务状态继续报告硬件未就绪。验证摘要及残余门槛见 `evidence/auth-validation.json`、`evidence/auth-integration.json` 和交付进度。

实现依据：Python 官方 http.server 部署限制、OWASP Password Storage/Session Management/CSRF 指南、Caddy 官方 Caddyfile/JSON 文档及 MediaMTX v1.21.2 OpenAPI；实际依赖锁定见 `packaging/dependencies.lock.json`。
