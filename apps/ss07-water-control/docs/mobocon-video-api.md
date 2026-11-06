# mobocon 视频拉流与配置接口（0.10.10）

mobocon 是外部 PC 控制程序。RK 向它提供两类接口：用 HTTPS JSON API 读取状态、修改配置；从流媒体服务器拉取六路视频，可选 WebRTC（WHEP）或 RTSP/TCP。这里不承载云台、跟焦或任何运动命令，RK 也不转发这类命令。

## 流媒体服务器选择

继续使用已锁定并集成的 **MediaMTX v1.21.2**。它已经在板上接收五路 USB 相机 H.264 和 SE2017 的 SDI 流，同时供网页 WebRTC 和录像使用。对外 relay 按需从内部 MediaMTX 拉流，只做转发，不解码也不重新编码，新增 CPU 开销很小；延迟主要来自相机编码和播放器缓冲。

| 方案 | 结论 |
| --- | --- |
| MediaMTX（采用） | 单个静态二进制，RTSP/RTSPS/WebRTC 同时可用，支持连接鉴权回调，已通过板端六路验证 |
| SRS | 偏 RTMP/HLS/WebRTC 直播，需要再引入一套配置与鉴权 |
| ZLMediaKit | 功能全，但要在 SDK 内交叉编译 C++ 依赖，维护成本更高 |
| GStreamer RTSP Server | 需自写服务和鉴权，复用不到现有录像/WebRTC 链路 |

```text
五路 USB H.264 ─┐
SE2017 SDI ─────┴─> 内部 MediaMTX (127.0.0.1:8554) ─> 录像
                              ├─> WebRTC：信令 HTTPS :8443 /media/<路径>/whep，媒体 UDP :8189
                              │
                              └─> 外部 relay ─> RTSP  :8555（局域网明文，可选）
                                             └─> RTSPS :8322（TLS）
Python 状态/配置 API ─> HTTPS :8443 /api/v1/...（Bearer 密钥）
```

## 板端开启（部署后由 root 执行）

1. 创建 `/etc/ss07-gateway/rtsp.json`（root 所有，0644）：

   ```json
   {"schema_version":1,"enabled":true,"port":8322,"plain_port":8555}
   ```

   `plain_port` 可选。省略时只开放 RTSPS；填写后同时开放局域网明文 RTSP，方便 ffmpeg 和 Node 客户端直接读取。明文 RTSP 会以明文发送用户名和密钥，只应在受控的设备网中使用。

2. 为 mobocon 创建 API 客户端密钥。密钥只在创建或轮换时显示一次，文件里只保存 SHA-256：

   ```sh
   sudo /opt/ss07/current/tools/ss07 api-client add \
     --api-clients /etc/ss07-credentials/api-clients.json \
     --name mobocon --role maintainer --group ss07-web
   ```

   其他子命令：`list`、`rotate`、`enable`、`disable`、`remove`。角色可选 `monitor`（状态和视频）、`reader`（另可读录像）、`maintainer`（另可读改配置）。密钥被禁用、轮换或删除时，外部 relay 会断开所有外部播放器，客户端需重新连接。

3. 重启 `ss07-media` 与 `ss07-api`，然后读取 `GET /api/v1/streams`，确认每路出现 `rtsp_url`。

## 视频拉流：WebRTC（WHEP）

适合 mobocon 界面里实时显示画面，延迟最低，一般 100–200 ms。信令走 API 网关，媒体走 UDP 8189，PC 需要能访问板子这个端口。

1. `POST https://192.168.3.233:8443/media/<路径>/whep`，请求头 `Authorization: Bearer <密钥>`、`Content-Type: application/sdp`，请求体是 SDP offer。
2. 返回 201，响应体是 SDP answer，`Location` 是会话地址。
3. 结束时 `DELETE <Location>`（带同一把密钥）。只有创建会话的客户端能关闭它。

会话上限：每个 API 客户端最多 12 个，与网页监看合计最多 48 个，六路同时开不受影响。密钥被禁用、轮换或删除后，板子会在约 1 秒内关闭它打开的 WebRTC 会话。

API 不返回跨域（CORS）头，浏览器页面不能直接带 Bearer 头访问它。Electron 中请在主进程（Node）发送 WHEP 请求，经 IPC 把 SDP 交给渲染进程的 `RTCPeerConnection`：

```js
// 主进程
ipcMain.handle('whep', async (_e, channel, offer) => {
  const r = await fetch(`https://192.168.3.233:8443/media/${channel}/whep`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${process.env.SS07_KEY}`, 'Content-Type': 'application/sdp' },
    body: offer,
  });
  if (r.status !== 201) throw new Error(`WHEP ${r.status}`);
  return { answer: await r.text(), location: r.headers.get('Location') };
});

