#pragma once
#include <array>
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <algorithm>

namespace mobocom {
constexpr std::uint8_t Version=0x22;
constexpr std::size_t MaxPayload=240, HeaderSize=37, MaxFrame=HeaderSize+MaxPayload+2;
// One UDP datagram carries exactly one frame; decode() rejects trailing or missing bytes.
constexpr std::uint64_t AllNodes=0; // dst_uid=0 denotes the fixed multicast group (all nodes).
enum class Kind:std::uint8_t {Request=1,Response=2,Ack=3,Error=4};
enum class Command:std::uint16_t {Hello=1,Discover=2,Info=4,Caps=5,SessionOpen=6,SetSource=0x10,Heartbeat=0x11,WriteTargets=0x20,ExecuteAction=0x21,ActionStatus=0x22,Status=0x30,Stop=0x40,Estop=0x41,ResetFault=0x42,ReadConfig=0x50,WriteConfig=0x51};
enum class Error:std::uint16_t {Ok=0,Malformed=1,Unsupported=2,State=3,Boot=4,Generation=5,Owner=6,Sequence=7,Conflict=8,Range=9,Group=10,Busy=11,Space=12,Safety=13,NotFound=14,Address=15,Capability=16,Bandwidth=17,Timeout=18,Driver=19,Epoch=20,Session=21};
inline std::uint16_t u16(const std::uint8_t* p){return p[0]|std::uint16_t(p[1])<<8;}
inline std::uint32_t u32(const std::uint8_t* p){return u16(p)|std::uint32_t(u16(p+2))<<16;}
inline std::uint64_t u64(const std::uint8_t* p){return u32(p)|std::uint64_t(u32(p+4))<<32;}
inline std::int32_t i32(const std::uint8_t* p){auto v=u32(p);std::int32_t r;std::memcpy(&r,&v,4);return r;}
inline void put16(std::uint8_t* p,std::uint16_t v){p[0]=v;p[1]=v>>8;}
inline void put32(std::uint8_t* p,std::uint32_t v){put16(p,v);put16(p+2,v>>16);}
inline void put64(std::uint8_t* p,std::uint64_t v){put32(p,v);put32(p+4,v>>32);}
// 32字节只读半字节表，避免长目标帧在MCU上反复逐位计算CRC挤占回复窗口。
// 仍为CCITT-FALSE：0x1021、初值FFFF、不反射；不改变任何线上字节或解析校验。
inline constexpr auto Crc16Nibbles=[] {
    std::array<std::uint16_t,16> table{};
    for(unsigned i=0;i<16;++i){std::uint16_t c=std::uint16_t(i<<12);for(unsigned bit=0;bit<4;++bit)c=std::uint16_t((c<<1)^((c&0x8000)?0x1021:0));table[i]=c;}
    return table;
}();
inline std::uint16_t crc16(const std::uint8_t* p,std::size_t n){
    std::uint16_t c=0xffff;while(n--){c^=std::uint16_t(*p++)<<8;c=std::uint16_t((c<<4)^Crc16Nibbles[c>>12]);c=std::uint16_t((c<<4)^Crc16Nibbles[c>>12]);}return c;
}
struct Message {Kind kind;std::uint64_t src_uid;std::uint64_t dst_uid;std::uint32_t sequence;Command command;std::uint64_t session;const std::uint8_t* payload;std::uint16_t length;};
inline bool decode(const std::uint8_t* p,std::size_t n,Message& m){
    if(!p||n<HeaderSize+2||p[0]!=0xd5||p[1]!=0x7d||p[2]!=Version||p[3]<1||p[3]>4||p[4]||!u32(p+21)||!u64(p+27))return false;
    const auto length=u16(p+35);if(length>MaxPayload||n!=HeaderSize+2u+length||crc16(p+2,n-4)!=u16(p+n-2))return false;
    m={Kind(p[3]),u64(p+5),u64(p+13),u32(p+21),Command(u16(p+25)),u64(p+27),p+37,length};return true;
}
inline std::size_t encode(std::uint8_t* out,const Message& m){
    if(!out||m.length>MaxPayload||!m.sequence||!m.session||unsigned(m.kind)<1||unsigned(m.kind)>4||(!m.payload&&m.length))return 0;
    out[0]=0xd5;out[1]=0x7d;out[2]=Version;out[3]=std::uint8_t(m.kind);out[4]=0;
    put64(out+5,m.src_uid);put64(out+13,m.dst_uid);put32(out+21,m.sequence);put16(out+25,std::uint16_t(m.command));put64(out+27,m.session);put16(out+35,m.length);
    if(m.length)std::memmove(out+37,m.payload,m.length);
    put16(out+37+m.length,crc16(out+2,35+m.length));return HeaderSize+m.length+2;
}
} // namespace mobocom
