# 跟焦 RS422 / RS485 无主板 USB 实板记录

日期：2026-10-29（北京时间）。本条记录参考网页与整合板正常路径的实物联调，不等于 Mobocon 客户端验收。

## 环境与实际流程

- 固件 INFO：正式 `0x00010007`，node=2，boot=47，UID=`0000C03CDB0AF6E8`。身份仅用于追溯，客户端不得硬编码。
- 未接主板 USB 诊断口，未接云台电机；公共状态 FAULT、fault=4、activeMask=0，反馈无效。
- RS422：FTDI `BH007O3D`，1000000、8N1；RS485：FTDI `BH007QEJ`，38400、8N1。以上序列仅用于当次接线识别，不能作为跨设备固定映射。
- 参考网页位于 `integrated_control_comm/tools/gimbal_web`，包含 candidate.6 SDK；测试前整合仓库 HEAD 为 `97d1f90`。

通过 RS422 读取 schema 4：初始 Wireless、revision=1、radio_ready=true。写 Wired 后回读关联 request_session/sequence，revision=2、Applied、error=0、output_idle=true、bus_quiet=true。

自动测试脚本首次发位置时漏传网页 API 要求的 context/revision，被互锁拒绝，未发送电机位置。补齐参数、重新明确有线接管后 revision=3，RS485 发出 `@101000070\r\n`，用户确认“达到0端了”。该拒绝是网页 API 参数检查，不是新增协议字段或固件异常。

随后释放自动测试连接，由用户网页接管。网页曾误选 `Bluetooth-Incoming-Port` 作为顶部 RS422，HELLO 无回复并出现“响应超时”；刷新端口列表、选择 `BH007O3D` 后重新识别成功，跟焦回读恢复，重新有线接管至 revision=4，并连接 `BH007QEJ`。没有改波特率、增加超时或借助主板 USB。用户完成后续网页操作后反馈“测试没有问题”。

## 可确认与未确认范围

| 项目 | 状态 |
| --- | --- |
| 无主板 USB 的 RS422 身份/状态/schema 4读取 | 已验证 |
| 无云台电机、云台 FAULT 时有线切源 | 已关联回读确认，跟焦功能可用 |
| RS485 R1 零位动作 | 已发送，用户确认机械零端 |
| 修正串口选择后的网页跟焦操作 | 用户确认测试正常；没有逐项记录全部操作 |
| 仅 RS422 切回无线后的手柄跟随/校准 | 本轮未单独确认，不沿用此前 USB 切源结果 |
| 云台运动期间双向切源 | 本轮无云台电机，未验证 |
| 按钮忙、丢 ACK、拔线/重连/重启、频道保存后恢复 | 本轮未覆盖 |
| RS485 线级波形、多主争用、长期稳定性 | 未验证 |
| Mobocon 产品客户端 | 未修改、未验收 |

## 接入提示

1. RS422 用于 schema 4 配置与回读，RS485 用于 PDMOVIE 位置/按钮发送；不向 RS422 写入 USB ASCII 命令。
2. 串口能够打开不代表握手成功。明确展示端口用途、身份识别结果，过滤或弱化系统蓝牙/调试虚拟端口；设备重新插入后刷新列表。
3. 云台 FAULT 不应阻止独立跟焦配置。界面提示应说明受影响的模块，不能笼统表示跟焦也不可用。
4. ACK、Applied、output_idle 和 bus_quiet 不代表镜头到位。机械结果须由用户或独立位置反馈确认。
5. 保持 PDMOVIE 0～7200 范围；PD-MX 厂家按钮操作与手柄 ESP32 卡顿升级属于设备侧问题，详见整合板仓库 `docs/focus-troubleshooting-2026-10-29.md`，不改变本协议 schema 或公共状态机。

参考仓库：`https://codeup.aliyun.com/629f6370487c500c27f60603/integrated_control_comm.git`。本次仅更新协议主仓库文档，不手改设备/网页的 SDK vendor，不生成新的 SDK 版本。
