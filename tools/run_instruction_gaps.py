#!/usr/bin/env python3
"""Independent bounded COR-02/03/05/06 CPU/RAM probes (public tests unchanged).

Run only with caller-held VM lock. Expected state comes from explicit Intel
profile rules, not the runtime decoder. ENTER prior stores follow the selected
AMD-observed profile; Intel ordinary-WB partial-store survival is not yet proved.
Near 66 branches follow Intel SDM Vol1 6.4.7, not QEMU's different 16-bit behavior.
"""
import argparse
import copy
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools/reference'))
import ref

CODE, DATA, HIGH = 0x100000, 0x200000, 0xffff800000200000
MASK, FLAGS = (1 << 64) - 1, 0xad7
ALIAS = 0xffff407abcde0000
FP_STATE = ('xmm', 'ymmh', 'mxcsr', 'x87')


def canonical(a):
    return a < 1 << 47 or a >= (1 << 64) - (1 << 47)


def memory_image():
    return {DATA: bytearray((i * 37 + 11) & 255 for i in range(0x4000))}


def bytes_at(memory, address, size):
    for base, raw in memory.items():
        if base <= address and address + size <= base + len(raw):
            return bytes(raw[address - base:address - base + size])
    raise ValueError(f'uncaptured memory {address:#x}+{size}')


def put(memory, address, value):
    for base, raw in memory.items():
        if base <= address and address + len(value) <= base + len(raw):
            raw[address - base:address - base + len(value)] = value
            return
    raise ValueError(f'uncaptured write {address:#x}+{len(value)}')


def case(name, code, regs=None, memory=None, setup=(), fault=None, effects=None, flags=FLAGS, flagmask=MASK, wantflags=None):
    initial = [0x1020304050607080 + i for i in range(16)]
    initial[4], initial[5] = DATA + 0x3000, DATA + 0x1800
    for r, v in (regs or {}).items():
        initial[r] = v & MASK
    mem = memory if memory is not None else memory_image()
    return dict(name=name, code=code, regs=initial, memory=mem, setup=list(setup), fault=fault,
                effects=effects or {}, flags=flags, flagmask=flagmask, wantflags=flags if wantflags is None else wantflags)


def enter_model(memory, rsp, rbp, width, nesting, allocation, protections=()):
    """Sequential SDM operations; fault returns original registers and preceding stores.

    The final stack-location check is a one-byte write-permission probe, not an
    architectural store. Chain loads read the current memory after prior pushes.
    """
    out, sp = copy.deepcopy(memory), rsp
    def access(address, size, write):
        address &= MASK
        if not canonical(address) or not canonical((address + size - 1) & MASK):
            return (12, 0, 0)
        for p, kind in protections:
            if any((address + i) & ~4095 == p for i in range(size)):
                if kind == 'unmap' or write and kind == 'readonly':
                    first = next(address + i for i in range(size) if (address + i) & ~4095 == p)
                    return (14, first, (2 if write else 0) | (1 if kind == 'readonly' else 0))
        return None
    def push(value):
        nonlocal sp
        address = (sp - width) & MASK
        fault = access(address, width, True)
        if fault:
            return fault
        put(out, address, (value & ((1 << (width * 8)) - 1)).to_bytes(width, 'little'))
        sp = address
        return None
    fault = push(rbp)
    frame = sp
    level = nesting & 31
    if not fault and level:
        for i in range(1, level):
            address = (rbp - i * width) & MASK
            fault = access(address, width, False)
            if fault:
                break
            fault = push(int.from_bytes(bytes_at(out, address, width), 'little'))
            if fault:
                break
        if not fault:
            fault = push(frame)
    final_sp = (sp - allocation) & MASK
    if not fault:
        fault = access(final_sp, 1, True)
    bp = (rbp & ~0xffff) | (frame & 0xffff) if width == 2 else frame
    return dict(memory=out, regs={4: rsp if fault else final_sp, 5: rbp if fault else bp}, fault=fault)


