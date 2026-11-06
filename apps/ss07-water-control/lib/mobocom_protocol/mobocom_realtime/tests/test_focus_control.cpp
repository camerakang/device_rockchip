#include "mobocom_device.hpp"
#include "../examples/virtual_driver.hpp"
#include "focus_control_vectors.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <iostream>
#include <vector>
#include <string>
using namespace mobocom;
struct FocusFixture {
    ConfigurationSnapshot snapshot{}; unsigned writes=0; bool busy=false;
    FocusFixture(){snapshot.revision=1;snapshot.length=encode_focus_control(snapshot.values.data(),{FocusControlSource::Wireless,true,true,true,0});}
    ConfigurationHooks hooks(){
        ConfigurationHooks h;h.context=this;
        h.busy=[](void* p){return static_cast<FocusFixture*>(p)->busy;};
        h.read=[](void* p,std::uint16_t schema,ConfigurationSnapshot& out){if(schema!=4)return Error::Unsupported;out=static_cast<FocusFixture*>(p)->snapshot;return Error::Ok;};
        h.write=[](void* p,std::uint16_t schema,std::uint32_t revision,const std::uint8_t* data,std::size_t size,std::uint64_t session,std::uint32_t sequence,std::uint64_t){
            auto& c=*static_cast<FocusFixture*>(p);if(schema!=4)return Error::Unsupported;
            FocusControlSource source;auto e=decode_focus_source(data,size,source);if(e!=Error::Ok)return e;
            if(revision!=c.snapshot.revision)return Error::Conflict;if(revision==0xffffffffU)return Error::Space;
            ++c.writes;++c.snapshot.revision;c.snapshot.state=ConfigurationState::Applied;c.snapshot.request_session=session;c.snapshot.request_sequence=sequence;
            c.snapshot.length=encode_focus_control(c.snapshot.values.data(),{source,true,true,true,0});return Error::Ok;
        };return h;
    }
};
std::vector<std::uint8_t> unhex(std::string_view s){
    std::vector<std::uint8_t> v;for(std::size_t i=0;i<s.size();i+=2)v.push_back(std::stoul(std::string(s.substr(i,2)),nullptr,16));return v;
}
int main(){
    FocusCommand cmd;
    for (auto hex : {"1001000000000000","100700000080ffff","100400000000ffff","1100","1101"}) {
        auto bytes=unhex(hex);assert(decode_focus_command(bytes.data(),bytes.size(),cmd)==Error::Ok);
        if(bytes.size()==8){assert(cmd.mask==bytes[1]);for(unsigned i=0;i<3;++i)assert(cmd.positions[i]==u16(bytes.data()+2+i*2));}
    }
    for(auto hex : {"","1000000000000000","1008000000000000","1001000001000000","1102","1200","10","110000"}) {
        auto bytes=unhex(hex);assert(decode_focus_command(bytes.data(),bytes.size(),cmd)!=Error::Ok);
    }
    std::uint8_t supported[8];FocusControlStatus capability;
    assert(encode_focus_control(supported,{FocusControlSource::Wired,true,true,true,0,true})==8);
    assert(decode_focus_control(supported,8,capability)&&capability.wired_commands);
    auto profile=examples::profile(2);examples::VirtualDriver driver;DeviceCore core(profile,2,20,driver.hooks());FocusFixture config;core.set_configuration_hooks(config.hooks());
    for(const auto& step:focus_control_vectors){
        const auto request=unhex(step.request),expected=unhex(step.reply);
        std::array<std::uint8_t,MaxFrame> response{};std::size_t length=0;
        assert(core.receive(request.data(),request.size(),step.at));assert(core.take_reply(step.at+20,response.data(),response.size(),length));
        assert(length==expected.size()&&std::equal(expected.begin(),expected.end(),response.begin()));
    }
    assert(config.writes==3&&core.generation()==1&&core.session_epoch()==1&&driver.safe_calls==0&&driver.flush_calls==0);
    FocusControlStatus status;
    assert(decode_focus_control(config.snapshot.values.data(),8,status)&&status.source==FocusControlSource::Wireless&&status.output_idle);
    assert(!decode_focus_control(config.snapshot.values.data(),7,status));
    config.snapshot.values[2]=1;assert(!decode_focus_control(config.snapshot.values.data(),8,status));
    config.snapshot.values[2]=0;config.snapshot.values[1]=16;assert(!decode_focus_control(config.snapshot.values.data(),8,status));
    // Runtime config remains independent of a live Remote source and its target stream.
    examples::VirtualDriver running_driver;DeviceCore running(profile,2,20,running_driver.hooks());FocusFixture live;running.set_configuration_hooks(live.hooks());
    assert(running.select_local_source(Source::Remote,1,1,100000,300000,1000)==Error::Ok);running.tick(1020);
    Target targets[]{{1,1,100},{1,2,200}};assert(running.local_targets(1,2,1,targets,2,1040)==Error::Ok);running.tick(1060);
    const auto flushes=running_driver.flush_calls,safe_calls=running_driver.safe_calls;
    std::uint8_t data[35]{};put64(data,2);put64(data+8,20);put32(data+16,2);put64(data+20,1);put16(data+28,4);put32(data+30,1);data[34]=1;
    std::array<std::uint8_t,MaxFrame> raw{},reply{};std::size_t size=0;
    Message m{Kind::Request,0,2,1,Command::WriteConfig,200,data,35};const auto n=encode(raw.data(),m);
    assert(running.receive(raw.data(),n,2000));assert(running.take_reply(2020,reply.data(),reply.size(),size));Message response;assert(decode(reply.data(),size,response)&&response.kind==Kind::Ack);
    assert(running.state()==State::Active&&running.source()==Source::Remote&&running.generation()==2&&running.session_epoch()==1);
    assert(running_driver.flush_calls==flushes&&running_driver.safe_calls==safe_calls&&running_driver.outputs[5]==100&&running_driver.outputs[6]==200);
    // A cached runtime write must not be acknowledged after STOP changes the public guard.
    running.fault_input(3000,Fault::Estop);assert(running.receive(raw.data(),n,4000));assert(running.take_reply(4020,reply.data(),reply.size(),size));
    assert(decode(reply.data(),size,response)&&response.kind==Kind::Error&&u16(response.payload)==std::uint16_t(Error::Generation));assert(live.writes==1);
    std::cout<<"PASS focus control vectors, runtime isolation, guards and retry\n";
}
