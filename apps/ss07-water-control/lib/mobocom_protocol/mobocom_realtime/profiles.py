"""Virtual fixtures only. IDs/units cannot be used as production device claims."""
from .reference import Profile, Resource, Action
from .device_types import DeviceType

# 这些Profile仅用于公共协议流程测试，不作为同名真实设备的硬件配置。
# DeviceType管理设备类别；下方0x8001等资源unit属于独立命名空间，数值相同不表示可互换。

def lens():
    return Profile(DeviceType.VIRTUAL_LENS,[Resource(1,i,0x8001,0,10000) for i in range(1,4)],
                   [Action(1,7,0,500_000)])
def gimbal():
    return Profile(DeviceType.VIRTUAL_GIMBAL,[Resource(1,i,0x8002,-180000,180000,3,1,90000,180000) for i in range(1,3)],
                   [Action(1,3,0,500_000)])
def fans():
    return Profile(DeviceType.VIRTUAL_FANS,[Resource(ep,r,0x8003,0,10000) for ep in range(1,4) for r in range(1,4)],
                   [Action(1,0,0,500_000)])
