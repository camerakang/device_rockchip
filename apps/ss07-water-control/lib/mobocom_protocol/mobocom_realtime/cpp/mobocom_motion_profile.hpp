#pragma once
#include "mobocom_configuration.hpp"

namespace mobocom {
// Schema 2: volatile Mobocom planner limits. Physical protection belongs to the driver.
constexpr std::uint16_t MotionProfileSchema=2;
constexpr std::uint16_t LiveMotionProfileSchema=3;
constexpr std::size_t MaxMotionProfileResources=7;
struct MotionLimit {
    std::uint16_t endpoint=0,resource=0;
    std::int32_t vmax=0,amax=0;
};
struct MotionLimitRange : MotionLimit {
    std::int32_t min_vmax=0,max_vmax=0,min_amax=0,max_amax=0;
};
inline Error decode_motion_limits(const std::uint8_t* data,std::size_t size,
                                  std::array<MotionLimit,MaxMotionProfileResources>& out,std::size_t& count) {
    count=0;
    if(!data||size<3||data[0]!=2||data[1]!=0||!data[2]||data[2]>out.size()||size!=3u+12u*data[2])return Error::Malformed;
    auto candidate=out;
    for(std::size_t i=0;i<data[2];++i){const auto* p=data+3+12*i;
        candidate[i]={u16(p),u16(p+2),i32(p+4),i32(p+8)};
        if(candidate[i].vmax<=0||candidate[i].amax<=0)return Error::Range;
        for(std::size_t j=0;j<i;++j)if(candidate[i].endpoint==candidate[j].endpoint&&candidate[i].resource==candidate[j].resource)return Error::Group;
    }
    out=candidate;count=data[2];return Error::Ok;
}
inline std::size_t encode_motion_limits(std::uint8_t* out,const MotionLimit* values,std::size_t count) {
    if(!out||!values||!count||count>MaxMotionProfileResources)return 0;
    out[0]=2;out[1]=0;out[2]=std::uint8_t(count);
    for(std::size_t i=0;i<count;++i){auto* p=out+3+12*i;const auto& v=values[i];
        if(v.vmax<=0||v.amax<=0)return 0;
        for(std::size_t j=0;j<i;++j)if(v.endpoint==values[j].endpoint&&v.resource==values[j].resource)return 0;
        put16(p,v.endpoint);put16(p+2,v.resource);put32(p+4,v.vmax);put32(p+8,v.amax);
    }return 3+12*count;
}
inline std::size_t encode_motion_profile(std::uint8_t* out,const MotionLimitRange* values,std::size_t count) {
    if(!out||!values||!count||count>MaxMotionProfileResources)return 0;
    out[0]=2;out[1]=0;out[2]=std::uint8_t(count);
    for(std::size_t i=0;i<count;++i){auto* p=out+3+28*i;const auto& v=values[i];
        if(v.min_vmax<=0||v.min_amax<=0||v.vmax<v.min_vmax||v.vmax>v.max_vmax||v.amax<v.min_amax||v.amax>v.max_amax)return 0;
        for(std::size_t j=0;j<i;++j)if(v.endpoint==values[j].endpoint&&v.resource==values[j].resource)return 0;
        put16(p,v.endpoint);put16(p+2,v.resource);put32(p+4,v.vmax);put32(p+8,v.amax);
        put32(p+12,v.min_vmax);put32(p+16,v.max_vmax);put32(p+20,v.min_amax);put32(p+24,v.max_amax);
    }return 3+28*count;
}
inline bool decode_motion_profile(const std::uint8_t* data,std::size_t size,
                                  std::array<MotionLimitRange,MaxMotionProfileResources>& out,std::size_t& count) {
    count=0;
    if(!data||size<3||data[0]!=2||data[1]!=0||!data[2]||data[2]>out.size()||size!=3u+28u*data[2])return false;
    auto candidate=out;
    for(std::size_t i=0;i<data[2];++i){const auto* p=data+3+28*i;auto& v=candidate[i];
        v.endpoint=u16(p);v.resource=u16(p+2);v.vmax=i32(p+4);v.amax=i32(p+8);
        v.min_vmax=i32(p+12);v.max_vmax=i32(p+16);v.min_amax=i32(p+20);v.max_amax=i32(p+24);
        if(v.min_vmax<=0||v.min_amax<=0||v.vmax<v.min_vmax||v.vmax>v.max_vmax||v.amax<v.min_amax||v.amax>v.max_amax)return false;
        for(std::size_t j=0;j<i;++j)if(v.endpoint==candidate[j].endpoint&&v.resource==candidate[j].resource)return false;
    }out=candidate;count=data[2];return true;
}
} // namespace mobocom
