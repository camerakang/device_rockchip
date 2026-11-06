#include "mobocom_device.hpp"
#include "mobocom_timecode.hpp"
#include "vectors.hpp"
#include "device_type_vectors.hpp"
#include "timeout_recovery_vectors.hpp"
#include "../examples/virtual_driver.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <iostream>
#include <vector>
#include <string>
using namespace mobocom;

using namespace mobocom::examples;
std::vector<std::uint8_t> unhex(std::string_view s){std::vector<std::uint8_t> v;auto digit=[](char c){return unsigned(c<='9'?c-'0':c-'a'+10);};for(std::size_t i=0;i<s.size();i+=2)v.push_back(std::uint8_t(digit(s[i])*16+digit(s[i+1])));return v;}
std::string hex(const std::uint8_t* p,std::size_t n){std::string s;for(std::size_t i=0;i<n;++i){s+="0123456789abcdef"[p[i]>>4];s+="0123456789abcdef"[p[i]&15];}return s;}
void golden_trace(){
    auto p1=profile(1),p2=profile(2),p3=profile(3);VirtualDriver d1,d2,d3,d4;
    DeviceCore a(p1,1,10,d1.hooks()),b(p2,2,20,d2.hooks()),c(p3,3,30,d3.hooks()),extra(p2,0x9007000000000004ULL,40,d4.hooks());DeviceCore* nodes[]={nullptr,&a,&b,&c,&extra};
    assert(a.valid()&&b.valid()&&c.valid()&&extra.valid());unsigned count=0,silent=0;
    for(const auto& s:golden_steps){auto& n=*nodes[s.node];assert(n.uid()==s.uid);if(s.tick){n.tick(s.at);continue;}auto raw=unhex(s.request);
        // Empty reply: the device must stay silent for this datagram (multicast scope, wrong UID, etc.).
        if(s.reply.empty()){assert(!n.receive(raw.data(),raw.size(),s.at));++silent;continue;}
        assert(n.receive(raw.data(),raw.size(),s.at));
        std::array<std::uint8_t,MaxFrame> out{};std::size_t size=0;assert(n.take_reply(s.at,out.data(),out.size(),size));
        if(hex(out.data(),size)!=s.reply){std::cerr<<"Mismatch node="<<s.node<<" step="<<count<<"\nexpected "<<s.reply<<"\nactual   "<<hex(out.data(),size)<<'\n';std::abort();}++count;
    }
    assert(silent==4); // Faulted DISCOVER now replies; only invalid scope and multicast ESTOP stay silent.
    std::cout<<"PASS C++/Python golden replies: "<<count<<", silent datagrams: "<<silent<<"; sizeof(DeviceCore)="<<sizeof(DeviceCore)<<" bytes\n";
}
void timeout_recovery(){
    auto p=profile(1);p.auto_reset_mask=6;VirtualDriver da,db;
    DeviceCore a(p,1,10,da.hooks()),b(p,1,10,db.hooks());DeviceCore* nodes[]={&a,&b};
    for(const auto& s:recovery_steps){auto& n=*nodes[s.scenario];
        if(s.op=="tick"){n.tick(s.at);continue;}
        if(s.op=="recover"){assert(n.recover_timeout_fault(s.at));assert(!n.recover_timeout_fault(s.at));continue;}
        auto raw=unhex(s.request);assert(n.receive(raw.data(),raw.size(),s.at));
        std::array<std::uint8_t,MaxFrame> out{};std::size_t size=0;
        assert(n.take_reply(s.at+20,out.data(),out.size(),size));assert(hex(out.data(),size)==s.reply);
    }
    for(auto* n:nodes){assert(n->state()==State::Safe&&n->source()==Source::None&&!n->owner());assert(!n->accepted_sequence()&&!n->applied_sequence());}
    for(auto fault:{Fault::Driver,Fault::Estop,Fault::LocalClock,Fault::OutputGap,Fault::StopTimeout,Fault::SwitchTimeout,Fault::ActionTimeout,Fault::LimitTrip}){
        VirtualDriver d;DeviceCore n(p,1,10,d.hooks());
        assert(n.select_local_source(Source::Remote,1,1,100000,300000,0)==Error::Ok);n.tick(0);
        d.safe_delay=10000;n.fault_input(1,Fault::TargetExpired);assert(!n.recover_timeout_fault(1));
        n.fault_input(2,fault);n.tick(10002);assert(n.fault()==fault);assert(!n.recover_timeout_fault(10002));
    }
    VirtualDriver d;DeviceCore n(p,1,10,d.hooks());n.fault_input(0,Fault::SourceTimeout);
    d.reset_ok=false;assert(!n.recover_timeout_fault(1));d.reset_ok=true;assert(n.recover_timeout_fault(1));
    p.auto_reset_mask=0;VirtualDriver off;DeviceCore disabled(p,2,20,off.hooks());disabled.fault_input(0,Fault::TargetExpired);assert(!disabled.recover_timeout_fault(1));
    std::cout<<"PASS local timeout recovery vectors, no restart/replay, hardware fault dominance\n";
}
void codec_datagram_timecode(){
    const std::uint8_t digits[]={'1','2','3','4','5','6','7','8','9'};assert(crc16(digits,9)==0x29b1);
    // 与独立逐位算法对照所有帧长度和多种字节模式，验证优化不改变CRC。
    for(unsigned seed=0;seed<8;++seed){std::array<std::uint8_t,MaxFrame> bytes{};for(unsigned i=0;i<MaxFrame;++i)bytes[i]=std::uint8_t(i*37+seed*53);
        for(unsigned length=0;length<=MaxFrame;++length){std::uint16_t expected=0xffff;for(unsigned i=0;i<length;++i){expected^=std::uint16_t(bytes[i])<<8;for(unsigned bit=0;bit<8;++bit)expected=std::uint16_t((expected<<1)^((expected&0x8000)?0x1021:0));}assert(crc16(bytes.data(),length)==expected);}}
    // One datagram is exactly one frame: truncated, padded or concatenated datagrams are rejected.
    auto raw=unhex(golden_steps[0].request);Message m{};assert(decode(raw.data(),raw.size(),m)&&m.dst_uid==1&&!m.src_uid);
    auto padded=raw;padded.push_back(0);assert(!decode(padded.data(),padded.size(),m));assert(!decode(raw.data(),raw.size()-1,m));
    auto twice=raw;twice.insert(twice.end(),raw.begin(),raw.end());assert(!decode(twice.data(),twice.size(),m));
    // UIDs above 2^53 survive the 64-bit header fields unchanged.
    std::array<std::uint8_t,MaxFrame> big{};Message wide{Kind::Response,0xfedcba9876543211ULL,AllNodes,3,Command::Info,5,nullptr,0};auto n=encode(big.data(),wide);
    assert(n==HeaderSize+2&&decode(big.data(),n,m)&&m.src_uid==0xfedcba9876543211ULL&&m.dst_uid==AllNodes);
    namespace tc=mobocom::timecode;auto bytes=unhex(golden_timecode);tc::Packet p{};assert(tc::decode(bytes.data(),bytes.size(),p));std::uint8_t out[26]{};assert(tc::encode(out,p));assert(hex(out,26)==golden_timecode);
    tc::HostTimeline h;assert(h.observe({1,0,1000000},0,260));std::uint64_t time;assert(h.start(1000,time)&&time==1001000);assert(h.now(40000,time)&&time==1040000&&h.fallback());assert(h.observe({2,0,500},40000,40260));assert(h.now(41000,time)&&time==1041000);h.stop();assert(h.start(41000,time)&&time==1500&&!h.fallback());
}
void safety_boundaries(){
    auto p=profile(1);VirtualDriver d;DeviceCore n(p,1,10,d.hooks());LocalControlInput local(n);assert(n.select_local_source(Source::Remote,n.session_epoch(),1,100000,300000,0)==Error::Ok);n.tick(0);
    Target v{1,1,100};assert(local.targets(n.session_epoch(),n.generation(),1,&v,1,0)==Error::Ok);v.value=200;assert(local.targets(n.session_epoch(),n.generation(),2,&v,1,1)==Error::Ok);assert(d.apply_calls==0);n.tick(1);assert(d.outputs[5]==200);
    for(std::uint64_t t=5000;t<=95000;t+=5000)n.tick(t);
    assert(local.heartbeat(n.session_epoch(),n.generation(),95000)==Error::Ok);n.tick(100001);assert(n.fault()==Fault::TargetExpired);n.tick(100002);assert(n.state()==State::Fault);
    assert(local.targets(n.session_epoch(),2,3,&v,1,100002)==Error::Generation);
    VirtualDriver d2;DeviceCore other(p,2,20,d2.hooks());assert(other.select_local_source(Source::Remote,other.session_epoch(),1,100000,300000,0)==Error::Ok);other.tick(0);assert(other.state()==State::Active);
    n.fault_input(100003);assert(other.state()==State::Active);
    VirtualDriver late;late.safe_delay=200000;DeviceCore delayed(p,3,30,late.hooks());assert(delayed.select_local_source(Source::Remote,delayed.session_epoch(),1,100000,300000,0)==Error::Ok);delayed.tick(100000);assert(delayed.fault()==Fault::SwitchTimeout);
    VirtualDriver coupled;coupled.coupled_limit=150;DeviceCore guarded(p,4,40,coupled.hooks());
    assert(guarded.select_local_source(Source::Remote,guarded.session_epoch(),1,100000,300000,0)==Error::Ok);guarded.tick(0);
    Target first{1,1,100},second{1,2,100};
    assert(guarded.local_targets(guarded.session_epoch(),guarded.generation(),1,&first,1,0)==Error::Ok);
    assert(guarded.local_targets(guarded.session_epoch(),guarded.generation(),2,&second,1,0)==Error::Range);
    assert(guarded.local_heartbeat(guarded.session_epoch(),guarded.generation(),10001)==Error::Generation);
    assert(guarded.fault()==Fault::OutputGap&&coupled.apply_calls==0);
    // Reply window: available immediately, discarded after 20 ms, replaced by every new datagram.
    std::array<std::uint8_t,MaxFrame> response{},held{};std::size_t size=0,held_size=0;auto raw=unhex(golden_steps[0].request);
    VirtualDriver rd;DeviceCore reply(p,1,10,rd.hooks());
    assert(reply.receive(raw.data(),raw.size(),0));assert(!reply.take_reply(20001,response.data(),response.size(),size));
    assert(reply.receive(raw.data(),raw.size(),30000));assert(reply.take_reply(30000,held.data(),held.size(),held_size));
    assert(!reply.take_reply(30001,response.data(),response.size(),size));
    assert(reply.receive(raw.data(),raw.size(),30100));auto bad=raw;bad.back()^=1;assert(!reply.receive(bad.data(),bad.size(),30101));
    assert(!reply.take_reply(30101,response.data(),response.size(),size));
    // Multicast SESSION_OPEN is dropped; the same payload opens the session only by UID unicast.
    std::array<std::uint8_t,MaxFrame> request{};Message f{};
    std::uint8_t open[20]{};put64(open,10);put64(open+8,reply.session_epoch());put32(open+16,reply.generation());
    Message multicast{Kind::Request,0,AllNodes,1,Command::SessionOpen,77,open,20};auto bytes=encode(request.data(),multicast);
    const auto epoch=reply.session_epoch();assert(!reply.receive(request.data(),bytes,30200)&&reply.session_epoch()==epoch);
    multicast.dst_uid=reply.uid();bytes=encode(request.data(),multicast);
    assert(reply.receive(request.data(),bytes,30300)&&reply.take_reply(30300,response.data(),response.size(),size));
    assert(decode(response.data(),size,f)&&f.kind==Kind::Ack&&reply.session_epoch()==epoch+1);
    // Removed ASSIGN_ID (3) is now an unknown unicast command.
    std::uint8_t g[20]{};put64(g,10);put32(g+8,reply.generation());put64(g+12,reply.session_epoch());
    Message legacy{Kind::Request,0,reply.uid(),2,Command(3),77,g,20};bytes=encode(request.data(),legacy);
    assert(reply.receive(request.data(),bytes,30400)&&reply.take_reply(30400,response.data(),response.size(),size));
    assert(decode(response.data(),size,f)&&f.kind==Kind::Error&&u16(f.payload)==std::uint16_t(Error::Unsupported));
    std::cout<<"PASS local latest targets, watchdog, switch timeout, independent nodes, datagram reply window, all-state discovery, multicast scope\n";
}
void registered_device_types(){
    // 编译期固定正式ID，防止生成文件与登记表一起被误改后仍靠一致性测试通过。
    static_assert(device_type_id(DeviceType::Fan)==0x9001);
    static_assert(device_type_id(DeviceType::FocusMotor)==0x9002);
    static_assert(device_type_id(DeviceType::Treadmill)==0x9003);
    static_assert(device_type_id(DeviceType::StairClimber)==0x9004);
    static_assert(device_type_id(DeviceType::MechanicalHorse)==0x9005);
    static_assert(device_type_id(DeviceType::RailCar)==0x9006);
    static_assert(device_type_id(DeviceType::Gimbal)==0x9007);
    static_assert(device_type_id(DeviceType::CutPoint)==0x9008);
    unsigned count=0;
    for(const auto& v:device_type_vectors){
        // 复用虚拟资源，仅验证INFO/DISCOVER/CAPS类型字段的逐字节编码。
        auto p=profile(1);p.type=v.type;VirtualDriver d;DeviceCore n(p,2,10,d.hooks());assert(n.valid());
        assert(is_registered_device_type(v.type));assert(is_virtual_device_type(v.type)!=is_production_device_type(v.type));
        auto raw=unhex(v.request);assert(n.receive(raw.data(),raw.size(),0));
        std::array<std::uint8_t,MaxFrame> out{};std::size_t size=0;assert(n.take_reply(0,out.data(),out.size(),size));
        assert(hex(out.data(),size)==v.reply);++count;
    }
    for(std::uint16_t type:{0,0xffff,0x9000,0x900a,0x8fff,0xf001}){
        // 未登记Profile不能接收请求，也不能触发驱动或安全回调。
        assert(!is_registered_device_type(type));assert(!is_virtual_device_type(type));assert(!is_production_device_type(type));
        auto p=profile(1);p.type=type;VirtualDriver d;DeviceCore invalid(p,2,10,d.hooks());assert(!invalid.valid());
        auto raw=unhex(device_type_vectors[0].request);assert(!invalid.receive(raw.data(),raw.size(),0));assert(d.apply_calls==0&&d.safe_calls==0);
    }
    std::cout<<"PASS registered types: "<<count<<" identity golden replies, reserved ID rejection\n";
}
void discovery_all_states(){
    auto p=profile(1);VirtualDriver d;DeviceCore n(p,0x9006000000000001ULL,10,d.hooks());
    std::array<std::uint8_t,MaxFrame> raw{},out{};std::size_t size=0;
    auto probe=[&](std::uint64_t now,State expected){
        n.tick(now,false);assert(n.state()==expected);
        auto query=[&](){
            std::uint8_t cursor[2]{};Message status{Kind::Request,0,n.uid(),1,Command::Status,999,cursor,2};
            auto bytes=encode(raw.data(),status);assert(n.receive(raw.data(),bytes,now));assert(n.take_reply(now,out.data(),out.size(),size));
            return std::vector<std::uint8_t>(out.begin(),out.begin()+size);
        };
        const auto before=query();const auto outputs=d.outputs;
        const auto applied=d.apply_calls,safe=d.safe_calls,flush=d.flush_calls,actions=d.action_calls;
        Message discover{Kind::Request,0,AllNodes,2,Command::Discover,999,nullptr,0};
        auto bytes=encode(raw.data(),discover);assert(n.receive(raw.data(),bytes,now));assert(n.take_reply(now,out.data(),out.size(),size));
        Message reply{};assert(decode(out.data(),size,reply)&&reply.src_uid==n.uid()&&reply.length==26&&reply.kind==Kind::Response);
        assert(query()==before&&d.outputs==outputs&&d.apply_calls==applied&&d.safe_calls==safe&&d.flush_calls==flush&&d.action_calls==actions);
    };
    probe(0,State::Safe);d.safe_delay=100;
    assert(n.select_local_source(Source::Remote,n.session_epoch(),n.generation(),100000,300000,0)==Error::Ok);
    probe(0,State::Switching);n.tick(100);
    Target target{1,1,500};assert(n.local_targets(n.session_epoch(),n.generation(),1,&target,1,100)==Error::Ok);n.tick(100);
    probe(100,State::Active);n.fault_input(100,Fault::Driver);probe(100,State::Stopping);probe(200,State::Fault);
    assert(n.fault()==Fault::Driver);
    std::cout<<"PASS discovery in all five states without control/output side effects\n";
}
void session_pressure(){
    auto p=profile(3);VirtualDriver d;DeviceCore n(p,2,10,d.hooks());
    std::array<std::uint8_t,MaxFrame> raw{},out{},duplicate{};std::array<std::uint8_t,20> payload{};
    for(std::uint64_t sid=1;sid<=10000;++sid){
        put64(payload.data(),10);put64(payload.data()+8,n.session_epoch());put32(payload.data()+16,n.generation());
        Message m{Kind::Request,0,2,1,Command::SessionOpen,sid,payload.data(),20};auto bytes=encode(raw.data(),m);auto at=sid*3000;
        assert(n.receive(raw.data(),bytes,at));std::size_t size=0;assert(n.take_reply(at+20,out.data(),out.size(),size));Message f{};assert(decode(out.data(),size,f)&&f.kind==Kind::Ack&&u64(f.payload+32)==sid+1);
        assert(n.receive(raw.data(),bytes,at+100));std::size_t repeated=0;assert(n.take_reply(at+120,duplicate.data(),duplicate.size(),repeated));assert(size==repeated&&!std::memcmp(out.data(),duplicate.data(),size));
        m={Kind::Request,0,2,1,Command::Info,sid+100000,nullptr,0};bytes=encode(raw.data(),m);assert(n.receive(raw.data(),bytes,at+200));assert(n.take_reply(at+220,out.data(),out.size(),size));assert(decode(out.data(),size,f)&&f.kind==Kind::Response&&u64(f.payload)==2);
    }
    assert(n.boot_id()==10&&n.state()==State::Safe&&n.fault()==Fault::None);
    std::cout<<"PASS 10000 session replacements/duplicate opens and stateless queries without reboot\n";
}
void limit_trip_fault(){
    // 物理限位触发：独立于通用 Driver 的公共故障码，锁存首个原因。
    auto p=profile(1);VirtualDriver d;DeviceCore n(p,1,10,d.hooks());assert(n.select_local_source(Source::Remote,n.session_epoch(),1,100000,300000,0)==Error::Ok);n.tick(0);
    static_assert(static_cast<std::uint16_t>(Fault::LimitTrip)==11);
    n.fault_input(1,Fault::LimitTrip);n.tick(2);assert(n.fault()==Fault::LimitTrip&&n.state()==State::Fault);
    n.fault_input(3,Fault::Driver);assert(n.fault()==Fault::LimitTrip);
}
int main(){timeout_recovery();limit_trip_fault();session_pressure();golden_trace();codec_datagram_timecode();safety_boundaries();registered_device_types();discovery_all_states();}
