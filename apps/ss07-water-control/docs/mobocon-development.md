# MOBOCON 开发文档（RK3588 端接口总览）

本文是 mobocon 开发者对接 SS-07 RK3588 视频与声呐系统的维护入口。Mobocon 侧的开发要求和测试用例见 [mobocon-test-plan.md](mobocon-test-plan.md)。接口细节见 [mobocon-video-api.md](mobocon-video-api.md)；Control 与时码协议的设计核验记录见 [mobocom-integration.md](mobocom-integration.md) 和 [timecode-integration.md](timecode-integration.md)。接口有变更时，先改对应专题文档，再更新本文第 7 节的变更记录。

## 1 职责边界

| 项目 | 负责 |
| --- | --- |
| RK3588（本工程） | 六路视频（五路 USB、一路 SDI）、五路声呐、监看与录像、状态与配置、独立只读 RS485 时码记录 |
| mobocon | 实时显示六路画面，读取状态，按需修改 RK 配置 |
| 云台接收器、跟焦器、UE | 其他项目。RK 不转发任何运动命令，mobocon 的 Control 总线（0x20）与 RK 无关 |

RK 的时码适配器是独立的只读通道，不经过 mobocon，也不向 mobocon 转发时码。

## 2 连接信息（当前现场）

| 项目 | 值 |
| --- | --- |
| RK 板地址 | 192.168.3.233 |
| API / 网页 | `https://192.168.3.233:8443`，自签名证书 |
| WebRTC 媒体 | UDP 8189，信令经 8443 的 `/media/<通道>/whep` |
| RTSP（明文，局域网） | `rtsp://192.168.3.233:8555/<通道>` |
| RTSPS（TLS） | `rtsps://192.168.3.233:8322/<通道>` |
| 通道名 | `front`、`back`、`left`、`right`、`down`、`sdi` |
| 编码 | 五路 USB 为 H.264 720p25；sdi 取自 SE2017（192.168.3.47） |

网页界面免登录。mobocon 使用 API 客户端密钥；启用 lan-monitor 后，白名单网段也可以不带密钥取视频和状态，见 [mobocon-video-api.md](mobocon-video-api.md) 的“免密钥局域网监看”。mobocon 使用 API 客户端密钥，密钥只在创建时显示一次，由现场维护者保管，不要写入仓库。

## 3 选择拉流方式

| 需求 | 方式 | 说明 |
| --- | --- | --- |
| 在 mobocon 界面里实时显示 | WebRTC（WHEP） | 延迟最低；Electron 请在主进程发 WHEP 请求，API 不返回 CORS 头 |
| 交给 ffmpeg、UE 或其他程序处理、转存 | RTSP/TCP | 兼容性最好，每路最多 4 个外部读者 |
| 网络不可信 | RTSPS | 需要信任网关证书 |

WebRTC 会话每个客户端最多 12 个，与网页监看合计最多 48 个。密钥被禁用、轮换或删除后，其 WebRTC 会话约 1 秒内被关闭，RTSP 播放器会被断开。

## 4 接口索引

所有 HTTP 请求带 `Authorization: Bearer <密钥>`，不要同时带 Cookie。

| 用途 | 接口 | 最低角色 |
| --- | --- | --- |
| 全部状态 | `GET /api/v1/status` | monitor |
| 视频流状态与拉流地址 | `GET /api/v1/streams`（含 `rtsp_url`、`native_url`） | monitor |
| 声呐、时码状态 | `GET /api/v1/sonars`、`/api/v1/timecode` | monitor |
| 状态推送 | `GET /api/v1/events`（SSE） | monitor |
| 本机 WHEP 会话数 | `GET /api/v1/media/sessions` | monitor |
| WebRTC 拉流 | `POST /media/<通道>/whep`，`DELETE <Location>` | monitor |
| 录像、时码历史、声呐历史 | `/api/v1/recordings...`、`/api/v1/timecode/history`、`/api/v1/sonars/history` | reader |
| 读取、保存配置 | `GET` / `PUT /api/v1/config` | maintainer |
| 预览、应用、恢复配置 | `POST /api/v1/config/plan`、`/apply`、`/recover` | maintainer |
| 应用审计 | `GET /api/v1/logs?source=application` | maintainer |