def enter_cases():
    for prefix, width, tag in [(b'', 8, '64'), (b'\x66', 2, '16'), (b'\x66\x48', 8, '66-rexw'), (b'\x48\x66', 2, 'rexw-66')]:
        for level in (0, 1, 2, 31, 32, 33, 255):
            for allocation in (0, 17, 0xffff):
                mem, rsp, rbp = memory_image(), DATA + 0x3000, DATA + 0x1800
                # The final probe is allowed to name otherwise uncaptured mapped RAM.
                if allocation == 0xffff:
                    mem[DATA - 0x10000] = bytearray(0x10000)
                model = enter_model(mem, rsp, rbp, width, level, allocation)
                yield case(f'enter-{tag}-level{level}-alloc{allocation}', prefix + b'\xc8' + allocation.to_bytes(2, 'little') + bytes([level]),
                           memory=mem, fault=model['fault'], effects=model)
        for alias in ('same', 'above', 'below'):
            mem, rsp = memory_image(), DATA + 0x3000
            rbp = rsp + {'same': 0, 'above': width, 'below': -width}[alias]
            model = enter_model(mem, rsp, rbp, width, 3, 0)
            yield case(f'enter-{tag}-chain-alias-{alias}', prefix + b'\xc8\0\0\3', regs={5: rbp}, memory=mem, effects=model)
        # Each internal access phase is faulted separately; preceding writes remain checked.
        for phase in ('first', 'chain-read', 'chain-write', 'frame-push', 'probe-unmap', 'probe-readonly'):
            mem, rsp, rbp, level, allocation = memory_image(), DATA + 0x3000, DATA + 0x1800, 2, 0
            page, kind = DATA, 'unmap'
            if phase == 'first':
                page = DATA + 0x2000
            elif phase == 'chain-read':
                rbp = DATA + 0x1000
            elif phase == 'chain-write':
                rsp, rbp = DATA + 0x1000 + width, DATA + 0x2800
            elif phase == 'frame-push':
                rsp, rbp = DATA + 0x1000 + 2 * width, DATA + 0x2800
            else:
                allocation = 0x2000
                kind = 'readonly' if phase == 'probe-readonly' else 'unmap'
            model = enter_model(mem, rsp, rbp, width, level, allocation, [(page, kind)])
            assert model['fault'] is not None
            yield case(f'enter-{tag}-fault-{phase}', prefix + b'\xc8' + allocation.to_bytes(2, 'little') + bytes([level]),
                       regs={4: rsp, 5: rbp}, memory=mem, setup=[f'{kind} {page:#x}'], fault=model['fault'], effects=model)
    # Operand-size16 updates BP only even when stack and frame are in different64KiB windows.
    mem = memory_image();mem[DATA + 0x10000] = bytearray(0x1000)
    rsp, rbp = DATA + 0x10800, DATA + 0x1800
    model = enter_model(mem, rsp, rbp, 2, 1, 7)
    yield case('enter16-stack-other-window', b'\x66\xc8\7\0\1', regs={4:rsp,5:rbp}, memory=mem, effects=model)
    mem = memory_image();mem[HIGH] = bytearray(4096)
    # Allocation across the canonical lower edge of the upper half: pushes succeeded, final probe #SS.
    edge = 0xffff800000000000
    mem[edge] = bytearray(4096)
    rsp = edge + 0x100
    model = enter_model(mem, rsp, DATA + 0x1800, 8, 0, 0x200)
    yield case('enter64-final-probe-noncanonical', b'\xc8\0\2\0', regs={4:rsp}, memory=mem, fault=model['fault'], effects=model)


def near_cases():
    # Intel near branches force64bit target/stack width, irrespective of66 orREX ordering.
    for prefix, tag in [(b'\x66','66'),(b'\x66\x48','66-rexw'),(b'\x48\x66','rexw-66'),(b'\x66\x67','66-a32')]:
        for kind, insn in [('call-reg',b'\xff\xd0'),('call-mem',b'\xff\x17'),('jmp-reg',b'\xff\xe0'),('jmp-mem',b'\xff\x27'),('ret',b'\xc3'),('ret-pop',b'\xc2\x12\0')]:
            target, rsp = CODE + 0x80, DATA + 0x3000
            code = prefix + insn
            ret_address = CODE + 1 + len(code)
            if kind.startswith('call'):
                text = '.byte ' + ','.join(hex(x) for x in prefix + insn) + '\nmov r11, 0x1234\njmp done\n.org 0x7f, 0x90\nmov r10, 0x5678\nret\ndone: nop'
            else:
                text = '.byte ' + ','.join(hex(x) for x in prefix + insn) + '\nud2\n.org 0x7f, 0x90\nmov r10, 0x5678\nnop'
            code = ref.assemble(text)
            mem = memory_image();put(mem, DATA + 0x100, target.to_bytes(8,'little'))
            if kind.startswith('ret'):
                put(mem, rsp, target.to_bytes(8,'little'))
            expected = copy.deepcopy(mem)
            want = {10:0x5678}
            if kind.startswith('call'):
                put(expected,rsp-8,ret_address.to_bytes(8,'little'));want[11]=0x1234
            if kind.startswith('ret'):
                want[4]=rsp+8+(0x12 if kind=='ret-pop' else 0)
            yield case(f'near-{tag}-{kind}',code,regs={0:target,7:(DATA+0x100)|(0xdeadbeef00000000 if tag=='66-a32' else 0)},memory=mem,effects={'regs':want,'memory':expected})
    for kind, insn in [('call-reg',b'\xff\xd0'),('call-mem',b'\xff\x17'),('jmp-reg',b'\xff\xe0'),('jmp-mem',b'\xff\x27'),('ret',b'\xc3')]:
        target, rsp = 0xdead000000100080, DATA+0x3000
        mem=memory_image();put(mem,DATA+0x100,target.to_bytes(8,'little'));put(mem,rsp,target.to_bytes(8,'little'))
        yield case(f'near-66-{kind}-noncanonical-target',b'\x66'+insn,regs={0:target,7:DATA+0x100},memory=mem,fault=(13,0,0))
    for kind, insn, write in [('call-mem',b'\xff\x17',False),('jmp-mem',b'\xff\x27',False),('ret',b'\xc3',False),('call-reg',b'\xff\xd0',True)]:
        regs={0:CODE+0x80,7:DATA+0x100}
        addr=DATA+0x100 if kind.endswith('mem') else DATA+0x3000-8 if write else DATA+0x3000
        yield case(f'near-66-{kind}-pagefault',b'\x66'+insn,regs=regs,setup=[f'unmap {addr & ~4095:#x}'],fault=(14,addr,2 if write else 0))


