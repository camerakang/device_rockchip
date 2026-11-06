"""Reproducible vectors, executable device traces, and network budget report."""
import json
import struct
from pathlib import Path
from .wire import ALL_NODES,HEADER_SIZE,MAX_FRAME,Frame,Kind,Command as C,encode,guard,targets
from .reference import DeviceCore,VirtualDriver,ACK,STATUS,ENTRY
from .profiles import lens,gimbal,fans
from .network import CONTROL_MULTICAST,CONTROL_PORT
from timecode.reference import Packet,encode as encode_timecode

ROOT=Path(__file__).resolve().parent
VERSION_LABEL='2.2.0-network.1'
LINK_BPS=100_000_000
# Ethernet preamble/SFD 8 + MAC header 14 + FCS 4 + inter-frame gap 12; IPv4 20 + UDP 8.
ETHERNET_OVERHEAD=38
IP_UDP_OVERHEAD=28

def link_bytes(datagram):
    """Bytes occupied on a 100BASE-TX link by one UDP datagram (no VLAN, no IP options)."""
    return max(46,datagram+IP_UDP_OVERHEAD)+ETHERNET_OVERHEAD

def budget_case(count,hz,resources):
    request=HEADER_SIZE+2+len(guard(0,0,0))+1+8*resources
    reply=HEADER_SIZE+2+ACK.size
    status_pages=(resources+7)//8
    status_request=HEADER_SIZE+2+2
    status_reply=min(MAX_FRAME,HEADER_SIZE+2+STATUS.size+8*ENTRY.size)
    per_second=count*(hz*(link_bytes(request)+link_bytes(reply))+status_pages*(link_bytes(status_request)+link_bytes(status_reply)))
    used=per_second*8/LINK_BPS
    return {'nodes':count,'hz_per_node':hz,'resources_per_node':resources,'target_request_bytes':request,'target_ack_bytes':reply,
            'link_bytes_per_target_cycle':link_bytes(request)+link_bytes(reply),'status_pages_per_second_per_node':status_pages,
            'link_bps':LINK_BPS,'utilization':round(used,6),'accepted':used<=0.8,
            'assumption':'100BASE-TX Ethernet/IPv4/UDP framing only; switch queueing, Wi-Fi and host scheduling are not modeled'}

