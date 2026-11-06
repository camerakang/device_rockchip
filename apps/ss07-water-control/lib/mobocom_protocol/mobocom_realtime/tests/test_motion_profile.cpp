#include "mobocom_device.hpp"
#include "../examples/virtual_driver.hpp"
#include "motion_profile_vectors.hpp"
#include "live_motion_profile_vectors.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <vector>
#include <string>
#include <iostream>
using namespace mobocom;
struct MemoryMotion {
    ConfigurationSnapshot snapshot{};std::array<MotionLimitRange,2> values{};unsigned writes=0;
    MemoryMotion(){for(unsigned i=0;i<2;++i){auto& v=values[i];v.endpoint=1;v.resource=i+1;v.vmax=v.max_vmax=50000;v.amax=v.max_amax=100000;v.min_vmax=v.min_amax=1000;}refresh();}
    void refresh(){snapshot.length=encode_motion_profile(snapshot.values.data(),values.data(),values.size());assert(snapshot.length==59);}
    ConfigurationHooks hooks(){ConfigurationHooks h;h.context=this;
        h.read=[](void* p,std::uint16_t schema,ConfigurationSnapshot& s){if(schema!=2&&schema!=3)return Error::Unsupported;s=static_cast<MemoryMotion*>(p)->snapshot;return Error::Ok;};
        h.write=[](void* p,std::uint16_t schema,std::uint32_t revision,const std::uint8_t* data,std::size_t size,std::uint64_t session,std::uint32_t sequence,std::uint64_t){
            auto& c=*static_cast<MemoryMotion*>(p);if(schema!=2&&schema!=3)return Error::Unsupported;
            std::array<MotionLimit,MaxMotionProfileResources> decoded{};std::size_t count;
            auto e=decode_motion_limits(data,size,decoded,count);if(e!=Error::Ok)return e;
            if(count!=2)return Error::Group;
            if(revision!=c.snapshot.revision)return Error::Conflict;
            if(revision==0xffffffffu)return Error::Space;
            auto candidate=c.values;
            for(std::size_t i=0;i<count;++i){const auto& v=decoded[i];if(v.endpoint!=1||v.resource<1||v.resource>2)return Error::Group;
                auto& r=candidate[v.resource-1];if(v.vmax<r.min_vmax||v.vmax>r.max_vmax||v.amax<r.min_amax||v.amax>r.max_amax)return Error::Range;
                r.vmax=v.vmax;r.amax=v.amax;
            }
            c.values=candidate;++c.writes;++c.snapshot.revision;c.snapshot.state=ConfigurationState::Applied;
            c.snapshot.request_session=session;c.snapshot.request_sequence=sequence;c.refresh();return Error::Ok;
        };return h;}
};
std::vector<std::uint8_t> unhex(std::string_view s){std::vector<std::uint8_t> v;for(std::size_t i=0;i<s.size();i+=2)v.push_back(std::stoul(std::string(s.substr(i,2)),nullptr,16));return v;}
int main(){
    {
        examples::VirtualDriver driver;DeviceCore core(examples::profile(2),2,20,driver.hooks());MemoryMotion config;core.set_configuration_hooks(config.hooks());
        for(const auto& row:live_motion_profile_vectors){auto request=unhex(row.request),expected=unhex(row.reply);std::array<std::uint8_t,MaxFrame> reply{};std::size_t length=0;
            core.tick(row.at-1);
            assert(core.receive(request.data(),request.size(),row.at));assert(core.take_reply(row.at+20,reply.data(),reply.size(),length));
            assert(length==expected.size()&&std::equal(expected.begin(),expected.end(),reply.begin()));
        }
        assert(config.writes==1);
    }
    examples::VirtualDriver driver;DeviceCore core(examples::profile(2),2,20,driver.hooks());MemoryMotion config;core.set_configuration_hooks(config.hooks());
    for(const auto& row:motion_profile_vectors){auto request=unhex(row.request),expected=unhex(row.reply);std::array<std::uint8_t,MaxFrame> reply{};std::size_t length=0;
        assert(core.receive(request.data(),request.size(),row.at));assert(core.take_reply(row.at+20,reply.data(),reply.size(),length));
        assert(length==expected.size()&&std::equal(expected.begin(),expected.end(),reply.begin()));
    }
    assert(config.writes==1&&config.values[0].vmax==25000&&config.values[1].vmax==25000&&driver.apply_calls==0);
    std::array<MotionLimitRange,MaxMotionProfileResources> ranges{};std::size_t count;
    assert(decode_motion_profile(config.snapshot.values.data(),config.snapshot.length,ranges,count)&&count==2);
    assert(!decode_motion_profile(config.snapshot.values.data(),config.snapshot.length-1,ranges,count));
    auto invalid=config.snapshot.values;invalid[1]=1;assert(!decode_motion_profile(invalid.data(),config.snapshot.length,ranges,count));
    std::array<std::uint8_t,100> data{};MotionLimit values[]={{1,1,25000,50000},{1,2,25000,50000}};
    auto size=encode_motion_limits(data.data(),values,2);assert(size==27);
    std::array<MotionLimit,MaxMotionProfileResources> limits{};assert(decode_motion_limits(data.data(),size,limits,count)==Error::Ok&&count==2);
    put16(data.data()+17,1);assert(decode_motion_limits(data.data(),size,limits,count)==Error::Group);
    values[0].vmax=0;assert(encode_motion_limits(data.data(),values,2)==0);
    std::cout<<"PASS motion profile vectors, atomic failure, replay, safety, codec\n";
}