// 渲染进程
async function play(channel, video) {
  const pc = new RTCPeerConnection();
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.ontrack = (e) => { video.srcObject = e.streams[0]; };
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((ok) => pc.iceGatheringState === 'complete' ? ok()
    : pc.addEventListener('icegatheringstatechange', () => pc.iceGatheringState === 'complete' && ok()));
  const { answer, location } = await window.ss07.whep(channel, pc.localDescription.sdp);
  await pc.setRemoteDescription({ type: 'answer', sdp: answer });
  return { pc, location };
}
```

纯 Node 后台也可以用 werift 或 node-datachannel 走同样的 WHEP 流程；如果只是转存或交给其他程序处理，用下面的 RTSP 更简单。

## 视频拉流：RTSP/TCP

六个固定路径：`front`、`back`、`left`、`right`、`down`、`sdi`。用户名是客户端名（如 `mobocon`），密码是 API 密钥。只支持 TCP 传输，每路最多 4 个外部读者。

```sh
ffmpeg -rtsp_transport tcp -i "rtsp://mobocon:<密钥>@192.168.3.233:8555/front" -c copy -t 10 front.mp4
ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay "rtsp://mobocon:<密钥>@192.168.3.233:8555/front"
```

RTSPS 地址为 `rtsps://192.168.3.233:8322/<路径>`，客户端需要信任网关证书。

直通转发本身只增加几毫秒。USB 相机硬件 H.264、局域网和低缓冲播放器组合一般在 150–300 ms，以现场实测为准；播放器默认缓冲常常是延迟的主要来源。

## 免密钥局域网监看（lan-monitor，0.10.9）

设备发现见 [Mobocon 网络节点](mobocon-network-node.md)：Mobocon 用组播 DISCOVER 得到 UID 和当前 IP，再用该 IP 访问下面的接口。

用于 Mobocon 在受控设备网内不带密钥取六路视频和状态。默认关闭；只有根管理员创建配置文件后才生效。

`/etc/ss07-gateway/lan-monitor.json`（root 所有，0644；样例 `config/lan-monitor.json.example`）：

```json
{"schema_version":1,"enabled":true,"allow_cidrs":["192.168.3.0/24"]}
```

`allow_cidrs` 只写 Mobocon 所在网段（最多 16 项，严格网段写法）。文件不存在、无法解析、权限可被他人改写、`enabled=false`、列表为空或含 `0.0.0.0/0` 时，模式均为关闭，服务照常启动并在日志记录原因。API 和 RTSP relay 每秒检查一次文件，修改或删除后无需重启即生效；RTSP relay 检测到变化时会断开全部外部播放器并重新启动。

来源地址由 HTTPS 网关按 TCP 对端写入，覆盖客户端自带的同名请求头；API 本身只监听 127.0.0.1。

| 请求（白名单来源，无 Authorization、无 Cookie） | 结果 |
| --- | --- |
| `GET /api/v1/status`、`/streams`、`/sonars`、`/timecode`、`/events`、`/media/sessions` | 200；`/media/sessions` 只列出同一来源 IP 的会话 |
| `POST /media/<通道>/whep` | 201；会话属主为来源 IP |
| `DELETE /media/<通道>/whep/<id>` | 只能关闭同一来源 IP 创建的会话，否则 404 |
| 录像、时码/声呐历史、`/api/v1/config`、`/logs`、所有 PUT/POST 维护接口 | 403 `LAN_MONITOR_FORBIDDEN` |
| `rtsp://192.168.3.233:8555/<通道>`、`rtsps://…:8322/<通道>`，不带用户名 | 只读放行；publish/写入一律拒绝 |

非白名单来源不带密钥访问受保护接口返回 401。带 Bearer 的请求完全按原规则处理；Bearer 与 Cookie 同时出现仍返回 401。WebRTC 会话上限沿用原计数：每个来源 IP 最多 12 个，总数 48 个。`/api/v1/streams` 的 `rtsp_url` 不含用户名和密钥。

日志（`journalctl -u ss07-api -u ss07-media`，前缀 `lan-monitor:`）记录模式状态、无密钥 WHEP 会话建立/关闭，被拒来源每 10 秒最多记一行；不记录任何凭据。

关闭方法：删除或改名该文件，或改 `"enabled": false`。关闭后无密钥请求立即回到 401，已建立的无密钥 WHEP 会话在下一次属主检查时撤销，RTSP relay 重启并断开无密钥播放器；Bearer 流程和 `ss07 api-client` 命令不变。

注意：网页控制台仍是 0.10.3 起的免登录直接访问模式，能打开网页的浏览器会得到维护会话。lan-monitor 只约束不带 Cookie 和密钥的程序请求，不收紧网页本身；如需限制网页访问，应在网络层隔离或恢复账号模式。

## SDI 输入状态（0.10.10）

