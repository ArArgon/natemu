"""Isolated REV-184 R1 regressions against retained synthetic aggregate input."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('legacy',root/'src/check.py')
checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
base=json.loads((root/'tests/valid.json').read_text())
def execute(data):
    with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as out:
        json.dump(data,out);out.flush();stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=checker.main(out.name)
        return code,stream.getvalue()
assert execute(base)[0]==0
bad=copy.deepcopy(base)
row=next(w for w in bad['wakeup'] if w['mechanism']=='dispatch_source_timer_strict')
row['early']=1;row['min_ns']=-1
code,text=execute(bad)
assert code!=0 and 'FAIL  I6 ' in text and 'decision thresholds suppressed' in text and '  MEET  T' not in text and '  MISS  T' not in text
for value in (0,-1,False,1.5):
    bad=copy.deepcopy(base);bad['signal'][0]['roundtrip_min_ns']=value
    code,text=execute(bad)
    assert code!=0 and 'FAIL  I10b ' in text and 'decision thresholds suppressed' in text and '  MEET  T' not in text and '  MISS  T' not in text
print('REV-184 R1: valid baseline and five isolated strict/RTT domain regressions pass')
