"""生成可重放的设备本地超时恢复向量；既有线向量保持不变。"""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mobocom_realtime.tests.test_timeout_recovery import recovery_vectors
rows=recovery_vectors()
(ROOT/'mobocom_realtime/timeout_recovery_vectors.json').write_text(json.dumps(rows,indent=2)+'\n',encoding='utf-8')
lines=['#pragma once','#include <string_view>','#include <cstdint>',
       'struct RecoveryStep {unsigned scenario;std::uint64_t at;std::string_view op,request,reply;};',
       'inline constexpr RecoveryStep recovery_steps[]={']
for scenario,row in enumerate(rows):
    for s in row['steps']:
        lines.append('{%d,%d,"%s","%s","%s"},'%(scenario,s['at'],s['op'],s.get('request',''),s.get('reply','')))
(ROOT/'mobocom_realtime/tests/timeout_recovery_vectors.hpp').write_text('\n'.join(lines+['};'])+'\n',encoding='utf-8')
print('Generated',sum(len(r['steps']) for r in rows),'local recovery steps')
