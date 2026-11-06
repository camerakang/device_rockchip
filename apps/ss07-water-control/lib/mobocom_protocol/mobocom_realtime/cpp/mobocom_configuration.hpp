#pragma once
#include "mobocom_wire.hpp"

namespace mobocom {
enum class ConfigurationState : std::uint8_t { Idle=0, Pending=1, Applied=2, Failed=3 };
constexpr std::uint16_t DualRadioSchema=1;
constexpr std::size_t ConfigurationHeaderBytes=23;
struct ConfigurationSnapshot {
    std::uint32_t revision=0;
    ConfigurationState state=ConfigurationState::Idle;
    Error error=Error::Ok;
    std::uint64_t request_session=0;
    std::uint32_t request_sequence=0;
    std::uint16_t length=0;
    std::array<std::uint8_t,MaxPayload-ConfigurationHeaderBytes> values{};
};
// All callbacks run on the DeviceCore owner. Bounded RAM updates or enqueue only; no flash/SPI wait.
struct ConfigurationHooks {
    void* context=nullptr;
    Error (*read)(void*,std::uint16_t,ConfigurationSnapshot&)=nullptr;
    Error (*write)(void*,std::uint16_t,std::uint32_t,const std::uint8_t*,std::size_t,
                   std::uint64_t,std::uint32_t,std::uint64_t)=nullptr;
    bool (*busy)(void*)=nullptr;
};
inline std::size_t encode_configuration(std::uint8_t* out,std::uint16_t schema,const ConfigurationSnapshot& s) {
    if(!out||s.length>s.values.size())return 0;
    put16(out,schema);put32(out+2,s.revision);out[6]=std::uint8_t(s.state);put16(out+7,std::uint16_t(s.error));
    put64(out+9,s.request_session);put32(out+17,s.request_sequence);put16(out+21,s.length);
    std::copy_n(s.values.data(),s.length,out+23);return 23+s.length;
}
inline bool decode_configuration(const std::uint8_t* data,std::size_t size,std::uint16_t& schema,ConfigurationSnapshot& s) {
    if(!data||size<23||data[6]>3||u16(data+21)>s.values.size()||size!=23u+u16(data+21))return false;
    schema=u16(data);s.revision=u32(data+2);s.state=ConfigurationState(data[6]);s.error=Error(u16(data+7));
    s.request_session=u64(data+9);s.request_sequence=u32(data+17);s.length=u16(data+21);
    std::copy_n(data+23,s.length,s.values.data());return true;
}
} // namespace mobocom