def bt_cases():
    # Selected contract: signed operand-width bit index adjustsEA before32bit wrap, thenFS/GS is added.
    for bits in (16,32,64):
        width=bits//8;word={16:'word',32:'dword',64:'qword'}[bits];reg={16:'cx',32:'ecx',64:'rcx'}[bits]
        for op in ('bt','bts','btr','btc'):
            for locked in ((False,True) if op!='bt' else (False,)):
                for index in (-bits-1,-1,0,bits-1,bits,bits+1):
                    address=DATA+0x100
                    base=(address-(index//bits)*width)&0xffffffff
                    mem=memory_image();old=int.from_bytes(bytes_at(mem,address,width),'little');bit=index%bits
                    value=old | 1<<bit if op=='bts' else old & ~(1<<bit) if op=='btr' else old ^ 1<<bit if op=='btc' else old
                    expected=copy.deepcopy(mem);put(expected,address,value.to_bytes(width,'little'))
                    code=ref.assemble(f'{"lock " if locked else ""}{op} {word} ptr [eax], {reg}')
                    yield case(f'bt-a32-{op}{bits}-{"lock-" if locked else ""}index{index}',code,regs={0:base|0xdeadbeef00000000,1:index&MASK},memory=mem,
                               effects={'memory':expected},wantflags=(FLAGS&~1)|((old>>bit)&1),flagmask=1)
        for base,index,target in [(0xfffffffe, bits*2, (0xfffffffe+2*width)&0xffffffff),(0,-bits,(-width)&0xffffffff)]:
            # FS shifts the wrapped32bit offset into captured ordinary RAM (base itself canonical).
            segment=DATA-target
            if segment<0:segment&=MASK
            mem=memory_image();old=int.from_bytes(bytes_at(mem,DATA,width),'little')
            yield case(f'bt-a32-wrap-{bits}-base{base:x}',ref.assemble(f'bt {word} ptr fs:[eax], {reg}'),regs={0:base,1:index&MASK},memory=mem,
                       setup=[f'fsbase {segment:#x}'],wantflags=(FLAGS&~1)|(old&1),flagmask=1)
        for op in ('bt','bts'):
            address=DATA+0x1000-width+1
            yield case(f'bt-a32-{op}{bits}-cross-page',ref.assemble(f'{op} {word} ptr [eax], {reg}'),regs={0:address,1:0},
                       setup=[f'unmap {DATA+0x1000:#x}'],fault=(14,DATA+0x1000,0 if op=='bt' else 2))


def enable_ac(c):
    """SetAM atCPL0 and restore initialGPR/flags before switching to the caseCPL."""
    c['prefix']=ref.assemble(f'mov rdx, cr0; bts rdx, 18; mov cr0, rdx; mov rdx, {c["regs"][2]:#x}; push {c["flags"]:#x}; popfq')+c.get('prefix',b'\x90')
    c['prefix_cpl']=0;c['initial_flags']=c['flags']&~0x40000
    if c['name'].startswith('far16-'):
        oldtarget=c['base']+1+len(c['code']);newtarget=c['base']+len(c['prefix'])+len(c['code'])
        for mem in (c['memory'],c['effects'].get('memory',{})):
            for base,raw in mem.items():
                # Rewrite only explicit pointer/frame/return-offset fields, not arbitrary pattern bytes.
                addresses=[c['regs'][7] if '-call-' in c['name'] or '-jmp-' in c['name'] else c['regs'][4]]
                if '-call-' in c['name']:addresses.append(c['regs'][4]-4)
                for address in addresses:
                    if base<=address<base+len(raw) and bytes_at(mem,address,2)==oldtarget.to_bytes(2,'little'):
                        put(mem,address,newtarget.to_bytes(2,'little'))


def additional_cases():
    for width, prefix in ((8,b''),(2,b'\x66')):
        rsp=DATA+0x3800;allocation=rsp-width-(DATA+0x1fff);mem=memory_image()
        model=enter_model(mem,rsp,DATA+0x1800,width,0,allocation,[(DATA+0x2000,'unmap')])
        assert model['fault'] is None
        yield case(f'enter{width*8}-final-byte-before-unmapped',prefix+b'\xc8'+allocation.to_bytes(2,'little')+b'\0',
                   regs={4:rsp},memory=mem,setup=[f'unmap {DATA+0x2000:#x}'],effects=model)
    for prefix,width,tag in [(b'\x66\x48',8,'66-rexw'),(b'\x48\x66',2,'rexw-66')]:
        mem=memory_image();bp=DATA+0x1800;value=int.from_bytes(bytes_at(mem,bp,width),'little')
        wantbp=value if width==8 else (bp&~0xffff)|value
        yield case('leave-'+tag,prefix+b'\xc9',memory=mem,effects={'regs':{4:bp+width,5:wantbp}})
    for op,opcode in [('call',b'\xff\x17'),('jmp',b'\xff\x27')]:
        mem=memory_image();address=DATA+0xffe;put(mem,address,(CODE+0x80).to_bytes(8,'little'))
        yield case('near-66-'+op+'-m64-upper-bytes-fault',b'\x66'+opcode,regs={7:address},memory=mem,
                   setup=[f'unmap {DATA+0x1000:#x}'],fault=(14,DATA+0x1000,0))
    for bits in (16,32,64):
        width=bits//8;base=0xfffffffe;target=(base+2*width)&0xffffffff
        word={16:'word',32:'dword',64:'qword'}[bits];reg={16:'cx',32:'ecx',64:'rcx'}[bits]
        mem=memory_image();old=int.from_bytes(bytes_at(mem,DATA,width),'little')
        yield case(f'bt-a32-gs-wrap-{bits}',ref.assemble(f'bt {word} ptr gs:[eax], {reg}'),regs={0:base,1:bits*2},memory=mem,
                   setup=[f'gsbase {DATA-target:#x}'],wantflags=(FLAGS&~1)|(old&1),flagmask=1)
    # Valid 16-bit far targets lie below64KiB. Stop at the target before executing another instruction.
    for cpl in (0,3):
        cs,ss=(8,0x10) if cpl==0 else (0x23,0x1b)
        for op,opcode in [('call',b'\x66\xff\x1f'),('jmp',b'\x66\xff\x2f'),('ret',b'\x66\xcb'),('iret',b'\x66\xcf')]:
            for odd_word in (False,True):
                base=0x8000;target=base+1+len(opcode);rsp=DATA+0x3000+(2 if odd_word else 0);ptr=DATA+0x102
                mem=memory_image();put(mem,ptr,target.to_bytes(2,'little')+cs.to_bytes(2,'little'))
                upper=0x3c0000 if op=='iret' else 0;flags=0x202|upper|(0x40000 if odd_word else 0)
                if odd_word: put(mem,rsp-8,flags.to_bytes(8,'little'))
                if op=='ret':put(mem,rsp,target.to_bytes(2,'little')+cs.to_bytes(2,'little'))
                if op=='iret':put(mem,rsp,b''.join(v.to_bytes(2,'little') for v in (target,cs,0xad7,0xa000,ss)))
                expected=copy.deepcopy(mem);regs={}
                if op=='call':
                    put(expected,rsp-4,target.to_bytes(2,'little')+cs.to_bytes(2,'little'));regs[4]=rsp-4
                if op=='ret':regs[4]=rsp+4
                if op=='iret':regs[4]=0xa000
                c=case(f'far16-{op}-cpl{cpl}-wordoffset{int(odd_word)*2}',opcode,regs={4:rsp,7:ptr},memory=mem,
                       effects={'regs':regs,'memory':expected},flags=flags,wantflags=(upper|0xad7) if op=='iret' else flags)
                c.update(base=base,cpls=(cpl,))
                if odd_word: enable_ac(c)
                c['code']+=ref.assemble('mov r14d, cs; mov r15d, ss')
                c['effects']['regs'].update({14:cs,15:ss})
                yield c
                # Invalid null CS: fault at original instruction, no architectural stack/register effect.
                badmem=copy.deepcopy(mem)
                put(badmem,ptr+2 if op in ('call','jmp') else rsp+2,b'\0\0')
                c=case(f'far16-{op}-null-cs-cpl{cpl}-wordoffset{int(odd_word)*2}',opcode,regs={4:rsp,7:ptr},memory=badmem,fault=(13,0,0),flags=flags)
                c.update(base=base,cpls=(cpl,))
                if odd_word: enable_ac(c)
                yield c
        for op,opcode in [('call',b'\x66\xff\x1f'),('jmp',b'\x66\xff\x2f'),('ret',b'\x66\xcb'),('iret',b'\x66\xcf')]:
            address=DATA+0xffe;mem=memory_image();put(mem,address,(0x8004).to_bytes(2,'little')+cs.to_bytes(2,'little'))
            c=case(f'far16-{op}-split-read-cpl{cpl}',opcode,regs={7:address} if op in ('call','jmp') else {4:address},memory=mem,
                   setup=[f'unmap {DATA+0x1000:#x}'],fault=(14,DATA+0x1000,0))
            c.update(base=0x8000,cpls=(cpl,));yield c

def alignment_watch_cases():
    for op,opcode in [('call',b'\x66\xff\x1f'),('jmp',b'\x66\xff\x2f'),('ret',b'\x66\xcb'),('iret',b'\x66\xcf')]:
        address=DATA+0x101 if op in ('call','jmp') else DATA+0x3001
        rsp=DATA+0x3000 if op in ('call','jmp') else address
        mem=memory_image();put(mem,address,b'\x04\x80\x23\0'+bytes(6));put(mem,rsp-8,(0x40202).to_bytes(8,'little'))
        c=case(f'far16-{op}-odd-ac',opcode,regs={4:rsp,7:address},memory=mem,fault=(17,0,0),flags=0x40202)
        c.update(base=0x8000,cpls=(3,));enable_ac(c);yield c
    for bits in (16,32,64):
        width=bits//8;word={16:'word',32:'dword',64:'qword'}[bits];reg={16:'cx',32:'ecx',64:'rcx'}[bits]
        for op in ('bt','btc'):
            mem=memory_image();put(mem,DATA+0x3000-8,(0x40202).to_bytes(8,'little'))
            c=case(f'bt-a32-{op}{bits}-ac',ref.assemble(f'{op} {word} ptr [eax], {reg}'),regs={0:DATA+1,1:0},memory=mem,
                   fault=(17,0,0),flags=0x40202)
            c['cpls']=(3,);enable_ac(c);yield c
            mem=memory_image();value=int.from_bytes(bytes_at(mem,DATA,width),'little');expected=copy.deepcopy(mem)
            if op=='btc':put(expected,DATA,(value^1).to_bytes(width,'little'))
            c=case(f'bt-a32-{op}{bits}-watch-last-byte',ref.assemble(f'{op} {word} ptr fs:[eax], {reg}'),regs={0:0xfffffffe,1:bits*2},memory=mem,
                   setup=[f'fsbase {DATA-((0xfffffffe+2*width)&0xffffffff):#x}'],
                   fault=(1,0,0),effects={'memory':expected},wantflags=(FLAGS&~1)|(value&1),flagmask=1)
            c['prefix']=ref.assemble(f'mov rdx, {DATA+width-1:#x}; mov dr0, rdx; mov edx, 0x30001; mov dr7, rdx; mov rdx, {c["regs"][2]:#x}; nop')
            c['prefix_cpl']=0;c['fault_at']=CODE+len(c['prefix'])+len(c['code']);yield c

# Hand-derived binary16 vectors: columns are nearest-even,down,up,toward-zero.
# These are fixed IEEE encodings, not values produced by the new runtime helper.
HALF_VECTORS = [
    ('zero',0x00000000,(0,0,0,0),(0,0,0,0)),
    ('negative-zero',0x80000000,(0x8000,)*4,(0,)*4),
    ('one',0x3f800000,(0x3c00,)*4,(0,)*4),
    ('tie-even',0x3f801000,(0x3c00,0x3c00,0x3c01,0x3c00),(0x20,)*4),
    ('negative-tie',0xbf801000,(0xbc00,0xbc01,0xbc00,0xbc00),(0x20,)*4),
    ('tiny-halfway',0x33000000,(0,0,1,0),(0x30,)*4),
    ('exact-subnormal',0x33800000,(1,)*4,(0,)*4),
    ('normal-predecessor',0x387fffff,(0x400,0x3ff,0x400,0x3ff),(0x20,0x30,0x20,0x30)),
    ('normal-midpoint',0x387fe000,(0x400,0x3ff,0x400,0x3ff),(0x30,)*4),
    ('overflow',0x47800000,(0x7c00,0x7bff,0x7c00,0x7bff),(0x28,)*4),
    ('infinity',0x7f800000,(0x7c00,)*4,(0,)*4),
    ('qnan',0x7fc12345,(0x7e09,)*4,(0,)*4),
    ('snan',0x7f812345,(0x7e09,)*4,(1,)*4),
    ('f32-subnormal',0x00000001,(0,0,1,0),(0x32,)*4),
]


def half_case(name,bits,half,raised,lanes=4,rounding=0,mxcsr=0x1f84,via_mxcsr=False,source=0,address=DATA+0x2000,stack=False,fault=None):
    mem=memory_image();values=[0x3f800000]*8;values[lanes-1]=bits
    put(mem,DATA+0x1000,b''.join(v.to_bytes(4,'little') for v in values))
    expected=copy.deepcopy(mem)
    if fault is None:
        out=[0x3c00]*lanes;out[-1]=half
        put(expected,address,b''.join(v.to_bytes(2,'little') for v in out))
    csr=(mxcsr&~0x6000)|(rounding<<13) if via_mxcsr else mxcsr
    opreg=('xmm' if lanes==4 else 'ymm')+str(source)
    insn=f'vcvtps2ph [{"rsp" if stack else "rdi"}], {opreg}, {4 if via_mxcsr else rounding}'
    c=case('cvt-'+name,ref.assemble(insn),regs={4 if stack else 7:address},memory=mem,fault=fault,
           effects={'memory':expected,'mxcsr':csr|raised})
    c['mxcsr']=csr;c['prefix']=ref.assemble(f'vmovdqu ymm{source}, [{DATA+0x1000:#x}]; nop')
    return c


def half_cases():
    for name,bits,halves,raised in HALF_VECTORS:
        for rounding in range(4):
            for lanes in (4,8):
                # Alternate immediate/MXCSR control and source0/9, while keeping all four rounding modes in each width.
                via=bool(rounding&1)
                yield half_case(f'{name}-rc{rounding}-lanes{lanes}',bits,halves[rounding],raised[rounding],lanes,rounding,via_mxcsr=via,source=9 if lanes==8 else 0)
    for control,csr,bits,want,raised in [('ftz-ignored',0x9f84,0x33000000,0,0x30),('daz-positive',0x1fc4,1,0,0),('daz-negative',0x1fc4,0x80000001,0x8000,0)]:
        yield half_case(control,bits,want,raised,8,mxcsr=csr,source=9)
    for lanes in (4,8):
        width=lanes*2
        for kind in ('unmap','readonly','cross','noncanonical-ds','noncanonical-ss'):
            address=DATA+0x2000
            if kind=='cross':address=DATA+0x3000-width+1
            if kind.startswith('noncanonical'):address=0x0000800000000100
            f=(12 if kind.endswith('ss') else 13,0,0) if kind.startswith('noncanonical') else (14,DATA+0x3000 if kind=='cross' else address,3 if kind=='readonly' else 2)
            c=half_case(f'store-{kind}-lanes{lanes}',0x47800000,0x7c00,0,lanes,address=address,stack=kind.endswith('ss'),fault=f)
            if kind in ('unmap','readonly'):c['setup'].append(f'{kind} {address:#x}')
            if kind=='cross':c['setup'].append(f'unmap {DATA+0x3000:#x}')
            yield c
        # Type11 SIMD memory stores do not #AC: offset1 still commits underCPL3 AM+AC.
        c=half_case(f'unaligned-no-ac-lanes{lanes}',0x3f801000,0x3c00,0x20,lanes,address=DATA+0x2001)
        c['flags']=c['wantflags']=0x40202;c['cpls']=(3,)
        for m in (c['memory'],c['effects']['memory']):put(m,c['regs'][4]-8,c['flags'].to_bytes(8,'little'))
        enable_ac(c);yield c
    # Definite exception-priority cells. OE's precise unmasked PE co-report and simultaneous
    # destination fault versus #XM remain separate reference-adjudication questions, not assumed here.
    for name,bits,maskbit,raised in [('invalid',0x7f800001,7,1),('denormal',1,8,2),('underflow-exact',0x33800000,11,0x10),('precision',0x3f801000,12,0x20)]:
        csr=0x1f84&~(1<<maskbit)
        yield half_case('unmasked-'+name,bits,0,raised,8,mxcsr=csr,source=9,fault=(19,0,0))

def half_supplements():
    # Complement the alternating control path inhalf_cases: everyRC is exercised both fromimm andMXCSR.
    halves=(0x3c00,0x3c00,0x3c01,0x3c00)
    for rc in range(4):
        for lanes in (4,8):
            yield half_case(f'round-control-complement-rc{rc}-lanes{lanes}',0x3f801000,halves[rc],0x20,lanes,rc,via_mxcsr=not bool(rc&1))
    c=half_case('unmasked-invalid-before-overflow',0x7f800001,0,1,8,mxcsr=0x1f04,fault=(19,0,0))
    for mem in (c['memory'],c['effects']['memory']):put(mem,DATA+0x1000,(0x47800000).to_bytes(4,'little'))
    yield c
    # Previously translatedDIVSS setsDZ; laterconversion would setOE|PE but its store faults.
    c=half_case('prior-divss-sticky-store-fault',0x47800000,0,4,8,mxcsr=0x1f80,fault=(14,DATA+0x2000,2))
    for mem in (c['memory'],c['effects']['memory']):
        put(mem,DATA+0x1800,(0x3f800000).to_bytes(4,'little')+bytes(4))
    c['prefix']=ref.assemble(f'movss xmm2, [{DATA+0x1800:#x}]; divss xmm2, [{DATA+0x1804:#x}]; vmovdqu ymm0, [{DATA+0x1000:#x}]; nop')
    c['setup'].append(f'unmap {DATA+0x2000:#x}');yield c
    c=half_case('unmasked-invalid-osxmmexcpt0',0x7f800001,0,1,8,mxcsr=0x1f04,fault=(6,0,0))
    for mem in (c['memory'],c['effects']['memory']):put(mem,c['regs'][4]-8,c['flags'].to_bytes(8,'little'))
    c['prefix']=ref.assemble(f'mov rdx, cr4; btr rdx, 10; mov cr4, rdx; mov rdx, {c["regs"][2]:#x}; push {c["flags"]:#x}; popfq')+c['prefix']
    c['prefix_cpl']=0;yield c
    # Retain selected-policy cells explicitly, including the unmaskedOE/PE ambiguity.
    # Differential/native observations must be adjudicated before claiming universalhardwareagreement.
    c=half_case('policy-unmasked-overflow-precision',0x47800000,0,0x28,8,mxcsr=0x1b84,fault=(19,0,0))
    c['oracle_basis']='Selected SIMD profile: unmaskedOE also publishesPE; hardware differential evidence pending.'
    yield c
    for name,bits,maskbit in [('invalid',0x7f800001,7),('denormal',1,8),('underflow',0x33800000,11),('overflow',0x47800000,10),('precision',0x3f801000,12)]:
        for kind in ('unmap','readonly','noncanonical'):
            address=0x0000800000000100 if kind=='noncanonical' else DATA+0x2000
            fault=(13,0,0) if kind=='noncanonical' else (14,address,3 if kind=='readonly' else 2)
            c=half_case(f'policy-{kind}-before-unmasked-{name}',bits,0,0,8,mxcsr=0x1f84&~(1<<maskbit),address=address,fault=fault)
            if kind!='noncanonical':c['setup'].append(f'{kind} {address:#x}')
            c['oracle_basis']='Selected memory-first transaction profile: destination fault precedesSIMD exception; rawdifferential evidence retained separately.'
            yield c

def cases():
    yield from near_cases()
    yield from enter_cases()
    yield from bt_cases()
    yield from additional_cases()
    yield from half_cases()
    yield from alignment_watch_cases()
    yield from half_supplements()


def description(c,cpl,prefix=False):
    code=c.get('prefix',b'\x90')+(b'' if prefix else c['code'])
    if not code: code=b'\x90'
    base=c.get('base',CODE)
    lines=[f'rip {base:#x}',f'cpl {c.get("prefix_cpl",cpl)}',f'rflags {c.get("initial_flags",c["flags"]):#x}','sse 1','xcr0 7','ne 1',
           'gpr '+' '.join(hex(r) for r in c['regs']),f'code {code.hex()}',
           'xmm 0 0x0123456789abcdef 0xfedcba9876543210','xmm 1 0x1122334455667788 0x8877665544332211']
    lines += [f'memory {a:#x} {bytes(raw).hex()}' for a,raw in c['memory'].items()]
    lines += c['setup']
    if 'mxcsr' in c: lines.append(f'mxcsr {c["mxcsr"]:#x}')
    if 'prefix_cpl' in c:
        switch=base+len(c.get('prefix',bytes([0x90])))-1
        lines.append(f'switch {switch:#x} {cpl}')
    return '\n'.join(lines)+'\n'


def judge(c,cpl,before,got):
    errors=[];f=c['fault'];base=c.get('base',CODE);at=base+len(c.get('prefix',b'\x90'))
    if f:
        vector,address,error=f
        want=[vector,address,error|(4 if vector==14 and cpl==3 else 0),c.get('fault_at',at)]
        if got['stop']!='guest_fault' or list(map(ref.num,got['fault']))!=want:
            errors.append(f'fault {got.get("fault")} stop {got["stop"]}, expected {want}')
    elif got['stop']!='reached' or ref.num(got['rip'])!=base+len(c.get('prefix',b'\x90'))+len(c['code']):
        errors.append(f'completion {got["stop"]} at {got["rip"]}')
    regs=list(c['regs']);effects=c['effects']
    for r,v in effects.get('regs',{}).items():regs[r]=v
    if list(map(ref.num,got['gpr']))!=regs:errors.append('register effects')
    if (ref.num(got['rflags'])^c['wantflags'])&c['flagmask']:errors.append('defined RFLAGS')
    for key in FP_STATE:
        if key=='mxcsr' and 'mxcsr' in effects:
            if ref.num(got[key])!=effects[key]:errors.append('MXCSR result')
        elif got.get(key)!=before.get(key):errors.append(key+' changed')
    expected=effects.get('memory',c['memory'])
    for base,raw in expected.items():
        found=next((m for m in got['memory'] if ref.num(m['addr'])==base),None)
        if found is None or bytes.fromhex(found['data'])!=bytes(raw):errors.append(f'memory effects at {base:#x}')
    if not got.get('frame_intact'):errors.append('frame sentinel changed')
    return errors


def execute(a,text,out,block):
    out.mkdir(parents=True,exist_ok=False);(out/'program.txt').write_text(text)
    cmd=[str(a.host),str(a.runtime),'run-program',str(a.fexc),str(out/'program.txt'),str(out),'--code-shape','--watchdog-ms','5000',
         '--guest-state-alias',hex(ALIAS)]+([str(block)] if block else [])
    (out/'command.json').write_text(json.dumps(cmd)+'\n')
    try:
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        (out/'stdout').write_text(p.stdout);(out/'stderr').write_text(p.stderr)
        return json.loads(p.stdout) if p.returncode==0 else None,p.returncode
    except subprocess.TimeoutExpired as e:
        for name,value in [('stdout',e.stdout),('stderr',e.stderr)]:
            (out/name).write_text(value.decode(errors='replace') if isinstance(value,bytes) else value or '')
        (out/'status.json').write_text(json.dumps({'status':'timeout','limit_seconds':30})+'\n')
        return None,'timeout'


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host',type=pathlib.Path,default=ROOT/'target/release/natemu-host')
    ap.add_argument('--runtime',type=pathlib.Path,default=ROOT/'target/aarch64-unknown-none/release/natemu-runtime')
    ap.add_argument('--fexc',type=pathlib.Path,required=True);ap.add_argument('--out',type=pathlib.Path,required=True)
    ap.add_argument('--only',default='');ap.add_argument('--max-block',type=int,help='omit to run both default and1')
    a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    (a.out/'inputs.json').write_text(json.dumps({str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in [a.host,a.runtime,a.fexc,pathlib.Path(__file__)]},indent=2)+'\n')
    rows=[]
    selected=[c for c in cases() if a.only in c['name']]
    assert len({c['name'] for c in selected})==len(selected)
    blocks=[a.max_block] if a.max_block is not None else [None,1]
    plan=[f'{c["name"]}-cpl{p}-block{b}' for c in selected for p in c.get('cpls',(0,3)) for b in blocks]
    (a.out/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    for c in selected:
        for cpl in c.get('cpls',(0,3)):
            for block in blocks:
                name=f'{c["name"]}-cpl{cpl}-block{block}'
                before,r0=execute(a,description(c,cpl,True),a.out/name/'prefix',block)
                got,rc=execute(a,description(c,cpl),a.out/name/'probe',block)
                errors=[f'prefix/probe error {r0}/{rc}'] if not before or before['stop']!='reached' or not got else judge(c,cpl,before,got)
                rows.append(dict(name=name,errors=errors,oracle_basis=c.get('oracle_basis','documented instruction contract')));print(name,'FAIL' if errors else 'PASS',errors,flush=True)
                (a.out/'results.json').write_text(json.dumps(rows,indent=2)+'\n')
    print(f'{len(rows)} executions, {sum(bool(r["errors"]) for r in rows)} failed',flush=True)
    return int(not rows or any(r['errors'] for r in rows))


if __name__=='__main__':sys.exit(main())
