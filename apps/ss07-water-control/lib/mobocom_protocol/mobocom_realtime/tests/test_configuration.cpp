#include "mobocom_device.hpp"
#include "../examples/virtual_driver.hpp"
#include "configuration_vectors.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <iostream>
#include <vector>
#include <string>
using namespace mobocom;
struct Configuration {
    ConfigurationSnapshot snapshot{};unsigned writes=0;
    Configuration(){snapshot.length=4;snapshot.values[0]=9;snapshot.values[1]=2;snapshot.values[2]=1;snapshot.values[3]=2;}
    ConfigurationHooks hooks(){
        ConfigurationHooks h;h.context=this;
        h.busy=[](void* p){return static_cast<Configuration*>(p)->snapshot.state==ConfigurationState::Pending;};
        h.read=[](void* p,std::uint16_t schema,ConfigurationSnapshot& s){if(schema!=1)return Error::Unsupported;s=static_cast<Configuration*>(p)->snapshot;return Error::Ok;};
        h.write=[](void* p,std::uint16_t schema,std::uint32_t revision,const std::uint8_t* data,std::size_t size,std::uint64_t session,std::uint32_t sequence,std::uint64_t){
            auto& c=*static_cast<Configuration*>(p);
            if(schema!=1)return Error::Unsupported;if(size!=2)return Error::Malformed;if(data[0]>15||data[1]>15)return Error::Range;
            if(revision!=c.snapshot.revision)return Error::Conflict;
            ++c.writes;++c.snapshot.revision;c.snapshot.state=ConfigurationState::Pending;c.snapshot.request_session=session;c.snapshot.request_sequence=sequence;return Error::Ok;
        };return h;
    }
};
std::vector<std::uint8_t> unhex(std::string_view s){
    std::vector<std::uint8_t> v;for(std::size_t i=0;i<s.size();i+=2)v.push_back(std::stoul(std::string(s.substr(i,2)),nullptr,16));return v;
}
int main(){
    auto profile=examples::profile(2);examples::VirtualDriver driver;DeviceCore core(profile,2,20,driver.hooks());Configuration config;core.set_configuration_hooks(config.hooks());core.fault_input(0,Fault::Driver);
    for(const auto& step:configuration_vectors){
        auto request=unhex(step.request),expected=unhex(step.reply);
        std::array<std::uint8_t,MaxFrame> response{};std::size_t length=0;
        assert(core.receive(request.data(),request.size(),step.at));assert(core.take_reply(step.at+20,response.data(),response.size(),length));
        assert(length==expected.size()&&std::equal(expected.begin(),expected.end(),response.begin()));
    }
    assert(config.writes==1&&core.source()==Source::None&&core.owner()==0&&core.fault()==Fault::Driver);
    assert(core.select_local_source(Source::Remote,1,1,100000,300000,20000)==Error::Busy);
    ConfigurationSnapshot decoded;std::uint16_t schema=0;std::array<std::uint8_t,MaxPayload> data{};
    auto size=encode_configuration(data.data(),1,config.snapshot);assert(size==27);
    assert(decode_configuration(data.data(),size,schema,decoded)&&schema==1&&decoded.revision==1&&decoded.request_sequence==2);
    assert(!decode_configuration(data.data(),size-1,schema,decoded));data[6]=4;assert(!decode_configuration(data.data(),size,schema,decoded));
    std::cout<<"PASS configuration cross-language vectors, retry, guards, no motion ownership\n";
}
