# 主机纯授时模块

用于发生器、Mobocom、UE，以及 SS-07 RK3588 的只读采集记录关联。RK 是记录主机，不是实体控制接收器；具体映射/失时记录契约见 [RK时码设计](../docs/ss07/rk-timecode.md)。DeviceCore 不依赖本模块，不将时码转发给设备，不绑定 NRF24、CDC 或设备驱动。

TimeCode 独立 wire version=`0x12`，每 10000µs 发一帧，长度 26B。多字节小端，CRC16 CCITT-FALSE。格式保持已有发生器兼容，不因 Control 升级到 `0x21` 改变。

| 偏移 | 字节 | 字段 |
| --- | --- | --- |
| 0 | 2 | D5 7C |
| 2、3 | 各1 | version=12, flags=0 |
| 4 | 8 | epoch_id，非零 |
| 12 | 4 | sequence |
| 16 | 8 | time_us，发送端首个 SOF 字节起始位时刻 |
| 24 | 2 | CRC，覆盖 `[2,24)` |

非 UTC 时间。接入适配器提供与该 SOF 关联的本地单调 origin_us，以及解析时 observed_us；串口 write 返回或 USB 收包时间不自动等于 SOF。无法校准时只能报告估计时间，不声明绝对误差或跨主机同步精度。

同纪元序号前向差 k 必须为 1～`2^31-1`，时间差在 `9950k～10050k`µs 内；重复、旧帧和错误帧不刷新年龄。新纪元允许重置序号。观测处理延迟达到 30000µs 的帧不作为有效映射。

HostTimeline 每次 start 用最新有效帧建立本场锚点；now 用本地单调时间连续推进。30ms 未获得有效帧标记 fallback，本场不停止、不跳转。恢复时码或换纪元更新下一场锚点，本场 fallback 保持到结束。下一场 start 要求有效时码；尚无有效时码时自动播放不能宣称已对齐，独立手动实时控制仍可使用。

上述 HostTimeline 行为是公共参考实现和 Mobocom 的既定播放策略，不代表 UE 应用已经接入。SS-07 系统设计仍须明确 UE 的开始/结束、失时后继续或暂停、恢复及下一场对齐策略，并在两端显示失时状态。若 UE 选择不同策略，需另行定义它与 Mobocom 的联动；两端各自使用本地时钟续播也不构成失时期间的同步保证。本次交付不修改或验收 UE 工程。

SDK 使用 u64/bigint 微秒避免 JS 时间精度损失。本地时钟倒退报告错误，不继续错误映射。此实现不含设备时钟调谐、不假设浏览器定时器为硬实时。

Python：[reference.py](reference.py)；C++：[mobocom_timecode.hpp](cpp/mobocom_timecode.hpp)；TypeScript：[index.ts](typescript/index.ts)。TypeScript HostTimeline 也从公共 SDK 导出，但模块实现独立，不引用 Control 编码器。Rust：[timecode.rs](../mobocom_realtime/rust/src/timecode.rs)，导出独立 `Packet/HostTimeline`，保持0x12，不被 Rust DeviceCore 使用。
