# Sonar Drive V1.1 接入与验收

U10 → USB Host，声呐板外部供电。U9 用于调试；时码适配器另接。业务服务使用 Linux termios，无需安装 pySerial。先按插拔对照确认 U10 的 by-path，不能用 ttyUSB 编号代替。五路由固件轮询，RK 独占只读，不发送启动命令。外部测量状态经现有 GET /api/v1/sonars 获取；历史和可选数据盘归档沿用现有服务。

在系统配置的 sonar 段填写以下内容；路径为占位符，方向映射仅为示例，必须按实际安装替换。默认示例系统配置保持未绑定。

```json
{"vendor_protocol":"sonar_drive_v1_1","binding":"/dev/serial/by-path/REPLACE_WITH_U10",
 "uart":{"baud":115200,"data_bits":8,"parity":"none","stop_bits":1},
 "nodes":{"front":{"address":"1"},"back":{"address":"2"},"left":{"address":"3"},"right":{"address":"4"},"down":{"address":"5"}}}
```

地址是字符串通道号，不是插座编号：CH1=CN4、CH2=CN2、CH3=CN3、CH4=CN5、CH5=CN6。无回波等状态立即清空距离；1.5 秒无新帧或断连后不显示旧值。历史事件 correlation_basis=device_stream、request_id=null，时间取 RK 收齐帧时的单调时钟。固件 ms 不作为外部时码。

实机联调先保持生产服务未占用此串口，在有 pySerial 的板端使用 tools/sonar-v11/accept_v11.py 按[厂方指南](sonar-v11-vendor.md)验收。指定实际路径、--gpio4-rework、--seconds 60 和全新的 --log 文件；检查 stream_pass=true、bad=0、gaps=0。再应用现场配置，读取五个方向并验证拔线后全部无有效距离。no_echo 可证明通路工作；测距精度需另做水下靶距测试。波形、阈值、暂停等调试命令不通过业务 API 开放。

## 现场部署

2026-11-20：RK 192.168.3.233 已部署 0.10.13。U10 实机串口 60 秒收到 600 帧，五路各 120 帧，bad=0、gaps=0；部署后 HTTPS GET /api/v1/sonars 五路序号均持续递增。

现场逻辑方向：CH1/CN4=前，CH2/CN2=后，CH3/CN3=左，CH4/CN5=右，CH5/CN6=下。实际探头朝向和水下靶距精度未验收。

绑定路径：`/dev/serial/by-path/platform-fc880000.usb-usb-0:1.2:1.0-port0`。0.10.13 的在线配置 plan 仍拒绝声呐段变更；现场由管理员核对候选与活动配置仅 sonar 段不同后同步活动配置，重启 ss07-sonar、ss07-api 生效。

### 0.10.15 声呐参数接口

白名单内 mobocon 不带 Authorization 和 Cookie。通道是板上 CH1～5，与方向映射分开：
- GET /api/v1/sonars/2/params：回包 data.channel 和 data.params（七个整数字段）。
- PUT 同一路径，JSON 示例：`{"key":"blank_us","value":1500}`。一次只改一项；先读原值，相同则跳过写入，不同则等 ACK 再读回。超时先重新读取，不要自动重写 NVS。
- 字段：thr_mv、blank_us、range_mm、sound_mps、offset_mm、width、gain_x10；范围及组合约束见声呐板交付指南。422 表示参数非法或组合被拒；502 表示固件拒绝读取或写后不一致；503 表示服务或串口不可用，504 表示未收到回复。旧固件可以继续测距，但可能拒绝 get；此时客户端应禁用调参，不显示假设的默认值。
- 方向 / USB 绑定使用原有 maintainer 配置流程 GET config → PUT config → plan → apply → 重新建立会话后确认 APPLIED。V1.1 声呐独立变更会重启 sonar 和 api，不重启摄像头。

2026-11-20 实机验收：192.168.3.233 正式安装 0.10.15（COMMITTED，软件健康检查通过）。通过 plan/apply 将 U10 在线绑定到 /dev/serial/by-path/platform-xhci-hcd.9.auto-usb-0:1.3:1.0-port0，已 APPLIED；仅重启 sonar、api。CH1～5 依次对应前后左右下，五路样本序号均推进。白名单内逐路 GET 参数两轮成功；CH2 的 thr_mv 从 800 写为 801，再恢复 800 并独立读回；越界值返回 422。快速连续请求时实物偶有 SONAR_COMMAND_REJECTED，须按返回结果处理，写入不确定时先读回，不自动重写。水下精度与断电保存未验收。

409 `SONAR_PARAMS_BUSY`：已有请求在执行或刚超时；等待后重新读取。七项均为整数：thr_mv 200–3200，blank_us 200–10000，range_mm 500–20000，sound_mps 1400–1600，offset_mm −1000～1000，width 1–16，gain_x10 仅 100/125/150/200。
