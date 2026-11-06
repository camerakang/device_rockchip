#include "mobocom_treadmill.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <vector>
using namespace mobocom;

struct Fake {
    std::vector<std::int32_t> applied;
    bool safe = true;
    DriverHooks hooks() {
        DriverHooks h;
        h.context = this;
        h.validate_targets = [](void*, const Target*, std::size_t) { return true; };
        h.apply_targets = [](void* p, const Target* t, std::size_t n, std::uint64_t) {
            auto& f = *static_cast<Fake*>(p);
            for (std::size_t i = 0; i < n; ++i) f.applied.push_back(t[i].value);
            return true;
        };
        h.start_safe = [](void*, Fault, std::uint64_t) { return true; };
        h.safe_confirmed = [](void* p, std::uint64_t) { return static_cast<Fake*>(p)->safe; };
        h.reset_allowed = [](void*) { return true; };
        h.flush_remote = [](void*) {};
        h.feedback = [](void*, std::uint16_t, std::uint16_t, std::int32_t& v) { v = 0; return true; };
        return h;
    }
};

int main() {
    Fake f;
    auto p = treadmill::profile(1);
    DeviceCore core(p, 0x9003000000000001ull, 1, f.hooks());
    assert(core.valid());
    assert(p.type == device_type_id(DeviceType::Treadmill));
    assert(p.action_count == 0);
    assert(p.resources[0].unit == treadmill::SpeedUnit);
    assert(p.resources[0].flags == 3);
    assert(p.resources[0].minimum == -600 && p.resources[0].maximum == 600);
    assert(p.source_mask == 6 && p.auto_reset_mask == 0);

    // REMOTE(RS485) 来源：目标受理后由 tick 交付，范围外目标被拒绝。
    std::uint64_t now = 1000;
    assert(core.select_local_source(Source::Remote, core.session_epoch(), core.generation(), 100000, 300000, now) == Error::Ok);
    now += 1000; core.tick(now);
    assert(core.state() == State::Active && core.source() == Source::Remote);
    Target ok{treadmill::Endpoint, treadmill::SpeedResource, 250};
    assert(core.local_targets(core.session_epoch(), core.generation(), 1, &ok, 1, now) == Error::Ok);
    now += 1000; core.tick(now);
    assert(!f.applied.empty() && f.applied.back() == 250);
    Target bad{treadmill::Endpoint, treadmill::SpeedResource, 601};
    assert(core.local_targets(core.session_epoch(), core.generation(), 2, &bad, 1, now) == Error::Range);

    // 目标过期 -> 故障锁存，不自动恢复。
    now += 200000; core.tick(now);
    assert(core.fault() == Fault::TargetExpired || core.fault() == Fault::SourceTimeout || core.fault() == Fault::OutputGap);
    assert(!core.recover_timeout_fault(now + 1));
    return 0;
}
