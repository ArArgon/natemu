#!/usr/bin/env python3
"""MASKMOVQ architectural checks (Intel SDM MASKMOVQ; Vol.3B Table 24-8).
Whole-eight-byte preflight including all-zero masks is our permitted implementation choice.
Faults preserve memory, MMX/x87 and SIMD state; success changes only selected bytes and TOP/FTW.
"""
import argparse, json, pathlib, struct, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/reference')]
import ref, run_i8_faults as i8, run_slice1_vm as vm1
from run_i5a_faults import D, CODE, NONCANON


def cases():
    out = []
    def add(name, bits=255, addr=D+0x200, setup=(), want=None, insn='maskmovq mm0, mm1', tail=(), **kw):
        mask = sum((0x80 if bits >> i & 1 else 0x7f) << (8*i) for i in range(8))
        pre = ['fninit', 'mov rax, 0x8877665544332211', 'movq mm0, rax', f'mov rax, {mask:#x}', 'movq mm1, rax',
               'emms', 'fld1', 'fldpi', f'mov rdi, {addr:#x}'] + list(tail)
        c = i8.case(name, insn, setup, want, prelude=pre, ymmh=False, **kw)
        c.update(bits=bits, addr=addr)
        out.append(c)
    for bits in (0, 1, 128, 85, 170, 255):
        for off in (0, 1, 7): add(f'mask{bits}-offset{off}', bits, D+0x200+off)
    for tag, addr, setup, want in [
        ('cross-ok', D+0xffc, [], None),
        ('last8', D+0xff8, [f'unmap {D+0x1000:#x}'], None),
        ('cross-np', D+0xffc, [f'unmap {D+0x1000:#x}'], [14,D+0x1000,6]),
        ('cross-ro', D+0xffc, [f'readonly {D+0x1000:#x}'], [14,D+0x1000,7]),
        ('first-ro', D+0xffc, [f'readonly {D:#x}'], [14,D+0xffc,7]),
        ('noncanonical', NONCANON, [], [13,0,0]),
        ('straddle', NONCANON-4, [], [13,0,0])]:
        for bits in (0, 1, 255): add(f'{tag}-mask{bits}',bits,addr,setup,want)
    for rex in range(0x40,0x50): add(f'rex{rex:02x}',85,insn=f'.byte {rex}, 0x0f, 0xf7, 0xc1')
    for data, mask in [(2,3),(4,5),(6,7),(7,0)]:
        add(f'regs{data}-{mask}',85,insn=f'maskmovq mm{data}, mm{mask}',
            tail=[f'movq mm{data}, mm0',f'movq mm{mask}, mm1','emms','fincstp'])
    add('same-register',0x88,insn='maskmovq mm0, mm0') # only byte7 has its MSB set; adjusted below
    out[-1]['bits']=128
    add('address32',85,addr=0x100200200,insn='.byte 0x67, 0x0f, 0xf7, 0xc1'); out[-1]['addr']=D+0x200
    for seg, msr in [('fs',0xc0000100),('gs',0xc0000101)]:
        add(seg,85,addr=0x200,insn=f'.byte {0x64 if seg=="fs" else 0x65}, 0x0f, 0xf7, 0xc1', cpl=0,
            tail=[f'mov ecx, {msr}',f'mov eax, {D}','xor edx, edx','wrmsr']); out[-1]['addr']=D+0x200
    for name,bits,want in [('em',4,[6,0,0]),('ts',8,[7,0,0]),('emts',12,[6,0,0])]:
        add(name,addr=NONCANON,want=want,cpl=0,tail=['mov rax, cr0',f'or eax, {bits}','mov cr0, rax'])
    add('noosfxsr',85,sse=False,xcr0=0)
    env=struct.pack('<14H',0x37b,0,0x3004,0,0xfff0,0,0,0,0,0,0,0,0,0)
    for tag, extra, want in [('mf',[],[16,0,0]),('mf-ts',['mov rax, cr0','or eax, 8','mov cr0, rax'],[7,0,0])]:
        add(tag,addr=NONCANON,setup=[f'memory {D+0x800:#x} {env.hex()}','ne 1'],want=want,cpl=0,
            tail=[f'fldenv [{D+0x800}]']+extra)
    for raw in ('0f f7 00','f0 0f f7 c1'):
        for ts in (False,True):
            add(f'ud-{raw.replace(" ","")}-{ts}',want=[6,0,0],cpl=0,insn='.byte '+','.join('0x'+b for b in raw.split()),
                tail=['mov rax, cr0','or eax, 8','mov cr0, rax'] if ts else [])
    # Enable AM at CPL0; the case switches to CPL3 just before MASKMOVQ, after its prelude.
    for bits in (0,255):
        for off in (0,1,4,7):
            add(f'ac-mask{bits}-off{off}',bits,D+0x200+off,want=[17,0,0] if off else None,cpl=0,
                tail=['mov rax, cr0','bts rax, 18','mov cr0, rax','nop'])
            c=out[-1]; at=CODE+len(ref.assemble('\n'.join(c['pre'][:-3])))
            c['setup'] += ['rflags 0x40202',f'switch {at:#x} 3']
    for tag,bits,offset,trap in [('selected',1,0,True),('masked-off',1,1,False),('zero',0,0,False)]:
        add('watch-'+tag,bits,cpl=0,tail=[f'mov rax, {D+0x200+offset}', 'mov dr0, rax',
            'mov eax, 0x10001', 'mov dr7, rax'])
        out[-1]['db']=trap
    add('watch-before-pf',255,D+0xffc,[f'unmap {D+0x1000:#x}'],[14,D+0x1000,2],cpl=0,
        tail=[f'mov rax, {D+0xffc}', 'mov dr0, rax', 'mov eax, 0x10001', 'mov dr7, rax'])
    return out


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--fexc',type=pathlib.Path,required=True); ap.add_argument('--out',type=pathlib.Path,required=True)
    ap.add_argument('--host',type=pathlib.Path); ap.add_argument('--runtime',type=pathlib.Path)
    ap.add_argument('--max-block',type=int); ap.add_argument('--only'); ap.add_argument('--no-build',action='store_true')
    a=ap.parse_args(); a.out.mkdir(parents=True,exist_ok=True)
    host,runtime=(ROOT/'target/release/natemu-host',ROOT/'target/aarch64-unknown-none/release/natemu-runtime') if a.no_build else vm1.build()
    host,runtime=a.host or host,a.runtime or runtime
    seen={}
    def run(a,h,r,c,lines,tag):
        code,labels=ref.assemble('\n'.join(f'{l}\nl{i}:' for i,l in enumerate(lines)),labels=True)
        offsets=[0]+[labels[f'l{i}'] for i in range(len(lines)-1)]
        work=a.out/tag;work.mkdir(exist_ok=True)
        (work/'program.txt').write_text(i8.description(c,code))
        cmd=[h,r,'run-program',a.fexc,work/'program.txt',work,'--watchdog-ms','5000']
        if a.max_block:cmd += [str(a.max_block)]
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        (work/'stdout.json').write_text(p.stdout);(work/'stderr.txt').write_text(p.stderr)
        v=json.loads(p.stdout) if not p.returncode else None;seen[tag]=v
        return v,offsets,p.stderr[-400:]
    i8.run=run
    results=[]
    for c in cases():
        if a.only and a.only not in c['name']:continue
        seen.clear()
        if c.get('db'):
            lines=c['pre']+c['lines']+i8.AFTER
            v,offsets,err=run(a,host,runtime,c,lines,c['name'])
            rip=CODE+offsets[len(c['pre'])+1]
            problems=[] if v and v['stop']=='guest_fault' and int(v['fault'][0],0)==1 and int(v['rip'],0)==rip else [f'watchpoint trap: {v and v.get("fault")}, {err}']
        else: problems=i8.judge(a,host,runtime,c)
        v=seen.get(c['name'])
        cut=seen.get(c['name']+'-cut')
        if v and c['want']:
            if cut and v['x87']!=cut['x87']:problems.append('x87 changed at fault')
        elif v:
            cut,_,err=run(a,host,runtime,c,c['pre']+['hlt'],c['name']+'-cut')
            if not cut:problems.append('cut failed: '+str(err))
            else:
                expected=dict(cut['x87']);expected.update(top=0,ftw='0xff')
                if v['x87']!=expected:problems.append(f'x87 {v["x87"]}, expected {expected}')
                gpr=list(cut['gpr'])
                if not c.get('db'): gpr[1]='0x1';gpr[2]='0x2'
                if v['gpr']!=gpr:problems.append('GPR changed unexpectedly')
                for k in ('xmm','ymmh','mxcsr'):
                    if v[k]!=cut[k]:problems.append(k+' changed')
                expected=bytearray.fromhex(cut['memory'][0]['data'])
                for j,b in enumerate(bytes.fromhex('1122334455667788')):
                    if c['bits']>>j&1:expected[c['addr']-D+j]=b
                if v['memory'][0]['data']!=expected.hex():problems.append('masked bytes differ')
        results.append(dict(name=c['name'],ok=not problems,problems=problems))
        if problems:print('FAIL',c['name'],str(problems)[:300],flush=True)
    (a.out/'results.json').write_text(json.dumps(results,indent=1)+'\n')
    bad=sum(not r['ok'] for r in results);print(f'{len(results)} cases, {len(results)-bad} passed, {bad} failed')
    sys.exit(bool(bad))
if __name__=='__main__':main()