def build():
    from .tests.test_motion_profile import write_vectors
    write_vectors()
    (ROOT/"tests").mkdir(exist_ok=True)
    (ROOT/"examples").mkdir(exist_ok=True)
    # 独立扩展向量，不改写既有运动事务的任何请求/应答期望。
    from .tests.test_configuration import configuration_vectors
    configuration = configuration_vectors()
    (ROOT/'configuration_vectors.json').write_text(json.dumps(configuration,indent=2)+'\n',encoding='utf-8')
    config_lines=['#pragma once','#include <cstdint>','#include <string_view>',
        'struct ConfigurationVector {std::uint64_t at;std::string_view request,reply;};',
        'inline constexpr ConfigurationVector configuration_vectors[]={']
    config_lines += ['{%d,"%s","%s"},'%(v['at_us'],v['request'],v['reply']) for v in configuration]
    (ROOT/'tests/configuration_vectors.hpp').write_text('\n'.join(config_lines+['};'])+'\n',encoding='utf-8')
    frames=[];trace=[]
    def frame(name,dst,seq,cmd,payload=b'',session=100):
        f=Frame(Kind.REQUEST,0,dst,seq,cmd,session,payload)
        raw=encode(f);frames.append({'name':name,'hex':raw.hex(),'kind':f.kind,'src_uid':str(f.src_uid),'dst_uid':str(f.dst_uid),'sequence':seq,'command':cmd,'session':str(session),'payload':payload.hex()});return raw
    # Trace instance index is local to the vectors; devices are addressed only by UID.
    nodes={1:DeviceCore(lens(),1,10,VirtualDriver()),2:DeviceCore(gimbal(),2,20,VirtualDriver()),3:DeviceCore(fans(),3,30,VirtualDriver())}
    seq={i:0 for i in nodes}
    def send(node,cmd,p=b'',at=0,name=None,raw=None,dst=None):
        if raw is None:seq[node]+=1;raw=frame(name or f'{node}:{cmd}:{seq[node]}',nodes[node].uid if dst is None else dst,seq[node],cmd,p)
        result=nodes[node].transact(raw,at)
        trace.append({'node':node,'uid':str(nodes[node].uid),'at_us':at,'request':raw.hex(),'reply':result.hex() if result else ''})
        return raw
    def tick(node,at):nodes[node].tick(at);trace.append({'node':node,'uid':str(nodes[node].uid),'at_us':at,'tick':True})
    for node in nodes:
        send(node,C.HELLO)
        send(node,C.INFO,at=20)
        for i in range(1+len(nodes[node].profile.resources)+len(nodes[node].profile.actions)):send(node,C.CAPS,struct.pack('<H',i),at=100+i*20)
        send(node,C.SESSION_OPEN,struct.pack("<QQI",nodes[node].boot_id,nodes[node].session_epoch,nodes[node].generation),at=900)
        send(node,C.SET_SOURCE,guard(nodes[node].boot_id,1,nodes[node].session_epoch)+struct.pack('<BII',2,100000,300000),at=1000)
        tick(node,1020)
        send(node,C.STATUS,b'\0\0',at=1040)
        send(node,C.HEARTBEAT,guard(nodes[node].boot_id,2,nodes[node].session_epoch),at=1060)
        send(node,C.EXECUTE_ACTION,guard(nodes[node].boot_id,2,nodes[node].session_epoch)+struct.pack('<H',1),at=2000)
        tick(node,22000)
        send(node,C.ACTION_STATUS,struct.pack('<Q',1),at=22020)
        values=[(r.endpoint,r.resource,0) for r in nodes[node].profile.resources]
        if node==2:send(node,C.WRITE_TARGETS,targets(20,2,values[:1],nodes[node].session_epoch),at=23000,name='partial_group_rejected')
        send(node,C.WRITE_TARGETS,targets(nodes[node].boot_id,2,values,nodes[node].session_epoch),at=23020)
        values=[(ep,res,100) for ep,res,_ in values]
        raw=send(node,C.WRITE_TARGETS,targets(nodes[node].boot_id,2,values,nodes[node].session_epoch),at=23040)
        send(node,C.WRITE_TARGETS,at=23060,raw=raw)
        tick(node,23080)
        send(node,C.STATUS,b'\0\0',at=23100)
        if node==3:send(node,C.STATUS,struct.pack('<H',8),at=23120)
        raw=send(node,C.STOP,guard(nodes[node].boot_id,nodes[node].generation,nodes[node].session_epoch),at=24000)
        send(node,C.STOP,at=24020,raw=raw)
        send(node,C.STATUS,b'\0\0',at=24040)
        send(node,C.ESTOP,guard(nodes[node].boot_id,nodes[node].generation,nodes[node].session_epoch),at=25000)
        send(node,C.RESET_FAULT,guard(nodes[node].boot_id,nodes[node].generation,nodes[node].session_epoch),at=25020)
        send(node,C.STATUS,b'\0\0',at=25040)
    # Fourth instance: multicast discovery in SAFE/ACTIVE/FAULT, scope limits and UID-unicast session.
    big_uid=0x9007000000000004  # above 2^53 so JavaScript callers must keep bigint
    nodes[4]=DeviceCore(gimbal(),big_uid,40,VirtualDriver());seq[4]=0
    send(4,C.DISCOVER,at=0,dst=ALL_NODES,name='discover_multicast')
    send(4,C.SESSION_OPEN,struct.pack('<QQI',40,1,1),at=100,dst=ALL_NODES,name='multicast_open_dropped')
    send(4,C.INFO,at=200,dst=ALL_NODES,name='multicast_info_dropped')
    send(4,C.DISCOVER,at=300,dst=big_uid,name='unicast_discover_dropped')
    send(4,C.INFO,at=400,name='info_by_uid')
    send(4,C.SESSION_OPEN,struct.pack('<QQI',40,1,1),at=500,name='open_by_uid')
    send(4,C.SET_SOURCE,guard(nodes[4].boot_id,nodes[4].generation,nodes[4].session_epoch)+struct.pack('<BII',2,100000,300000),at=550,name='set_source_active')
    tick(4,570)
    send(4,C.DISCOVER,at=580,dst=ALL_NODES,name='discover_while_active')
    send(4,C.ESTOP,at=600,dst=ALL_NODES,name='multicast_estop')
    send(4,C.STATUS,b'\0\0',at=700,name='status_after_multicast_estop')
    send(4,C.DISCOVER,at=800,dst=ALL_NODES,name='discover_while_fault')
    tc=encode_timecode(Packet(0x1122334455667788,0x12345678,123456789))
    result={'version':VERSION_LABEL,'transport':{'udp_port':CONTROL_PORT,'multicast':CONTROL_MULTICAST,'one_frame_per_datagram':True},
            'frames':frames,'trace':trace,'timecode_hex':tc.hex()}
    (ROOT/'vectors.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines=['#pragma once','#include <cstdint>','#include <string_view>',
           '// Each request/reply is one complete UDP datagram; an empty reply means the device stays silent.',
           'struct GoldenStep {unsigned node;std::uint64_t uid;std::uint64_t at;bool tick;std::string_view request,reply;};','inline constexpr GoldenStep golden_steps[]={']
    for step in trace:lines.append('{%d,%sULL,%d,%s,"%s","%s"},'%(step['node'],step['uid'],step['at_us'],'true' if step.get('tick') else 'false',step.get('request',''),step.get('reply','')))
    lines+=['};',f'inline constexpr std::string_view golden_timecode="{tc.hex()}";']
    (ROOT/'tests/vectors.hpp').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    report={'status':'implementation-candidate','version':VERSION_LABEL,'frames':len(frames),'trace_steps':len(trace),
        'virtual_parameters':{'target_ttl_us':100000,'lease_ttl_us':300000,'safe_delay_us':0,'action_delay_us':20000},
        'budgets':[budget_case(2,100,3),budget_case(16,100,3),budget_case(64,100,9),budget_case(1,100,16)],
        'fixed_capacity':{'resources':16,'sessions':1,'action_results':8,'max_frame_bytes':MAX_FRAME,'pending_replies':1},
        'hardware':'not measured; no switch, Wi-Fi or cross-device synchronization claim'}
    (ROOT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for i,p in ((1,lens()),(2,gimbal()),(3,fans())):
        steps=[s for s in trace if s['node']==i]
        (ROOT/f'examples/{i}_walkthrough.json').write_text(json.dumps({'virtual':True,'device_type':p.device_type,'resource_count':len(p.resources),'steps':steps},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result,report

if __name__=='__main__':
    _,report=build();print(json.dumps(report,ensure_ascii=False,indent=2))