`sdi` 路另有三个字段：`input_signal` 为编码器报告的 SDI 输入状态（`present` 有信号、`no_signal` 无信号、`unknown` 读不到编码器状态），`input_info` 为编码器原文（如 `SDI NoSignal N/A`），`input_error` 为读取失败原因。SDI 无信号时，SE2017 仍会输出一路全黑的 1920×1080 码流，因此这时 `availability=no_signal`、`capture_state=NO_SIGNAL`、`error=sdi_no_signal`，并且不提供 `live_url`、`rtsp_url`、`native_url`；`/api/v1/status` 的告警里会有 `SDI_NO_SIGNAL`。`input_signal=unknown` 时仍按码流是否到达判断 `available`。`sdi` 的 `width`、`height` 取自转发码流，`fps_observed` 为 RK 实测的转发帧率，`bitrate_observed` 为接收码率（bit/s）。

`availability` 的取值：`available` 可播放；`no_signal` 码流在但没有输入信号（目前只有 `sdi`）；`unavailable` 无码流或设备异常。

板上需要一份 SE2017 网页账号文件，媒体服务才能读取输入状态（只调用编码器网页自己使用的状态查询，不改编码器设置）。由 root 创建 `/etc/ss07-credentials/sdi-encoder.json`，属主 root、组 `ss07-web`、权限 0640（样例 `config/sdi-encoder.json.example`）：

```json
{"schema_version":1,"model":"avmatrix-se2017","username":"admin","password":"<编码器网页密码>"}
```

文件不存在时 `input_signal=unknown`、`input_error=encoder_status_not_configured`；权限过宽或格式错误时为 `encoder_credentials_invalid`；密码错误为 `encoder_auth_failed`；编码器不可达为 `encoder_unreachable`。修改后重启 `ss07-media` 生效。

## HTTP API

基地址 `https://192.168.3.233:8443`，每个请求带 `Authorization: Bearer <密钥>`。使用 Bearer 时不要同时发送 Cookie，也不需要 CSRF 或 Origin。

| 方法与路径 | 最低角色 | 说明 |
| --- | --- | --- |
| `GET /api/v1/status` | monitor | 完整状态：相机、声呐、时码、存储、告警 |
| `GET /api/v1/streams` | monitor | 六路视频状态，含 `rtsp_url`（明文）和 `native_url`（RTSPS） |
| `GET /api/v1/sonars` | monitor | 声呐状态；厂家协议接入前没有测量值 |
| `GET /api/v1/timecode` | monitor | 只读 RS485 时码状态 |
| `GET /api/v1/events` | monitor | SSE 状态推送 |
| `GET /api/v1/media/sessions` | monitor | 调用者自己的 WHEP 会话列表、数量与全局总数（0.10.10） |
| `POST /media/<路径>/whep`、`DELETE <Location>` | monitor | WebRTC 拉流会话 |
| `GET /api/v1/config` | maintainer | 候选配置 `persisted`、`revision`、`active_revision`、逐服务应用结果 |
| `PUT /api/v1/config` | maintainer | 保存候选：`{"candidate":{...},"expected_revision":"<64位hex>"}` |
| `POST /api/v1/config/plan` | maintainer | 预览影响：`{"expected_revision","expected_active_revision"}` |
| `POST /api/v1/config/apply` | maintainer | 应用；202 只表示受理，之后轮询 `GET /api/v1/config` |
| `POST /api/v1/config/recover` | maintainer | 重试未完成的恢复：`{"expected_operation_id"}` |
| `GET /api/v1/logs?source=application` | maintainer | 配置应用审计 |

修改流程：`GET /api/v1/config` 取得 `persisted` 和 `revision`；修改后 `PUT` 保存，得到新 revision；`plan` 查看会重启哪些服务；`apply` 后轮询 `application.state`，直到 `APPLIED`、`ROLLED_BACK` 或 `ROLLBACK_FAILED`。revision 不匹配返回 409，表示期间有人修改，需要重新读取。应用期间 API 可能重启，短暂连接失败时继续轮询，不要自动重复提交。审计中的操作者记为 API 客户端名。

Node.js 22 示例（自签名证书需通过 `NODE_EXTRA_CA_CERTS` 信任网关证书）：

```js
const base = 'https://192.168.3.233:8443';
const headers = { Authorization: `Bearer ${process.env.SS07_KEY}` };
const get = async (path) => (await (await fetch(base + path, { headers })).json()).data;

const { streams } = await get('/api/v1/streams');
for (const s of streams) console.log(s.channel_id, s.state, s.rtsp_url);

const cfg = await get('/api/v1/config');
const plan = await fetch(base + '/api/v1/config/plan', {
  method: 'POST',
  headers: { ...headers, 'Content-Type': 'application/json' },
  body: JSON.stringify({ expected_revision: cfg.revision, expected_active_revision: cfg.active_revision }),
});
console.log(plan.status, await plan.json());
```

| 状态码 | 含义 |
| --- | --- |
| 401 | 密钥错误或已禁用、未配置客户端文件，或 Bearer 与 Cookie 同时出现 |
| 403 | 角色权限不足 |
| 404 | 关闭不属于自己的 WebRTC 会话 |
| 409 | revision 冲突 |
| 422 | 请求体不合法 |
| 503 | 维护服务不可用、结果未确认，或 WebRTC 会话达到上限（`MEDIA_SESSION_LIMIT`） |
