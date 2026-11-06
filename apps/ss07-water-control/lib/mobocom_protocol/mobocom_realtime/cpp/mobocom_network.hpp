#pragma once

#include <cstdint>

namespace mobocom::network {

// Fixed by the v0x22 network binding. The multicast address is organization
// local and TTL=1; deployments use a dedicated/static-IP Ethernet segment.
inline constexpr const char* MulticastAddress = "239.255.33.21";
inline constexpr std::uint16_t Port = 47821;
inline constexpr std::uint32_t DiscoveryWindowUs = 100000;
inline constexpr std::uint32_t DefaultTransactionTimeoutMs = 30;

} // namespace mobocom::network
