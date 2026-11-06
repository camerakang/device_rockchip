"""Generate/check the additive schema-4 trace; existing motion vectors remain untouched."""
from pathlib import Path
import argparse
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mobocom_realtime.tests.test_focus_control import focus_vectors

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    rows=focus_vectors()
    header='#pragma once\n#include <cstdint>\n#include <string_view>\nstruct FocusControlVector {std::uint64_t at;std::string_view request,reply;};\ninline constexpr FocusControlVector focus_control_vectors[]={\n'
    header+=''.join('{%d,"%s","%s"},\n'%(r['at_us'],r['request'],r['reply']) for r in rows)+'};\n'
    files={'mobocom_realtime/focus_control_vectors.json':json.dumps(rows,indent=2)+'\n',
           'mobocom_realtime/tests/focus_control_vectors.hpp':header}
    for name,text in files.items():
        path=ROOT/name
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8')!=text:raise SystemExit('stale vector: '+name)
        else:path.write_text(text,encoding='utf-8',newline='\n')
    print('CHECKED' if args.check else 'GENERATED',len(rows),'focus control vectors')
if __name__=='__main__':main()
