# 原生播放器访问

0.9.0 在现有六路媒体服务外增加可选的 **RTSPS / TCP** 入口，复用本机配置的 monitor、reader、maintainer 账户。默认关闭；没有默认账号、密码或证书。这里的网络服务属于 RK 视频项目，不承载运动命令。

## 数据与权限边界

```text
五路 USB H.264 / SE2017
           ↓
内部 MediaMTX ──→ 独立录像进程 / 浏览器 WebRTC
           ↓ RTSP，仅 127.0.0.1
外部 MediaMTX relay ──→ RTSPS / TCP 原生播放器
           ↕ 连接鉴权、账户变更撤权
Python 媒体监督服务
```

外部 relay 使用同一份锁定的 MediaMTX **v1.21.2** 二进制，按需拉取内部流，不新增解码或编码。视频字节一直由原生程序处理。Python 只检查账号、管理子进程、持久化有界审计和发布状态。额外 TLS、网络复制及 relay 内存消耗需要在真实 RK 六路负载下测量，不能把开发机测试视为板端性能结论。

内部 RTSP、媒体管理 API、WHEP HTTP 地址仍为 loopback。外部 relay 的管理 API、WebRTC、HLS、RTMP、SRT、录像与发布功能均关闭；只接受固定的 front、back、left、right、down、sdi 六个读取路径，每路最多四个外部读者。明文 RTSP、匿名、query token 和发布请求均不能取得视频。

鉴权回调绑定随机 loopback 端口，地址含随机能力值，仅写在私有临时配置中。回调有请求长度、连接数量和超时限制。密码用现有 scrypt 参数校验，全局同时只运行一次昂贵校验；同一凭据同时打开多个通道时共享这一轮检查。未知用户共享失败计数；每用户每分钟最多五次新密码检查，全局最多三十次。15 秒握手缓存仅保存带随机密钥的摘要，每次命中仍读取并核对账户版本，最多 64 项。账号/密码不出现在 URL、进程参数或审计中。

账户文件被禁用、改密码、改角色或替换，都会先阻止新连接，再结束整个外部 relay，覆盖已经 PLAY 及尚在 SETUP 的连接。保守策略会断开所有外部播放器；客户端需重新连接。账户文件丢失或校验/审计失败时也关闭外部访问。监督循环每 250 ms 检查一次，原生退出有超时与强退兜底；这不是硬实时期限承诺。内部媒体、录像、WebRTC 不因这次撤权重启。API 重启也不终止原生播放器。

## 显式配置

根管理员配置 `/etc/ss07-gateway/rtsp.json`，模板在应用的 `config/rtsp.example.json`，schema 为 `config/rtsp.schema.json`：

```json
{"schema_version":1,"enabled":false,"port":8322}
```

缺失文件等价于关闭。启用时把 `enabled` 改为 true，端口需为 1024～65535 且与 HTTPS、内部 API、媒体和 RTP 端口无冲突。文件要求 root 管理，建议 0644；`network.json` 中的接口地址、证书与私钥被复用，URL 主机名取其 `public_origin`。应使用客户端信任、SAN 包含该主机名的证书，不关闭客户端证书校验。

例如配置的 HTTPS 主机为 `rk.example`，RTSPS 端口为 8322，则前方通道为 `rtsps://rk.example:8322/front`，其余五路替换最后的方向名。在播放器的凭据栏输入账号和密码，选择 TCP 传输。仅支持明文 RTSP 的播放器不适用于这个入口；不要把密码拼入共享链接或命令行。

首次启用走现有 `ss07-release.py check-ready` 和显式 `activate` 预检。已经运行的安装修改此 root 配置后，需要在维护窗口重新检查并重启 `ss07-media.service` 和 `ss07-application.service`，使媒体和端口冲突检查使用同一配置；媒体重启本身会短暂中断视频。普通账户的密码/角色/启用状态修改则自动撤权，无需重启这些内部服务。网页配置不开放网络与 TLS provisioning 的修改。

0.9.0 的 `ss07-media` 使用静态服务账号，属于 `ss07-web` 组，并获得 `ss07-state`、`ss07-gateway` 附加组。这样可只读账户和 TLS 私钥，审计写到 `/var/lib/ss07-media/access.sqlite`，API 只有组读权限。生产账户、证书和开启配置均不放进应用包。

安装器保留可选配置和 schema 1 的新增审计库；审计纳入一致备份/兼容性检查，但程序回滚不回灌旧审计数据。回滚至 0.8.0 时旧程序忽略此可选配置，外部 RTSPS 不再启动，内部服务仍使用原有接口。不得修改旧包或旧镜像来冒充新版本。

## 状态与审计

`GET /api/v1/status` 增加 `native_rtsp`：DISABLED 表示未启用，READY 表示新鲜的监督状态且原生监听存在，UNAVAILABLE 表示未能确认。只有对应流同时收到新鲜 H.264 时，`streams[].native_url` 才提供无凭据的 RTSPS 地址；监听存在本身不代表摄像头在线。网页流卡片显示这个地址，离线时清除。

维护角色可读取/导出 `GET /api/v1/logs?source=rtsp`。审计包含 RTSP_ACCESS_START、RTSP_LOGIN_OK、RTSP_LOGIN_FAILED、RTSP_REVOKE，仅保留最近 4096 项，最大数据库容量沿用 16 MiB 限制；握手重试不会重复写成功事件。无用户名的正常认证挑战和被限流请求不会逐条落盘，不应把这个接口视为完整网络包日志。

## 可复现验证

普通管理测试只需要 Python 标准库：

```sh
make test
python3 tools/test-arm64-management.py --sysroot /path/to/locked-arm64-rootfs --output /new/arm64-test-output
```

媒体联测单独运行 `tools/test-rtsp-access.py`，需要 `--camera-test`、`--mediamtx`、`--chromium`、`--output`。相机输入是明确标识的 synthetic_test 原生编码源，存储使用只存在于测试工具的目录适配器。输出目录必须不存在。测试检查六路并发 TLS/RTP 接收和 H.264 解码、三个角色、拒绝未授权与发布、四读者配额、账户撤权、缺失凭据恢复、API 重启、WebRTC/录像共存及审计。

额外播放器兼容性检查在独立进程中运行 `tools/test-rtsp-client.py`，使用开发机 PyGObject/GStreamer/GIO 和 SRTP 插件，凭据从 stdin 传入。它使用客户端专属 CA 数据库并保留全部证书检查，原生接收样本再独立解码。测试可用 `--client-runtime` 指定仅包含 SRTP 插件与 libsrtp2 的独立目录，避免修改开发机已安装包；这些测试依赖没有加入产品管理服务。

保留的失败尝试也见输出日志：首次拒绝登录客户端等待不足、撤权后误读旧心跳，以及测试客户端原生断言、缺少解析/SRTP 插件及采样窗口未包含足够 IDR 后视频。修正属于测试等待、隔离和依赖选择；生产没有取消鉴权或 TLS 校验来绕过测试。

证据汇总见 [原生 RTSP 验证](evidence/native-rtsp-validation.json)。真实 USB 相机、SE2017、LAN 证书/接口、静态服务 UID/文件权限、板端性能与八小时运行尚需授权后的设备验收。
