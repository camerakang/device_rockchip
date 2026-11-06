#pragma once
#include "mobocom_wire.hpp"
#include "mobocom_device_types.hpp"
#include "mobocom_configuration.hpp"
#include "mobocom_motion_profile.hpp"
#include "mobocom_focus_control.hpp"
#include <limits>

namespace mobocom {
enum class Source:std::uint8_t {None=0,Remote=1,Mobocom=2};
enum class State:std::uint8_t {Safe=0,Switching=1,Active=2,Stopping=3,Fault=4};
enum class Fault:std::uint16_t {None=0,SourceTimeout=1,TargetExpired=2,ActionTimeout=3,Driver=4,StopTimeout=5,SwitchTimeout=6,Estop=7,LocalClock=8,OutputGap=9,GenerationExhausted=10,LimitTrip=11};
enum class Outcome:std::uint8_t {Running=1,Success=2,Failed=3,Cancelled=4};
struct ResourceSpec {
    std::uint16_t endpoint,resource,unit,flags;
    std::int32_t minimum,maximum;
    std::uint16_t group=0;
    std::int32_t vmax=0,amax=0;
};
struct ActionSpec {std::uint16_t opcode;std::uint32_t resource_mask;std::uint16_t parameter_bytes;std::uint32_t timeout_us;};
struct DeviceProfile {
    // type用device_type_id(DeviceType::...)；version表示类别契约版本，firmware表示固件版本。
    // 类型不代替UID或资源单位；具体轴数、范围和安全行为仍由下方能力及Hooks定义。
    std::uint16_t type,version;std::uint32_t firmware;
    const ResourceSpec* resources;std::uint16_t resource_count;
    const ActionSpec* actions=nullptr;std::uint16_t action_count=0;
    std::uint8_t source_mask=6;std::uint16_t max_target_hz=0;
    std::uint32_t update_gap_us=0,target_min_us=0,target_max_us=0,lease_min_us=0,lease_max_us=0,safe_timeout_us=0,switch_timeout_us=0;
    std::uint32_t auto_reset_mask=0;
};
struct Target {std::uint16_t endpoint,resource;std::int32_t value;};
struct DriverHooks {
    void* context=nullptr;
    bool (*validate_targets)(void*,const Target*,std::size_t)=nullptr;
    bool (*apply_targets)(void*,const Target*,std::size_t,std::uint64_t)=nullptr;
    bool (*start_safe)(void*,Fault,std::uint64_t)=nullptr;
    bool (*safe_confirmed)(void*,std::uint64_t)=nullptr;
    bool (*reset_allowed)(void*)=nullptr;
    void (*flush_remote)(void*)=nullptr;
    bool (*validate_action)(void*,const ActionSpec&,const std::uint8_t*,std::size_t)=nullptr;
    bool (*start_action)(void*,const ActionSpec&,const std::uint8_t*,std::size_t,std::uint64_t,std::uint64_t)=nullptr;
    Outcome (*poll_action)(void*,std::uint64_t,std::uint64_t)=nullptr;
    void (*cancel_action)(void*,std::uint64_t)=nullptr;
    bool (*feedback)(void*,std::uint16_t,std::uint16_t,std::int32_t&)=nullptr;
};

// Single task owns all methods. ISR/NRF24 callbacks enqueue into that owner_.
// 固定存储：当前会话、建立会话及安全恢复缓存；只读陌生会话不分配记录。
class DeviceCore {
    static constexpr std::size_t MaxResources=16,History=8;
    // A reply not taken by the network adapter within this window is discarded; the host retries identical bytes.
    static constexpr std::uint64_t ReplyHoldUs=20000;
    struct Session {std::uint64_t id=0;std::uint32_t sequence=0;std::uint16_t request_size=0,reply_size=0;std::array<std::uint8_t,MaxFrame> request{},reply{};};
    struct Result {std::uint64_t id=0;Outcome outcome=Outcome::Running;std::uint16_t error=0;std::uint8_t progress=0;};
    const DeviceProfile& p_;DriverHooks h_;bool valid_=false;
    Session session_{},opening_{},safety_{},configuration_cache_{};
    ConfigurationHooks configuration_{};
    bool configuration_busy()const{return configuration_.busy&&configuration_.busy(configuration_.context);}
    std::uint64_t epoch_=1;
    std::array<Result,History> results_{};std::size_t result_cursor_=0;
    std::array<std::int32_t,MaxResources> values_{};
    std::array<std::uint64_t,MaxResources> received_{};
    std::uint16_t known_=0,pending_=0,active_=0;
    std::uint64_t last_now_=0,last_output_service_=0,lease_until_=0,transition_start_=0,next_operation_=1,operation_started_=0;
    bool output_clock_started_=false,operation_exhausted_=false;
    std::uint32_t target_ttl_=0,lease_ttl_=0,local_sequence_=0;
    Source next_source_=Source::None;std::uint64_t next_owner_=0;
    const ActionSpec* operation_spec_=nullptr;
    std::array<std::uint8_t,MaxFrame> reply_{};std::size_t reply_size_=0;
    std::uint64_t reply_at_=0,reply_until_=0;
    bool safe_confirmed(std::uint64_t now)const{return h_.safe_confirmed&&h_.safe_confirmed(h_.context,now);}
    void changed(){++state_version_;}
    bool advance_generation(){
        if(generation_==0xffffffffu)return false;
        ++generation_;local_sequence_=0;return true;
    }
    Result* result(std::uint64_t id){for(auto& r:results_)if(r.id==id&&id)return &r;return nullptr;}
    void clear(){
        pending_=active_=0;known_=0;values_.fill(0);received_.fill(0);output_clock_started_=false;
        if(operation_){if(h_.cancel_action)h_.cancel_action(h_.context,operation_);if(auto r=result(operation_)){r->outcome=Outcome::Cancelled;r->error=0;r->progress=0;}operation_=0;operation_spec_=nullptr;}
        source_=Source::None;owner_=0;lease_until_=0;accepted_sequence_=applied_sequence_=0;
    }
    void stop_impl(std::uint64_t now,Fault reason){
        // 可恢复超时不得掩盖随后出现的硬件/调度故障；急停仍最高优先级。
        const bool timeout=fault_==Fault::SourceTimeout||fault_==Fault::TargetExpired;
        if(reason==Fault::Estop||(reason!=Fault::None&&(fault_==Fault::None||
            (timeout&&reason!=Fault::SourceTimeout&&reason!=Fault::TargetExpired))))fault_=reason;
        if(state_==State::Stopping||state_==State::Fault)return;
        if(state_==State::Safe){if(fault_!=Fault::None){state_=State::Fault;changed();}return;}
        clear();next_source_=Source::None;next_owner_=0;advance_generation();state_=State::Stopping;transition_start_=now;changed();
        if((!h_.start_safe||!h_.start_safe(h_.context,reason,now)))fault_=Fault::Driver;
    }
    Error guard(const std::uint8_t* data,std::size_t size)const{
        if(size<20)return Error::Malformed;
        return u64(data)!=boot_id_?Error::Boot:u64(data+12)!=epoch_?Error::Epoch:u32(data+8)!=generation_?Error::Generation:Error::Ok;
    }
    Error switch_source(Source desired,std::uint64_t session,std::uint32_t target,std::uint32_t lease,std::uint64_t now){
        if(configuration_busy())return Error::Busy;
        if(state_!=State::Safe&&state_!=State::Active)return Error::State;
        if(fault_!=Fault::None||generation_>=0xfffffffeu)return Error::Safety;
        if(unsigned(desired)>2||(desired!=Source::None&&!(p_.source_mask&(1u<<unsigned(desired)))))return Error::Capability;
        if(target<p_.target_min_us||target>p_.target_max_us||lease<p_.lease_min_us||lease>p_.lease_max_us)return Error::Range;
        clear();target_ttl_=target;lease_ttl_=lease;next_source_=desired;next_owner_=desired==Source::Mobocom?session:0;
        transition_start_=now;state_=State::Switching;changed();
        if(!h_.start_safe(h_.context,Fault::None,now))stop_impl(now,Fault::Driver);
        return Error::Ok;
    }
    Error targets(const Target* entries,std::size_t count,std::uint32_t sequence,std::uint64_t now){
        if(!count||count>p_.resource_count)return Error::Malformed;
        std::uint16_t mask=0;std::array<std::uint8_t,MaxResources> indices{};
        for(std::size_t k=0;k<count;++k){
            std::size_t i=0;for(;i<p_.resource_count;++i)if(entries[k].endpoint==p_.resources[i].endpoint&&entries[k].resource==p_.resources[i].resource)break;
            if(i==p_.resource_count)return Error::Capability;
            if(mask&(1u<<i))return Error::Malformed;
            const auto& r=p_.resources[i];if(entries[k].value<r.minimum||entries[k].value>r.maximum)return Error::Range;
            if(operation_spec_&&(operation_spec_->resource_mask&(1u<<i)))return Error::Busy;
            mask|=1u<<i;indices[k]=std::uint8_t(i);
        }
        for(std::size_t i=0;i<p_.resource_count;++i)if((mask&(1u<<i))&&p_.resources[i].group)
            for(std::size_t j=0;j<p_.resource_count;++j)if(p_.resources[j].group==p_.resources[i].group&&!(mask&(1u<<j)))return Error::Group;
        std::array<Target,MaxResources> candidate{};std::size_t candidate_count=0;
        for(std::size_t i=0;i<p_.resource_count;++i)if((known_|mask)&(1u<<i)){
            auto value=values_[i];for(std::size_t k=0;k<count;++k)if(indices[k]==i)value=entries[k].value;
            const auto& r=p_.resources[i];candidate[candidate_count++]={r.endpoint,r.resource,value};
        }
        if(!h_.validate_targets(h_.context,candidate.data(),candidate_count))return Error::Range;
        if(!output_clock_started_||!(pending_|active_)){last_output_service_=now;output_clock_started_=true;} // 从空闲（无待交付/持续目标）受理目标即重新建立交付期限；gap 只约束有目标待服务的区间
        for(std::size_t k=0;k<count;++k){auto i=indices[k];values_[i]=entries[k].value;received_[i]=now;known_|=1u<<i;pending_|=1u<<i;if(p_.resources[i].flags&1)active_|=1u<<i;}
        accepted_sequence_=sequence;lease_until_=now+lease_ttl_;changed();return Error::Ok;
    }
    Error action(std::uint16_t opcode,const std::uint8_t* data,std::size_t size,std::uint64_t now){
        const ActionSpec* a=nullptr;for(std::size_t i=0;i<p_.action_count;++i)if(p_.actions[i].opcode==opcode)a=p_.actions+i;
        if(!a)return Error::Capability;if(size!=a->parameter_bytes)return Error::Malformed;
        if(operation_||(a->resource_mask&(active_|pending_)))return Error::Busy;
        if(!h_.validate_action(h_.context,*a,data,size))return Error::Range;
        if(operation_exhausted_)return Error::Space;
        operation_=next_operation_;if(next_operation_==std::numeric_limits<std::uint64_t>::max())operation_exhausted_=true;else ++next_operation_;
        auto& r=results_[result_cursor_++%History];r={operation_,Outcome::Running,0,0};operation_started_=now;operation_spec_=a;
        if(!h_.start_action(h_.context,*a,data,size,operation_,now)){
            r.outcome=Outcome::Failed;r.error=std::uint16_t(Error::Safety);h_.cancel_action(h_.context,operation_);operation_=0;operation_spec_=nullptr;stop_impl(now,Fault::Driver);return Error::Safety;
        }
        lease_until_=now+lease_ttl_;changed();return Error::Ok;
    }
    std::size_t ack(std::uint8_t* d)const{
        put64(d,boot_id_);put32(d+8,generation_);d[12]=std::uint8_t(state_);d[13]=std::uint8_t(source_);put16(d+14,std::uint16_t(fault_));
        put32(d+16,accepted_sequence_);put32(d+20,applied_sequence_);put64(d+24,operation_);put64(d+32,epoch_);return 40;
    }
    Error command(const Message& m,std::uint64_t now,std::uint8_t* d,std::size_t& n,Kind& kind){
        const auto* b=m.payload;const auto len=m.length;kind=Kind::Ack;n=0;
        if(m.command==Command::ReadConfig){
            if(len!=2)return Error::Malformed;
            if(!configuration_.read)return Error::Unsupported;
            ConfigurationSnapshot snapshot{};auto e=configuration_.read(configuration_.context,u16(b),snapshot);
            if(e!=Error::Ok)return e;
            n=encode_configuration(d,u16(b),snapshot);if(!n)return Error::Driver;
            kind=Kind::Response;return Error::Ok;
        }
        if(m.command==Command::WriteConfig){
            if(len<34||u64(b)!=uid_)return Error::Malformed;
            auto e=guard(b+8,len-8);if(e!=Error::Ok)return e;
            if(!configuration_.write)return Error::Unsupported;
            if(configuration_busy())return Error::Busy;
            const bool live=u16(b+28)==LiveMotionProfileSchema;
            const bool focus=u16(b+28)==FocusControlSchema;
            if(live){
                if(m.session!=session_.id)return Error::Session;
                if(state_!=State::Active||source_!=Source::Mobocom||owner_!=m.session||fault_!=Fault::None)return Error::Owner;
                if(operation_)return Error::Busy;
            }else if(!focus&&((state_!=State::Safe&&state_!=State::Fault)||source_!=Source::None||owner_))return Error::State;
            const bool motion=u16(b+28)==MotionProfileSchema;
            if(motion&&(state_!=State::Safe||fault_!=Fault::None||!safe_confirmed(now)))return Error::State;
            if(motion&&generation_>=0xfffffffeu)return Error::Space;
            e=configuration_.write(configuration_.context,u16(b+28),u32(b+30),b+34,len-34,m.session,m.sequence,now);
            if(e==Error::Ok&&motion){clear();advance_generation();h_.flush_remote(h_.context);changed();}
            if(e==Error::Ok&&live)changed(); // No target/lease renewal or control-context change.
            if(e==Error::Ok)n=ack(d);return e;
        }
        if(m.command==Command::Hello||m.command==Command::Info){
            // uid,type,firmware,boot,wire_version,profile_version,epoch
            if(len)return Error::Malformed;put64(d,uid_);put16(d+8,p_.type);put32(d+10,p_.firmware);put64(d+14,boot_id_);d[22]=Version;put16(d+23,p_.version);put64(d+25,epoch_);n=33;kind=Kind::Response;return Error::Ok;
        }
        if(m.command==Command::Caps){
            if(len!=2)return Error::Malformed;const auto cursor=u16(b),total=std::uint16_t(1+p_.resource_count+p_.action_count);if(cursor>=total)return Error::Range;
            put16(d,cursor+1<total?cursor+1:0xffff);put16(d+2,total);auto* q=d+7;
            if(!cursor){d[4]=1;put16(q,p_.type);put16(q+2,p_.version);q[4]=p_.source_mask;put16(q+5,p_.max_target_hz);
                const std::uint32_t v[]={p_.update_gap_us,p_.target_min_us,p_.target_max_us,p_.lease_min_us,p_.lease_max_us,p_.safe_timeout_us,p_.switch_timeout_us};
                for(unsigned i=0;i<7;++i)put32(q+7+4*i,v[i]);put16(q+35,p_.resource_count);put16(q+37,p_.action_count);put32(q+39,p_.auto_reset_mask);n=43;
            }else if(cursor<=p_.resource_count){d[4]=2;const auto& r=p_.resources[cursor-1];put16(q,r.endpoint);put16(q+2,r.resource);put16(q+4,r.unit);put16(q+6,r.flags);put32(q+8,std::uint32_t(r.minimum));put32(q+12,std::uint32_t(r.maximum));put16(q+16,r.group);put32(q+18,std::uint32_t(r.vmax));put32(q+22,std::uint32_t(r.amax));n=26;
            }else{d[4]=3;const auto& a=p_.actions[cursor-1-p_.resource_count];put16(q,a.opcode);put32(q+2,a.resource_mask);put16(q+6,a.parameter_bytes);put32(q+8,a.timeout_us);n=12;}
            put16(d+5,std::uint16_t(n));n+=7;kind=Kind::Response;return Error::Ok;
        }
        if(m.command==Command::Status){
            if(len!=2)return Error::Malformed;auto cursor=u16(b);if(cursor>=p_.resource_count)return Error::Range;
            put64(d,boot_id_);put32(d+8,generation_);put32(d+12,state_version_);d[16]=std::uint8_t(state_);d[17]=std::uint8_t(source_);put16(d+18,std::uint16_t(fault_));put64(d+20,owner_);
            put32(d+28,accepted_sequence_);put32(d+32,applied_sequence_);put64(d+36,operation_);put32(d+44,lease_until_>now?std::uint32_t(lease_until_-now):0);put16(d+48,active_);put16(d+50,pending_);d[52]=safe_confirmed(now);
            put16(d+53,cursor+8<p_.resource_count?cursor+8:0xffff);const auto count=std::min<unsigned>(8,p_.resource_count-cursor);d[55]=std::uint8_t(count);put64(d+56,epoch_);n=64;
            for(unsigned k=0;k<count;++k){auto i=cursor+k;const auto& r=p_.resources[i];auto* q=d+n;put16(q,r.endpoint);put16(q+2,r.resource);put32(q+4,std::uint32_t(values_[i]));std::int32_t fb=0;bool valid=(r.flags&2)&&h_.feedback&&h_.feedback(h_.context,r.endpoint,r.resource,fb);put32(q+8,std::uint32_t(fb));put32(q+12,(known_&(1u<<i))?std::uint32_t(std::min<std::uint64_t>(0xffffffffu,now-received_[i])):0xffffffffu);q[16]=((known_&(1u<<i))?1:0)|(valid?2:0);n+=17;}
            kind=Kind::Response;return Error::Ok;
        }
        if(m.command==Command::ActionStatus){
            if(len!=8)return Error::Malformed;auto* r=result(u64(b));if(!r)return Error::NotFound;put64(d,r->id);d[8]=std::uint8_t(r->outcome);put16(d+9,r->error);d[11]=r->progress;n=12;kind=Kind::Response;return Error::Ok;
        }
        if(m.command==Command::Stop||m.command==Command::Estop){if(len!=20)return Error::Malformed;auto e=guard(b,len);if(e!=Error::Ok)return e;stop_impl(now,m.command==Command::Estop?Fault::Estop:Fault::None);n=ack(d);return Error::Ok;}
        if(m.command!=Command::SetSource&&m.command!=Command::ResetFault&&m.command!=Command::Heartbeat&&m.command!=Command::WriteTargets&&m.command!=Command::ExecuteAction)return Error::Unsupported;
        auto e=guard(b,len);if(e!=Error::Ok)return e;
        if(m.command==Command::SetSource){if(len!=29)return Error::Malformed;e=switch_source(Source(b[20]),m.session,u32(b+21),u32(b+25),now);}
        else if(m.command==Command::ResetFault){
            if(configuration_busy())return Error::Busy;
            if(len!=20)return Error::Malformed;if(state_!=State::Fault||!safe_confirmed(now)||!h_.reset_allowed(h_.context))return Error::Safety;
            advance_generation();fault_=Fault::None;state_=State::Safe;changed();
        }else{
            if(state_!=State::Active||source_!=Source::Mobocom||owner_!=m.session)return Error::Owner;
            if(m.command==Command::Heartbeat){if(len!=20)return Error::Malformed;lease_until_=now+lease_ttl_;}
            else if(m.command==Command::WriteTargets){
                if(len<21||len!=21u+8u*b[20]||b[20]>MaxResources)return Error::Malformed;
                std::array<Target,MaxResources> entries{};for(unsigned i=0;i<b[20];++i){auto q=b+21+8*i;entries[i]={u16(q),u16(q+2),i32(q+4)};}e=targets(entries.data(),b[20],m.sequence,now);
            }else{if(len<22)return Error::Malformed;e=action(u16(b+20),b+22,len-22,now);}
        }
        if(e==Error::Ok)n=ack(d);return e;
    }
private:
    std::uint64_t uid_,boot_id_,owner_=0,operation_=0;
    std::uint32_t generation_=1,state_version_=1,accepted_sequence_=0,applied_sequence_=0;
    State state_=State::Safe;Source source_=Source::None;Fault fault_=Fault::None;
public:
    std::uint64_t uid() const {return uid_;}
    std::uint64_t boot_id() const {return boot_id_;}
    std::uint64_t session_epoch() const {return epoch_;}
    std::uint64_t owner() const {return owner_;}
    std::uint64_t operation() const {return operation_;}
    std::uint32_t generation() const {return generation_;}
    std::uint32_t state_version() const {return state_version_;}
    std::uint32_t accepted_sequence() const {return accepted_sequence_;}
    std::uint32_t applied_sequence() const {return applied_sequence_;}
    State state() const {return state_;}
    Source source() const {return source_;}
    Fault fault() const {return fault_;}
    DeviceCore(const DeviceProfile& profile,std::uint64_t id,std::uint64_t boot,DriverHooks hooks):p_(profile),h_(hooks),uid_(id),boot_id_(boot){
        // 未登记类型令整个节点无效，避免设备适配器临时定义ID后发布到网络。
        // 网络版本只用非零UID寻址；已登记不等于硬件安全就绪，仍须通过全部资源、时序及必要回调检查。
        valid_=id&&boot&&is_registered_device_type(p_.type)&&p_.version&&p_.firmware&&p_.resources&&p_.resource_count&&p_.resource_count<=16&&p_.action_count<=16&&(!p_.action_count||p_.actions)&&p_.source_mask&&!(p_.source_mask&~6)&&p_.max_target_hz&&p_.update_gap_us&&p_.target_min_us&&p_.target_min_us<=p_.target_max_us&&p_.lease_min_us&&p_.lease_min_us<=p_.lease_max_us&&p_.safe_timeout_us&&p_.switch_timeout_us&&!(p_.auto_reset_mask&~6u)&&h_.validate_targets&&h_.apply_targets&&h_.start_safe&&h_.safe_confirmed&&h_.reset_allowed&&h_.flush_remote;
        for(unsigned i=0;valid_&&i<p_.resource_count;++i){const auto& r=p_.resources[i];if(!r.endpoint||!r.resource||!r.unit||r.minimum>r.maximum||(r.flags&~3)||((r.flags&2)&&!h_.feedback))valid_=false;for(unsigned j=0;j<i;++j)if(r.endpoint==p_.resources[j].endpoint&&r.resource==p_.resources[j].resource)valid_=false;}
        if(p_.action_count&&(!h_.validate_action||!h_.start_action||!h_.poll_action||!h_.cancel_action))valid_=false;
        for(unsigned i=0;valid_&&i<p_.action_count;++i){const auto& a=p_.actions[i];if(!a.opcode||!a.timeout_us||a.parameter_bytes>218||(a.resource_mask>>p_.resource_count))valid_=false;for(unsigned j=0;j<i;++j)if(a.opcode==p_.actions[j].opcode)valid_=false;}
    }
    DeviceCore(const DeviceCore&)=delete;DeviceCore& operator=(const DeviceCore&)=delete;
    bool valid()const{return valid_;}
    void set_configuration_hooks(ConfigurationHooks hooks){configuration_=hooks;}
    bool configuration_pending()const{return configuration_busy();}
    void tick(std::uint64_t now,bool apply_output=true){
        if(!valid_)return;if(now<last_now_){stop_impl(last_now_,Fault::LocalClock);return;}last_now_=now;
        if(state_==State::Switching){
            if(now-transition_start_>=p_.switch_timeout_us)stop_impl(now,Fault::SwitchTimeout);
            else if(safe_confirmed(now)){
                if(!advance_generation()){stop_impl(now,Fault::None);return;}source_=next_source_;owner_=next_owner_;state_=source_==Source::None?State::Safe:State::Active;lease_until_=source_==Source::None?0:now+lease_ttl_;h_.flush_remote(h_.context);changed();
            }
        }
        if(state_==State::Stopping){if(safe_confirmed(now)){state_=fault_==Fault::None?State::Safe:State::Fault;changed();}else if(now-transition_start_>=p_.safe_timeout_us){fault_=Fault::StopTimeout;state_=State::Fault;changed();}}
        if(state_!=State::Active)return;
        if(now>=lease_until_){stop_impl(now,Fault::SourceTimeout);return;}
        for(unsigned i=0;i<p_.resource_count;++i)if((active_&(1u<<i))&&now-received_[i]>=target_ttl_){stop_impl(now,Fault::TargetExpired);return;}
        if(operation_){
            auto* r=result(operation_);
            if(now-operation_started_>=operation_spec_->timeout_us){h_.cancel_action(h_.context,operation_);r->outcome=Outcome::Failed;r->error=std::uint16_t(Error::Timeout);operation_=0;operation_spec_=nullptr;stop_impl(now,Fault::ActionTimeout);return;}
            auto outcome=h_.poll_action(h_.context,operation_,now);if(outcome!=Outcome::Running){r->outcome=outcome;r->error=outcome==Outcome::Success?0:std::uint16_t(Error::Driver);r->progress=outcome==Outcome::Success?100:0;operation_=0;operation_spec_=nullptr;changed();if(outcome!=Outcome::Success){stop_impl(now,Fault::Driver);return;}}
        }
        if((active_||pending_)&&output_clock_started_&&now-last_output_service_>p_.update_gap_us){stop_impl(now,Fault::OutputGap);return;}
        if(apply_output&&(pending_||active_)){std::array<Target,MaxResources> entries{};std::size_t count=0;for(unsigned i=0;i<p_.resource_count;++i)if((pending_|active_)&(1u<<i)){const auto& r=p_.resources[i];entries[count++]={r.endpoint,r.resource,values_[i]};}
            if(!h_.apply_targets(h_.context,entries.data(),count,now)){stop_impl(now,Fault::Driver);return;}if(applied_sequence_!=accepted_sequence_)changed();applied_sequence_=accepted_sequence_;pending_=0;last_output_service_=now;output_clock_started_=true;
        }
    }
    void fault_input(std::uint64_t now,Fault reason=Fault::Driver){tick(now,false);stop_impl(now,reason);tick(now,false);}
    // 仅供设备所有者任务在tick及硬件故障处理之后调用；无报文、无队列、无控制权恢复。
    // opt-in：普通tick不自动清故障，类别还须允许对应auto_reset_mask位。
    bool recover_timeout_fault(std::uint64_t now){
        if(!valid_||now<last_now_||configuration_busy()||state_!=State::Fault||
           (fault_!=Fault::SourceTimeout&&fault_!=Fault::TargetExpired)||
           !(p_.auto_reset_mask&(1u<<unsigned(fault_)))||source_!=Source::None||owner_||
           active_||pending_||operation_||generation_==0xffffffffu||
           !safe_confirmed(now)||!h_.reset_allowed(h_.context))return false;
        last_now_=now;clear();next_source_=Source::None;next_owner_=0;
        advance_generation();fault_=Fault::None;state_=State::Safe;changed();return true;
    }
    Error select_local_source(Source desired,std::uint64_t expected_epoch,std::uint32_t expected,std::uint32_t target,std::uint32_t lease,std::uint64_t now){
        if(!valid_)return Error::Capability;tick(now,false);if(expected_epoch!=epoch_)return Error::Epoch;if(expected!=generation_)return Error::Generation;if(desired==Source::Mobocom)return Error::Owner;return switch_source(desired,0,target,lease,now);
    }
    Error local_targets(std::uint64_t expected_epoch,std::uint32_t expected,std::uint32_t seq,const Target* values,std::size_t count,std::uint64_t now){
        if(!valid_||(!values&&count))return Error::Capability;tick(now,false);if(expected_epoch!=epoch_)return Error::Epoch;if(expected!=generation_)return Error::Generation;
        if(state_!=State::Active||source_!=Source::Remote)return Error::Owner;if(!seq||seq<=local_sequence_)return Error::Sequence;auto e=targets(values,count,seq,now);if(e==Error::Ok)local_sequence_=seq;return e;
    }
    Error local_action(std::uint64_t expected_epoch,std::uint32_t expected,std::uint32_t seq,std::uint16_t opcode,const std::uint8_t* data,std::size_t size,std::uint64_t now){
        if(!valid_||(!data&&size))return Error::Capability;tick(now,false);if(expected_epoch!=epoch_)return Error::Epoch;if(expected!=generation_)return Error::Generation;if(state_!=State::Active||source_!=Source::Remote)return Error::Owner;if(!seq||seq<=local_sequence_)return Error::Sequence;auto e=action(opcode,data,size,now);if(e==Error::Ok)local_sequence_=seq;return e;
    }
    Error local_heartbeat(std::uint64_t expected_epoch,std::uint32_t expected,std::uint64_t now){if(!valid_)return Error::Capability;tick(now,false);if(expected_epoch!=epoch_)return Error::Epoch;if(expected!=generation_)return Error::Generation;if(state_!=State::Active||source_!=Source::Remote)return Error::Owner;lease_until_=now+lease_ttl_;return Error::Ok;}
    // One UDP datagram per call. Returns true only when a reply is queued for take_reply().
    // A new datagram discards any untaken older reply; retries with identical bytes hit the cache.
    bool receive(const std::uint8_t* raw,std::size_t size,std::uint64_t end){
        reply_size_=0;if(!valid_)return false;
        tick(end,false);if(end<last_now_)return false;
        Message m{};if(!decode(raw,size,m)||m.kind!=Kind::Request||m.src_uid)return false;
        std::array<std::uint8_t,MaxPayload> data{};std::size_t n=0;Kind kind=Kind::Ack;Error error=Error::Ok;
        auto cached_reply=[&](const Session& c){reply_=c.reply;reply_size_=c.reply_size;reply_at_=end;reply_until_=end+ReplyHoldUs;return true;};
        const bool read=m.command==Command::Hello||m.command==Command::Info||m.command==Command::Caps||m.command==Command::Status||m.command==Command::ActionStatus||m.command==Command::ReadConfig;
        const bool config=m.command==Command::WriteConfig;
        const bool live=config&&m.length>=34&&u16(m.payload+28)==LiveMotionProfileSchema;
        const bool focus=config&&m.length>=34&&u16(m.payload+28)==FocusControlSchema;
        const bool open=m.command==Command::SessionOpen;
        const bool safety=m.command==Command::Stop||m.command==Command::Estop||m.command==Command::ResetFault;
        if(m.dst_uid==AllNodes){
            // Multicast carries only unguarded all-node STOP/ESTOP (silent) and DISCOVER.
            if((m.command==Command::Stop||m.command==Command::Estop)&&!m.length){stop_impl(end,m.command==Command::Estop?Fault::Estop:Fault::None);return false;}
            if(m.command==Command::Discover&&!m.length){
                // type,firmware,boot,epoch,generation; the UID is src_uid in the header.
                put16(data.data(),p_.type);put32(data.data()+2,p_.firmware);put64(data.data()+6,boot_id_);put64(data.data()+14,epoch_);put32(data.data()+22,generation_);n=26;
                Message response{Kind::Response,uid_,AllNodes,m.sequence,m.command,m.session,data.data(),std::uint16_t(n)};reply_size_=encode(reply_.data(),response);reply_at_=end;reply_until_=end+ReplyHoldUs;return true;
            }
            return false;
        }
        if(m.dst_uid!=uid_||m.command==Command::Discover)return false;
        Session* cache=nullptr;
        if(open){
            if(m.length!=20)error=Error::Malformed;
            else if(u64(m.payload)!=boot_id_)error=Error::Boot;
            else if(opening_.id==m.session&&opening_.sequence==m.sequence){
                if(size==opening_.request_size&&!std::memcmp(raw,opening_.request.data(),size)&&session_.id==m.session)return cached_reply(opening_);
                error=Error::Conflict;
            }else if(u64(m.payload+8)!=epoch_)error=Error::Epoch;
            else if(u32(m.payload+16)!=generation_)error=Error::Generation;
            else if(configuration_busy())error=Error::Busy;
            else if(state_!=State::Safe||source_!=Source::None||owner_||!safe_confirmed(end))error=Error::State;
            else if(epoch_==std::numeric_limits<std::uint64_t>::max())error=Error::Space;
            else{clear();++epoch_;generation_=1;local_sequence_=0;session_=Session{};session_.id=m.session;opening_=Session{};safety_=Session{};configuration_cache_=Session{};h_.flush_remote(h_.context);changed();n=ack(data.data());cache=&opening_;}
        }else{
            // 先验证epoch/会话，再查缓存；淘汰会话绝不因缓存重试重新执行。
            if(config&&(m.length<34||u64(m.payload)!=uid_))error=Error::Malformed;
            else if(!read){const auto offset=config?8:0;const auto* g=m.payload+offset;auto len=m.length-offset;error=guard(g,len);
                if(error==Error::Generation&&!live&&!focus)error=Error::Ok; // Runtime writes never replay across STOP/generation.
                if(error==Error::Ok&&!safety&&!config&&session_.id!=m.session)error=Error::Session;
                if(error==Error::Ok&&live){
                    if(session_.id!=m.session)error=Error::Session;
                    else if(state_!=State::Active||source_!=Source::Mobocom||owner_!=m.session||fault_!=Fault::None)error=Error::Owner;
                    else if(operation_)error=Error::Busy;
                }
            }
            if(error==Error::Ok){
                // 配置写独占缓存，查询/安全命令不能淘汰正在保存的写事务。
                cache=config?&configuration_cache_:(session_.id==m.session?&session_:(safety?&safety_:nullptr));
                if(cache&&cache->id==m.session&&m.sequence<=cache->sequence){
                    if(m.sequence==cache->sequence&&size==cache->request_size&&!std::memcmp(raw,cache->request.data(),size))return cached_reply(*cache);
                    error=m.sequence==cache->sequence?Error::Conflict:Error::Sequence;cache=nullptr;
                }
            }
            if(error==Error::Ok)error=command(m,end,data.data(),n,kind);
        }
        if(error!=Error::Ok){kind=Kind::Error;put16(data.data(),std::uint16_t(error));put16(data.data()+2,0);n=4+ack(data.data()+4);}
        Message response{kind,uid_,AllNodes,m.sequence,m.command,m.session,data.data(),std::uint16_t(n)};reply_size_=encode(reply_.data(),response);reply_at_=end;reply_until_=end+ReplyHoldUs;
        if(cache){cache->id=m.session;cache->sequence=m.sequence;cache->request_size=std::uint16_t(size);cache->reply_size=std::uint16_t(reply_size_);std::memcpy(cache->request.data(),raw,size);cache->reply=reply_;if(open&&error==Error::Ok)session_=*cache;}
        return true;
    }
    // Network adapter calls this right after receive() and sends the bytes as one UDP datagram to the requester.
    bool take_reply(std::uint64_t now,std::uint8_t* out,std::size_t capacity,std::size_t& size){
        size=0;if(!reply_size_||now<reply_at_)return false;if(now>reply_until_){reply_size_=0;return false;}if(!out||capacity<reply_size_)return false;
        size=reply_size_;std::memcpy(out,reply_.data(),size);reply_size_=0;return true;
    }
};

class LocalControlInput {
    DeviceCore& node_;
public:
    explicit LocalControlInput(DeviceCore& node):node_(node){}
    Error targets(std::uint64_t epoch,std::uint32_t generation_,std::uint32_t sequence,const Target* values,std::size_t count,std::uint64_t now){return node_.local_targets(epoch,generation_,sequence,values,count,now);}
    Error action(std::uint64_t epoch,std::uint32_t generation_,std::uint32_t sequence,std::uint16_t opcode,const std::uint8_t* data,std::size_t size,std::uint64_t now){return node_.local_action(epoch,generation_,sequence,opcode,data,size,now);}
    Error heartbeat(std::uint64_t epoch,std::uint32_t generation_,std::uint64_t now){return node_.local_heartbeat(epoch,generation_,now);}
};
} // namespace mobocom
