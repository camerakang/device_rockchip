#pragma once
// Software fixture only; does not drive or confirm real hardware.
#include "mobocom_device.hpp"
namespace mobocom::examples {
struct VirtualDriver {
    std::uint64_t safe_started=0,safe_delay=0,action_started=0,action_delay=20000;
    unsigned safe_calls=0,apply_calls=0,action_calls=0,flush_calls=0;
    bool safe_fail=false,apply_fail=false,reset_ok=true;
    std::int32_t coupled_limit=0;
    std::array<std::int32_t,16> outputs{};
    DriverHooks hooks(){
        DriverHooks h{};h.context=this;
        h.validate_targets=[](void* c,const Target* values,std::size_t count){
            const auto limit=static_cast<VirtualDriver*>(c)->coupled_limit;
            if(!limit)return true;
            std::int64_t sum=0;for(std::size_t i=0;i<count;++i)sum+=values[i].value;
            return sum<=limit;
        };
        h.apply_targets=[](void* c,const Target* t,std::size_t n,std::uint64_t){auto& d=*static_cast<VirtualDriver*>(c);++d.apply_calls;if(d.apply_fail)return false;for(std::size_t i=0;i<n;++i)d.outputs[t[i].endpoint*4+t[i].resource]=t[i].value;return true;};
        h.start_safe=[](void* c,Fault,std::uint64_t now){auto& d=*static_cast<VirtualDriver*>(c);d.safe_started=now;++d.safe_calls;d.outputs.fill(0);return !d.safe_fail;};
        h.safe_confirmed=[](void* c,std::uint64_t now){auto& d=*static_cast<VirtualDriver*>(c);return !d.safe_fail&&now-d.safe_started>=d.safe_delay;};
        h.reset_allowed=[](void* c){return static_cast<VirtualDriver*>(c)->reset_ok;};
        h.flush_remote=[](void* c){++static_cast<VirtualDriver*>(c)->flush_calls;};
        h.validate_action=[](void*,const ActionSpec&,const std::uint8_t*,std::size_t){return true;};
        h.start_action=[](void* c,const ActionSpec&,const std::uint8_t*,std::size_t,std::uint64_t,std::uint64_t now){auto& d=*static_cast<VirtualDriver*>(c);++d.action_calls;d.action_started=now;return true;};
        h.poll_action=[](void* c,std::uint64_t,std::uint64_t now){auto& d=*static_cast<VirtualDriver*>(c);return now-d.action_started>=d.action_delay?Outcome::Success:Outcome::Running;};
        h.cancel_action=[](void*,std::uint64_t){};
        h.feedback=[](void*,std::uint16_t,std::uint16_t,std::int32_t&){return false;};
        return h;
    }
};
inline constexpr ResourceSpec lens_resources[]={{1,1,0x8001,1,0,10000},{1,2,0x8001,1,0,10000},{1,3,0x8001,1,0,10000}};
inline constexpr ResourceSpec gimbal_resources[]={{1,1,0x8002,3,-180000,180000,1,90000,180000},{1,2,0x8002,3,-180000,180000,1,90000,180000}};
inline constexpr ResourceSpec fan_resources[]={{1,1,0x8003,1,0,10000},{1,2,0x8003,1,0,10000},{1,3,0x8003,1,0,10000},{2,1,0x8003,1,0,10000},{2,2,0x8003,1,0,10000},{2,3,0x8003,1,0,10000},{3,1,0x8003,1,0,10000},{3,2,0x8003,1,0,10000},{3,3,0x8003,1,0,10000}};
inline constexpr ActionSpec lens_action{1,7,0,500000},gimbal_action{1,3,0,500000},fan_action{1,0,0,500000};
// 示例索引1/2/3选择虚拟跟焦/云台/风机；类型取自公共枚举，不能按索引拼出新ID。
// 资源unit与类型ID分别定义；虚拟时序和范围不能直接用来驱动真实设备。
inline DeviceProfile profile(unsigned i){return {device_type_id(i==1?DeviceType::VirtualLens:i==2?DeviceType::VirtualGimbal:DeviceType::VirtualFans),1,1,i==1?lens_resources:i==2?gimbal_resources:fan_resources,std::uint16_t(i==1?3:i==2?2:9),i==1?&lens_action:i==2?&gimbal_action:&fan_action,1,6,100,10000,20000,1000000,50000,3000000,100000,100000};}
}
