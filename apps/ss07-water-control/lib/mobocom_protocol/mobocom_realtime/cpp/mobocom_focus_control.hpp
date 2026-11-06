#pragma once
#include "mobocom_configuration.hpp"
#include <array>

namespace mobocom {
constexpr std::uint16_t FocusControlSchema=4;
// Board-local focus source; deliberately distinct from DeviceCore::Source.
enum class FocusControlSource : std::uint8_t { Wireless=0, Wired=1, Stopped=2, Fault=3 };
struct FocusControlStatus {
    FocusControlSource source=FocusControlSource::Stopped;
    bool output_idle=false, radio_ready=false, bus_quiet=false;
    std::uint32_t button_remaining_ms=0;
    bool wired_commands=false;
};
inline std::size_t encode_focus_control(std::uint8_t* out,const FocusControlStatus& s) {
    if(!out||std::uint8_t(s.source)>3||(s.output_idle&&s.button_remaining_ms))return 0;
    out[0]=std::uint8_t(s.source);out[1]=(s.output_idle?1:0)|(s.radio_ready?2:0)|(s.bus_quiet?4:0)|(s.wired_commands?8:0);
    put16(out+2,0);put32(out+4,s.button_remaining_ms);return 8;
}
inline bool decode_focus_control(const std::uint8_t* data,std::size_t size,FocusControlStatus& s) {
    if(!data||size!=8||data[0]>3||(data[1]&~15)||u16(data+2)||(data[1]&1&&u32(data+4)))return false;
    s={FocusControlSource(data[0]),bool(data[1]&1),bool(data[1]&2),bool(data[1]&4),u32(data+4),bool(data[1]&8)};return true;
}
inline Error decode_focus_source(const std::uint8_t* data,std::size_t size,FocusControlSource& source) {
    if(!data||size!=1)return Error::Malformed;
    if(data[0]>2)return Error::Range;
    source=FocusControlSource(data[0]);return Error::Ok;
}
// Normalized positions, independent of the downstream motor protocol.
struct FocusCommand { std::uint8_t operation=0, mask=0; std::array<std::uint16_t,3> positions{}; };
inline Error decode_focus_command(const std::uint8_t* data,std::size_t size,FocusCommand& out) {
    if(!data||!size)return Error::Malformed;
    FocusCommand c{};c.operation=data[0];
    if(c.operation==0x10){
        if(size!=8)return Error::Malformed;
        c.mask=data[1];if(!c.mask||(c.mask&~7))return Error::Range;
        for(unsigned i=0;i<3;++i){c.positions[i]=u16(data+2+2*i);if(!(c.mask&(1u<<i))&&c.positions[i])return Error::Range;}
    }else if(c.operation==0x11){
        if(size!=2)return Error::Malformed;
        if(data[1]>1)return Error::Range;c.mask=data[1];
    }else return Error::Unsupported;
    out=c;return Error::Ok;
}
} // namespace mobocom
