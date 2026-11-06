#pragma once
// TREADMILL (0x9003) 速度型类别契约，见 docs/TREADMILL_PROFILE.md。
// 不改变 wire 0x22，不新增类型 ID；速度单位 cm/s，实测反馈为双驱平均速度。
#include "mobocom_device.hpp"

namespace mobocom::treadmill {

inline constexpr std::uint16_t ProfileVersion = 1;
inline constexpr std::uint16_t SpeedUnit = 0x9003;     // 单位码：cm/s（独立于 type 的命名空间）
inline constexpr std::int32_t MaxSpeedCmPerS = 600;    // 初始配置，须以实机标定为准
inline constexpr std::uint16_t Endpoint = 1;
inline constexpr std::uint16_t SpeedResource = 1;

// flags: bit0 持续输出（需目标新鲜度保护），bit1 提供实测反馈。
inline constexpr ResourceSpec Resources[] = {
    {Endpoint, SpeedResource, SpeedUnit, 3, -MaxSpeedCmPerS, MaxSpeedCmPerS, 0, 0, 0},
};

// 没有动作：无 HOME/SET_ZERO/SET_RANGE（增量编码器，不提供绝对位置）。
inline DeviceProfile profile(std::uint32_t firmware) {
    DeviceProfile p{};
    p.type = device_type_id(DeviceType::Treadmill);
    p.version = ProfileVersion;
    p.firmware = firmware;
    p.resources = Resources;
    p.resource_count = sizeof(Resources) / sizeof(Resources[0]);
    p.actions = nullptr;
    p.action_count = 0;
    p.source_mask = 6;               // REMOTE(RS485) + MOBOCOM
    p.max_target_hz = 100;
    p.update_gap_us = 20000;
    p.target_min_us = 20000;
    p.target_max_us = 1000000;
    p.lease_min_us = 50000;
    p.lease_max_us = 3000000;
    p.safe_timeout_us = 5000000;
    p.switch_timeout_us = 5000000;
    p.auto_reset_mask = 0;           // 目标过期/租约超时均不自动恢复
    return p;
}

} // namespace mobocom::treadmill