响应统一为 `{"data": ...}`，错误为 `{"error": {"code", "message", "details"}}`。状态码含义见 [mobocon-video-api.md](mobocon-video-api.md)。

## 5 开发指引

**取得密钥。** 在板上由 root 执行 `ss07 api-client add --api-clients /etc/ss07-credentials/api-clients.json --name mobocon --role maintainer --group ss07-web`。只需看画面和状态时用 `monitor` 角色。

**修改配置的标准流程。**

1. `GET /api/v1/config`，记下 `revision` 和 `active_revision`。
2. 修改 `persisted` 后 `PUT`，请求体为 `{"candidate": ..., "expected_revision": ...}`，得到新 revision。
3. `POST /plan` 查看将重启的服务和对录像、会话的影响，向操作员确认。
4. `POST /apply` 只表示受理（202），之后轮询 `GET /api/v1/config`，直到 `application.state` 为 `APPLIED`、`ROLLED_BACK` 或 `ROLLBACK_FAILED`。
5. 409 表示配置已被他人修改，需要重新读取。网络超时不代表没有执行，不要自动重发 `apply`。

**状态字段的解读。** `hardware_ready` 和 `hardware_verified` 为 false 时，表示服务已加载但尚未通过实物验收。声呐没有厂家协议前不会出现测量值，mobocon 不应把空值或模拟状态当作真实设备数据。

**证书。** 板子使用自签名证书。开发时用 `NODE_EXTRA_CA_CERTS` 信任网关证书，不要在产品里关闭证书校验。

## 6 待确认与未接入

| 项目 | 状态 |
| --- | --- |
| 声呐型号、厂家协议、地址与串口参数 | 待提供，未接入，不会猜测报文 |
| 录像数据盘（1 TB） | 现场未连接，录像与回放接口未在实物上验证 |
| SDI 摄像机 | 未接入，sdi 路目前只有 SE2017 自身输出 |
| 时码适配器（USB 转 RS485，500000 波特） | 服务已部署，现场时码有效性待接入验证 |
| 播放延迟 | 未实测，WebRTC 预期 100–200 ms，RTSP 150–300 ms，需用 mobocon 现场测量 |
| mobocon 侧的 WHEP 与 RTSP 接入代码 | 未编写，本仓库只提供示例 |
| Mobocom V2.2 网络节点 `ss07-mobocom-node`（VIDEO_SONAR 0x9009） | 0.10.9 已实现并通过 5/5 黄金向量；实网验证记录见 [mobocon-network-node.md](mobocon-network-node.md)。协议库快照的来源提交尚未推送到 codeup |

## 7 变更记录

| 版本 | 日期 | 内容 |
| --- | --- | --- |
| 0.10.10 | 2026-11-06 | `sdi` 补上宽高、实测帧率和码率；读取 SE2017 输入状态，无信号时 `availability=no_signal` 并告警 `SDI_NO_SIGNAL`；新增免密钥可用的 `GET /api/v1/media/sessions`；测试计划改正 wire_version 为 0x22（十进制 34） |
| 0.10.9 | 2026-11-06 | 新增独立 Rust 服务 `ss07-mobocom-node`（VIDEO_SONAR 0x9009，UDP 47821/239.255.33.21，仅身份、发现与安全待机）和可选免密钥局域网监看 lan-monitor；`ss07 node-info`、`ss07 node-service install/uninstall/status` |
| 0.10.6 | 2026-11-05 | 新增 API 客户端 Bearer 密钥、WebRTC/WHEP 与 RTSP 对 mobocon 开放、可选明文 RTSP 端口 8555；已部署到 192.168.3.233 并验证六路拉流 |

## 8 维护规则

- 接口、端口、角色或错误码变化时，同一次改动里更新本文和 [mobocon-video-api.md](mobocon-video-api.md)。
- 不在文档、仓库或日志中写入密钥。示例使用 `<密钥>` 占位。
- 未经实物验证的内容写进第 6 节，验证后再移出，并记录证据位置。
