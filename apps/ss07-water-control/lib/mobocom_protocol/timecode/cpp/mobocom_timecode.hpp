#pragma once
#include <cstdint>
#include <cstddef>

// This module has no device node dependency and preserves TimeCode version 0x12.
namespace mobocom::timecode {
constexpr std::uint8_t Version=0x12;
constexpr std::uint32_t PeriodUs=10000,LossUs=30000;
struct Packet {std::uint64_t epoch=0;std::uint32_t sequence=0;std::uint64_t time_us=0;};
inline std::uint16_t read16(const std::uint8_t* p){return p[0]|std::uint16_t(p[1])<<8;}
inline std::uint32_t read32(const std::uint8_t* p){return read16(p)|std::uint32_t(read16(p+2))<<16;}
inline std::uint64_t read64(const std::uint8_t* p){return read32(p)|std::uint64_t(read32(p+4))<<32;}
inline void write16(std::uint8_t* p,std::uint16_t v){p[0]=v;p[1]=v>>8;}
inline void write32(std::uint8_t* p,std::uint32_t v){write16(p,v);write16(p+2,v>>16);}
inline void write64(std::uint8_t* p,std::uint64_t v){write32(p,v);write32(p+4,v>>32);}
inline std::uint16_t crc(const std::uint8_t* p,std::size_t n){std::uint16_t c=0xffff;while(n--){c^=std::uint16_t(*p++)<<8;for(int i=0;i<8;++i)c=(c<<1)^((c&0x8000)?0x1021:0);}return c;}
inline bool decode(const std::uint8_t* p,std::size_t n,Packet& out){
    if(!p||n!=26||p[0]!=0xd5||p[1]!=0x7c||p[2]!=Version||p[3]||!read64(p+4)||crc(p+2,22)!=read16(p+24))return false;
    out={read64(p+4),read32(p+12),read64(p+16)};return true;
}
inline bool encode(std::uint8_t* out,const Packet& p){if(!out||!p.epoch)return false;out[0]=0xd5;out[1]=0x7c;out[2]=Version;out[3]=0;write64(out+4,p.epoch);write32(out+12,p.sequence);write64(out+16,p.time_us);write16(out+24,crc(out+2,22));return true;}
class HostTimeline {
    Packet latest_{};std::uint64_t origin_=0,observed_=0,play_origin_=0,play_time_=0,play_epoch_=0,last_local_=0;
    bool has_=false,playing_=false,fallback_=false;
public:
    bool observe(const Packet& p,std::uint64_t origin,std::uint64_t observed){
        if(!p.epoch||origin>observed||observed-origin>=LossUs||(has_&&observed<observed_))return false;
        if(has_&&p.epoch==latest_.epoch){auto k=std::uint32_t(p.sequence-latest_.sequence);if(!k||k>=0x80000000||p.time_us<latest_.time_us)return false;auto d=p.time_us-latest_.time_us;if(d<std::uint64_t(k)*9950||d>std::uint64_t(k)*10050)return false;}
        if(playing_&&(observed-observed_>=LossUs||p.epoch!=play_epoch_))fallback_=true;
        latest_=p;origin_=origin;observed_=observed;has_=true;return true;
    }
    bool start(std::uint64_t local,std::uint64_t& time){
        if(playing_||!has_||local<observed_||local-observed_>=LossUs)return false;playing_=true;fallback_=false;play_origin_=local;play_time_=latest_.time_us+local-origin_;play_epoch_=latest_.epoch;last_local_=local;time=play_time_;return true;
    }
    bool now(std::uint64_t local,std::uint64_t& time){if(!playing_||local<last_local_)return false;last_local_=local;if(local-observed_>=LossUs)fallback_=true;time=play_time_+local-play_origin_;return true;}
    bool fallback()const{return fallback_;}
    void stop(){playing_=false;}
};
} // namespace mobocom::timecode
