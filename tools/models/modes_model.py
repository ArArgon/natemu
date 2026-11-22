"""Independent bounded expectations for the mode transitions of 16-, 32- and compatibility-mode code at CPL 0: the
reference the boot-time interpreter's transition and fault cases are checked against (boot-interpreter plan sections
6 and 8), and still the model of five `system` fault cases (`tests/smp/run_slice3_vm.py`).

Vendor profile: Intel. WRMSR EFER raises #GP(0) for a nonzero EDX (EFER bits 63:32 are reserved) and for any EAX bit
outside EFER_DEFINED (SCE, LME, LMA, NXE; AMD would add SVME, LMSLE, FFXSR and TCE); a write to LMA is ignored. These
match the resident's WRMSR (`crates/runtime/src/execution.rs` `system`) and `tests/lib/system_fault_model.py`. The
valid CR4 bits are the CPU profile's (`models/cpu_profile.json`: a bit whose CPUID feature is not advertised, LA57 and
PCIDE among them, is reserved).

Sources: Vol. 2 "MOV - Move to/from Control Registers" (exceptions in real-address, protected and compatibility mode:
PG = 1 with PE = 0 and NW = 1 with CD = 0 are invalid CR0 combinations, #GP(0); a reserved CR4 bit, #GP(0); activating
IA-32e mode with CS.L = 1 or a 16-bit TSS in TR, #GP(0); clearing CR4.PAE with LMA = 1, #GP(0); clearing CR0.PG in
64-bit mode, #GP(0)), "WRMSR", "JMP" (far, real-address and protected mode); Vol. 3A 2.5 "Control Registers", 12.8.5
"Initializing IA-32e Mode" (the 64-bit mode consistency checks: "An attempt is made to enable or disable IA-32e mode
while paging is enabled", PAE before PG, PAE kept while active, CS.L, a 16-bit TSS), 12.8.5.4 "Switching Out of IA-32e
Mode Operation" (clearing PG in compatibility mode clears LMA), 12.9.1 "Switching to Protected Mode" and 12.9.2
"Switching Back to Real-Address Mode" ("if the segment registers are not reloaded, execution continues using the
descriptor attributes loaded during protected mode").

The programs (`programs(profile)`, a list of instruction bytes at SOURCE) start in 32-bit protected mode with paging
off at CPL 0 (CR0 10011H, CS a flat 32-bit segment). Profiles 22-38 are the 32-bit programs parked since owner decision
3 (contracts 28-31, `docs/restructure/programs.md` C23-C39): the activation round trip, PAE paging, and their faults.
Profiles 39-47 cover the boot-time transitions: PE cleared and set (P32 -> P16 -> real mode -> P16 -> P32, the SDM's
12.9.2 order), the invalid CR0 combinations, EFER.LME cleared and set under PG = 0 between two activations, CS.L under
LMA = 0 (ignored by a far JMP, refused by activation), MOV CR4 with Linux's feature bits and PAE under PG = 0, and PG
enabled without LME (legacy paging). Profiles 48 and 49 run LIDT (m16&32: a 16-bit limit, a 32-bit base) and a 32-bit
near RET in compatibility mode (OVMF, kvm-unit-tests; Vol. 2 "LGDT/LIDT", "RET"), and LIDT's #PF there; RET checks the
popped EIP against the CS limit (#GP(0)) on a flat stack at STACK. Every record carries `imode`, the interpreted classification of
`segment_model.interpreted_mode` at the instruction ('R', 'P16', 'P32', 'C16', 'C32', or None for 64-bit code).

Model choices:
M1 A TLB may keep a translation after its PTE is cleared without INVLPG (contracts section 3 item 3): profiles 34 and 35
   clear a PTE and access the page again. `model(..., stale=...)` says, access by access, whether the old entry is used;
   `outcomes(profile)` is every permitted ending, and a checker accepts any of them (natemu's interpreter walks every
   access: the fresh-walk history).
M2 Enabling paging without LME (legacy two-level or PAE paging) is architecturally legal and a typed stop in natemu's
   first version (plan sections 1, 11): its MOV CR0 record carries `stop`. The model goes on with PAE paging
   (`pae_model`, profile 23) and ends the program at legacy two-level paging, which it does not model (profile 46).
M3 Reserved CR0 bits 31:0 are not written and ET reads 1 (KVM: "Write to CR0 reserved bits are ignored, even on
   Intel"; ET is hardwired on P6 and later, 2.5).
M4 A real-mode far JMP checks the offset against the current CS limit and changes the selector and base only
   (`segment_model` B11); the protected-mode far JMP checks of this model are the subset its programs reach (null, TI,
   table limit, type, L = D = 1 only with LMA = 1, present, the new CS limit, a canonical 64-bit target); the full order is
   `segment_model.jmp_far_interp`'s.
M5 The 16-bit-TSS rule on IA-32e activation is an Intel-profile choice (contracts section 3 item 15).
"""
import copy

from models import cpu_profile
from models import pae_model as pae
from models.segment_model import interpreted_mode

_CONTROL = cpu_profile.load().d['control']
CR4_VALID = sum(1 << v['bit'] for v in _CONTROL['cr4']['bits'].values() if v['valid'])
CR0_DEFINED = sum(1 << v['bit'] for v in _CONTROL['cr0']['bits'].values())
LINUX_CR4 = 0x150EB0                 # PSE PAE PGE OSFXSR OSXMMEXCPT UMIP FSGSBASE OSXSAVE SMEP: a trampoline's CR4

DATA=0x100000
SOURCE=0x102000
CS32=[8,0,0xffffffff,0xc09b]
CS64=[0x18,0,0xffffffff,0xa09b]
EFER_DEFINED=0xd01


def programs(profile):
    mov=lambda v:b'\xb8'+v.to_bytes(4,'little')
    cr0,cr3,cr4=[bytes.fromhex(s) for s in ('0f22c0','0f22d8','0f22e0')]
    wr=b'\x0f\x30';load=b'\x8b\x03';wide=b'\x48\x8b\x03'
    far=b'\xea'+bytes(4)+b'\x18\0';pointer=b'\x48\xff\x2e'
    lidt=lambda a:b'\x0f\x01\x1d'+a.to_bytes(4,'little')          # LIDT [disp32] (32-bit code: m16&32)
    start=[mov(0x620),cr4,mov(0x100),wr,mov(0x6000),cr3,mov(0x80010011),cr0]
    cases=[
        [*start,far,wide,pointer,load,mov(0x10011),cr0,load],
        [mov(0x620),cr4,mov(0x80010011),cr0,load,mov(0x10011),cr0,
         mov(0x100),wr,mov(0x6000),cr3,mov(0x80010011),cr0,far,wide,pointer,
         load,mov(0x10011),cr0,load],
        [load,mov(0x80010011),cr0,far,wide,pointer,load,mov(0x10011),cr0,b'\xe9'+bytes(4)],
        [*start,mov(0),wr],
        [mov(0x100),wr,mov(0x80010011),cr0],
        [*start,mov(0x600),cr4],
        [*start,far,mov(0x10011),cr0],
        [mov(0xffffffff),wr],
        [*start,far,far],
        [*start,far[:5]+b'\x38\0'],
        [*start,far,wide],
        [*start,far,wide],
        [*start,far,wide,pointer,load],
        [*start,far,wide],
        [*start,far,wide],
        [*start,far[:5]+b'\x10\0'],
        [*start,far,wide,pointer,wide],
        None,                                                    # 39: assembled below
        [mov(0x80010010),cr0],                                   # 40: PG = 1 with PE = 0
        [mov(0x20010011),cr0],                                   # 41: NW = 1 with CD = 0
        [*start,far,wide,pointer,load,mov(0x10011),cr0,          # 42: deactivation, LME cleared and set under PG = 0,
         mov(0),wr,mov(0x100),wr,mov(0x80010011),cr0,load],      #     activation again
        [mov(0x620),cr4,mov(0x100),wr,mov(0x6000),cr3,           # 43: a far JMP to L = D = 1 under LMA = 0 (32-bit code),
         far[:5]+b'\x30\0',mov(0x80010011),cr0],                 #     then activation with CS.L = 1
        [mov(0x100),wr,mov(LINUX_CR4),cr4,mov(0x600),cr4,        # 44: CR4 features and PAE under PG = 0, then LA57
         mov(LINUX_CR4|1<<12),cr4],
        [*start,mov(0x80010010),cr0],                            # 45: PE cleared with PG = 1 (compatibility mode)
        [mov(0x80010011),cr0,load],                              # 46: PG without LME, PAE = 0: legacy paging
        None,                                                    # 47: assembled below
        [*start,lidt(IDT_POINTER),b'\xc3',load],                 # 48: LIDT and near RET in compatibility mode
        [*start,lidt(DATA+0x1000)],                              # 49: LIDT's operand not mapped: #PF
    ]
    if profile in (39,47):
        return _assemble(_real_programs(profile))
    result=cases[profile-22]
    offset=0
    for index,instruction in enumerate(result):
        offset+=len(instruction)
        if instruction[:1]==b'\xea':
            result[index]=b'\xea'+(SOURCE+offset).to_bytes(4,'little')+instruction[5:]
        elif instruction[:1]==b'\xe9':
            result[index]=b'\xe9'+(-offset).to_bytes(4,'little',signed=True)
    return result


IDT_POINTER=DATA+0x110               # profile 48: LIDT's operand (limit FFFH, base 12345678H)
STACK=DATA+0x300                     # ESP at the start: profile 48's RET pops the next instruction's address there
CODE16=0x20                          # profiles 39-47: a 16-bit code segment, base CODE16_BASE, limit FFFFH
CODE16_BASE=0x100000
LONG_D=0x30                          # profiles 39-47: L = D = 1, a 32-bit code segment while LMA = 0
REAL_CS=0xF800                       # the real-mode CS of profiles 39 and 47 (base F8000H: SOURCE is at offset A000H)


def _real_programs(profile):
    """Profiles 39 and 47: P32 -> P16 -> real mode (PE cleared under the 16-bit CS, then a far JMP ptr16:16 to REAL_CS)
    -> P16 (PE set under the stale real-mode CS) -> P32 (66 EA ptr16:32), as 12.9.1 and 12.9.2 order it; profile 47
    sets CR4.PAE and EFER.LME in real mode and then activates IA-32e mode from P32."""
    mov16=lambda v:b'\x66\xb8'+v.to_bytes(4,'little')
    cr0,cr3,cr4,wr=bytes.fromhex('0f22c0'),bytes.fromhex('0f22d8'),bytes.fromhex('0f22e0'),b'\x0f\x30'
    down=[('jmpf',CODE16,CODE16_BASE,0,4),mov16(0x10010),cr0,('jmpf',REAL_CS,REAL_CS<<4,0,2)]
    up=[mov16(0x10011),cr0,('jmpf',8,0,1,4)]
    if profile==39:
        return [*down,*up,b'\x8b\x03']
    mov=lambda v:b'\xb8'+v.to_bytes(4,'little')
    return [*down,mov16(0x620),cr4,mov16(0x100),wr,*up,mov(0x6000),cr3,mov(0x80010011),cr0,b'\x8b\x03']


def _assemble(items):
    """Instruction bytes; ('jmpf', selector, base, prefix66, offset bytes) is a far JMP to the next instruction, whose
    offset is its linear address less `base`, the target CS's base."""
    out,offset=[],0
    for item in items:
        if isinstance(item,bytes):
            offset+=len(item);out.append(item);continue
        _,selector,base,prefix,width=item
        offset+=prefix+1+width+2
        out.append(b'\x66'*prefix+b'\xea'+(SOURCE+offset-base).to_bytes(width,'little')+selector.to_bytes(2,'little'))
    return out


def far_pointer(instruction,cs):
    """(offset, selector) of a JMP ptr16:16 or ptr16:32 (EA, the 66H prefix flipping CS.D's operand size), or None."""
    prefix=instruction[:1]==b'\x66'
    if instruction[prefix:prefix+1]!=b'\xea':return None
    width=4 if bool(cs[3]&0x4000)!=prefix else 2
    body=instruction[prefix+1:]
    return int.from_bytes(body[:width],'little'),int.from_bytes(body[width:width+2],'little')


def imode(cr0,efer,cs):
    return interpreted_mode(cr0,efer,cs[3],0)


def tables():
    memory=[0]*16384
    for offset,value in ((0,0x7007),(0x1000,0x8007),(0x2000,0x9007),
                         (0x3000+256*8,DATA|7),(0x3000+258*8,SOURCE|7)):
        pae.qword(memory,offset,value)
    return memory


def data(profile,memory):
    descriptors={8:0x00cf9b000000ffff,0x10:0x00cf93000000ffff,
                 0x18:0x00af9b000000ffff,0x28:0x00008b0000000067}
    if profile==32:descriptors[0x18]&=~(1<<47)
    if profile==33:descriptors[0x18]|=1<<54
    if profile>=39:descriptors.update({CODE16:0x00009b000000ffff|CODE16_BASE<<16,LONG_D:0x00ef9b000000ffff})
    memory[0x200:0x238]=[0]*0x38
    for selector,value in descriptors.items():
        pae.qword(memory,0x200+selector,value)
    offset=0;target=SOURCE
    for instruction in programs(profile):
        offset+=len(instruction)
        if instruction==b'\x48\xff\x2e':target=SOURCE+offset;break
    if profile==38:target=0x800000000000
    memory[0x100:0x10a]=target.to_bytes(8,'little')+(0x18 if profile==38 else 8).to_bytes(2,'little')
    if profile>=48:
        offset=0
        for instruction in programs(profile):
            offset+=len(instruction)
            if instruction==b'\xc3':memory[STACK-DATA:STACK-DATA+4]=list((SOURCE+offset).to_bytes(4,'little'))
        memory[IDT_POINTER-DATA:IDT_POINTER-DATA+6]=list((0xFFF).to_bytes(2,'little')+(0x12345678).to_bytes(4,'little'))
    return memory


def mode(cr0,cr4,efer,cs):
    if not cr0&0x80000000:return 0
    if efer&0x400:return 4 if cs[3]&0x2000 else 3
    return 2 if cr4&32 else 1


def empty_visit():
    return dict(cs_before=[0]*4,cs_after=[0]*4,mode_before=0,mode_after=0,
                pointer_address=0,pointer_bytes=[0]*16,descriptor_address=0,descriptor=0)


def empty_state():
    return dict(gdtr=[0]*2,tr=[0]*4,pointer=0,reads=0,pointer_reads=0,
                descriptor_reads=0,mutations=0,visits=[empty_visit() for _ in range(20)])


def walk(memory,linear,write=False,wp=True):
    base=0x6000
    writable=True
    for shift in (39,30,21,12):
        offset=base-0x6000+((linear>>shift)&511)*8
        entry=pae.qword(memory,offset)
        if not entry&1:return None,2 if write else 0
        writable=writable and bool(entry&2)
        if shift==12:
            if write and wp and not writable:return None,3
            pae.qword(memory,offset,entry|32|(64 if write else 0))
            return (entry&0xfffffff000)|(linear&4095),None
        pae.qword(memory,offset,entry|32)
        base=entry&0xfffffff000


def model(profile,initial_eax,memory,edx=0,stale=()):
    """`edx`: EDX throughout the program (WRMSR's high half). `stale` (M1): for each access, in order, to a page whose PTE
    the program cleared without INVLPG, whether it uses the old translation (a TLB that kept it); missing entries walk."""
    source=programs(profile);offsets=[0]
    for instruction in source:offsets.append(offsets[-1]+len(instruction))
    table=tables();pt=pae.tables(13);ps=pae.empty_state();ms=empty_state()
    ms.update(gdtr=[DATA+0x200,0x37],tr=[0x28,0,0x67,0x83 if profile==36 else 0x8b],pointer=DATA+0x100)
    cr0,cr3,cr4,efer=0x10011,0x6000 if profile==24 else 0x1020,0x620 if profile==24 else 0x600,0x100 if profile==24 else 0
    cs=list(CS32);eax=initial_eax;generation=1;index=0;completed=0;esp=STACK
    records=[];mapping=None;walks=0;cleared=set();hits=iter(stale)

    def route(linear,write=False):
        nonlocal walks
        active=mode(cr0,cr4,efer,cs)
        if not active:return linear,None
        if linear>>12 in cleared and next(hits,False):return linear,None      # M1: the old (identity) entry
        walks+=1
        if active==2:return pae.walk(pt,ps['hidden']['entries'],linear,write,bool(cr0&0x10000),counts=ps)
        assert active!=1,'legacy two-level paging is not modelled (M2)'
        return walk(table,linear,write,bool(cr0&0x10000))

    def put(visits,visit):
        if len(records)<len(visits):visits[len(records)]=visit
        else:visits.append(visit)

    while len(records)<(11 if profile==24 else len(source)):
        instruction=source[index];rip=SOURCE+offsets[index]
        pv=pae.empty_visit();pv.update(before=copy.deepcopy(ps['hidden']),attempts_before=ps['attempts'],reads_before=ps['reads'])
        mv=empty_visit();mv.update(cs_before=list(cs),mode_before=mode(cr0,cr4,efer,cs))
        rec=dict(index=index,rip=rip,source=instruction,before=(cr0,cr3,cr4),efer_before=efer,
                 generation_before=generation,eax_before=eax,vector=0,error=0,linear=0,
                 native_fault=False,native_permission=False,gpa=None,cs_before=list(cs),imode=imode(cr0,efer,cs))
        fetch,error=route(rip);rec['fetch_gpa']=fetch
        if error is not None:
            rec.update(vector=14,error=error,linear=rip)
        elif instruction[:1]==b'\xb8' or instruction[:2]==b'\x66\xb8':eax=int.from_bytes(instruction[-4:],'little')
        elif instruction in (b'\x8b\x03',b'\x48\x8b\x03'):
            if mapping is None:
                rec['native_fault']=True
                gpa,error=route(DATA)
                if error is not None:rec.update(vector=14,error=error,linear=DATA)
                else:mapping=(gpa,mode(cr0,cr4,efer,cs)==0)
            if not rec['vector']:
                rec['gpa']=mapping[0];at=mapping[0]-DATA
                rec['memory_before']=memory[at:at+16]
                width=8 if instruction[:1]==b'\x48' else 4
                eax=int.from_bytes(bytes(memory[at:at+width]),'little')
                rec['memory_after']=memory[at:at+16]
        elif instruction==b'\x0f\x30':
            if edx or eax&~EFER_DEFINED or (cr0&0x80000000 and (eax^efer)&0x100):rec['vector']=13
            else:
                efer=(eax&~0x400)|(efer&0x400);generation+=1;mapping=None;cleared.clear()
        elif instruction[:2]==b'\x0f\x22':
            reg=(instruction[2]>>3)&7
            value=eax
            if reg==0:
                if value&0x80000000 and not value&1 or value&1<<29 and not value&1<<30:rec['vector']=13
                value=value&CR0_DEFINED|0x10                                      # M3
            if reg==4 and value&~CR4_VALID:rec['vector']=13
            proposed=list((cr0,cr3,cr4));proposed[{0:0,3:1,4:2}[reg]]=value
            pefer=efer
            if reg==0 and not cr0&0x80000000 and value&0x80000000 and efer&0x100:
                if not cr4&32 or cs[3]&0x2000 or ms['tr'][3]&15 in (1,3):rec['vector']=13
                else:pefer|=0x400
            if reg==0 and cr0&0x80000000 and not value&0x80000000:
                if efer&0x400 and cs[3]&0x2000:rec['vector']=13                  # in 64-bit mode
                else:pefer&=~0x400
            if reg==4 and efer&0x400 and (value^cr4)&32:rec['vector']=13
            if not rec['vector']:
                if not pefer&0x100 and pae.needs_reload((cr0,cr3,cr4),proposed,reg):
                    addresses,values,error=pae.load(pt,proposed[1]);pv['load_addresses']=addresses
                    ps['attempts']+=1;ps['reads']+=4
                    if error is not None:rec['vector']=error
                    else:ps['hidden']=dict(entries=values,generation=ps['hidden']['generation']+1)
                if not rec['vector']:
                    if reg==0 and not cr0&0x80000000 and value&0x80000000 and not efer&0x100:
                        rec['stop']='paging without long mode'                    # M2
                    cr0,cr3,cr4=proposed;efer=pefer;generation+=1;mapping=None;cleared.clear()
        elif instruction[:1]==b'\xe9':pass
        elif instruction[:3]==b'\x0f\x01\x1d':                                 # LIDT m16&32 (32-bit operand size)
            addr=int.from_bytes(instruction[3:],'little')
            gpa,error=route(addr)
            if error is not None:rec.update(vector=14,error=error,linear=addr)
            else:
                raw=bytes(memory[gpa-DATA:gpa-DATA+6])
                ms['idtr']=[int.from_bytes(raw[2:],'little'),int.from_bytes(raw[:2],'little')];rec['idtr_after']=ms['idtr']
        elif instruction==b'\xc3':                                             # RET near, 32-bit (flat SS)
            gpa,error=route(esp)
            if error is not None:rec.update(vector=14,error=error,linear=esp)
            else:
                ret=int.from_bytes(bytes(memory[gpa-DATA:gpa-DATA+4]),'little')
                if ret>cs[2]:rec.update(vector=13,error=0)                     # beyond the CS limit
                else:esp+=4;rec.update(far_target=ret,esp_after=esp)
        else:
            target=selector=None
            if far_pointer(instruction,cs):
                if mode(cr0,cr4,efer,cs)==4:rec['vector']=6
                else:target,selector=far_pointer(instruction,cs)
            else:
                mv['pointer_address']=DATA+0x100
                gpa,error=route(DATA+0x100)
                if error is not None:rec.update(vector=14,error=error,linear=DATA+0x100)
                else:
                    pointer=memory[gpa-DATA:gpa-DATA+10];mv['pointer_bytes'][:10]=pointer
                    ms['reads']+=1;ms['pointer_reads']+=1
                    target=int.from_bytes(bytes(pointer[:8]),'little');selector=int.from_bytes(bytes(pointer[8:]),'little')
            if not rec['vector'] and not cr0&1:                                   # M4: real mode
                if target>cs[2]:rec.update(vector=13,error=0)
                else:cs=[selector,selector<<4,cs[2],cs[3]];generation+=1;mapping=None
            elif not rec['vector']:
                if selector==0 or selector&4 or (selector&~7)+7>ms['gdtr'][1]:
                    rec.update(vector=13,error=selector&~3)
                else:
                    addr=ms['gdtr'][0]+(selector&~7);mv['descriptor_address']=addr
                    gpa,error=route(addr)
                    if error is not None:rec.update(vector=14,error=error,linear=addr)
                    else:
                        desc=pae.qword(memory,gpa-DATA);mv['descriptor']=desc
                        ms['reads']+=1;ms['descriptor_reads']+=1
                        attr=((desc>>40)&255)|((desc>>40)&0xf000)
                        sixty4=efer&0x400 and attr&0x2000
                        if not attr&0x10 or not attr&8:rec.update(vector=13,error=selector&~3)
                        elif efer&0x400 and attr&0x6000==0x6000:rec.update(vector=13,error=selector&~3)
                        elif not attr&0x80:rec.update(vector=11,error=selector&~3)
                        elif sixty4 and target>=0x800000000000 and target<0xffff800000000000:
                            rec.update(vector=13,error=0)
                        elif not sixty4 and target>seglimit(desc):rec.update(vector=13,error=0)
                        else:
                            cs=[selector&~3,segbase(desc),seglimit(desc),attr];generation+=1;mapping=None
            rec['far_target']=target
        next_rip=rip if rec['vector'] else (SOURCE if instruction[:1]==b'\xe9' else
                   cs[1]+rec['far_target'] if 'far_target' in rec else rip+len(instruction))
        rec.update(after=(cr0,cr3,cr4),efer_after=efer,cs_after=list(cs),generation_after=generation,
                   eax_after=eax,next_rip=next_rip,imode_after=imode(cr0,efer,cs))
        pv.update(after=copy.deepcopy(ps['hidden']),attempts_after=ps['attempts'],reads_after=ps['reads'])
        put(ps['visits'],pv)
        mv.update(cs_after=list(cs),mode_after=mode(cr0,cr4,efer,cs));put(ms['visits'],mv)
        records.append(rec)
        if rec['vector'] or mode(cr0,cr4,efer,cs)==1:break                         # M2: legacy paging ends the model
        completed+=1
        if profile==34 and instruction==b'\x48\x8b\x03':
            pae.qword(table,0x3000+256*8,0);ms['mutations']+=1;cleared.add(DATA>>12)
        if profile==35 and instruction[:1]==b'\xea':
            pae.qword(table,0x3000+258*8,0);ms['mutations']+=1;cleared.add(SOURCE>>12)
        index=0 if instruction[:1]==b'\xe9' else index+1
        if index==len(source):break
    return dict(records=records,memory=memory,pae_tables=pt,pae=ps,modes=ms,mode_tables=table,
                completed=completed,walks=walks,generation=generation,mapping=mapping)


def segbase(desc):
    return (desc>>16)&0xffffff|(desc>>56&0xff)<<24


def seglimit(desc):
    limit=desc&0xffff|(desc>>48&0xf)<<16
    return limit<<12|0xfff if desc>>55&1 else limit


PROFILES=range(22,50)


def ending(result):
    """(records, last index, vector, error, stop) of a model result: what a checker compares."""
    last=result['records'][-1]
    return len(result['records']),last['index'],last['vector'],last['error'],last.get('stop')


def outcomes(profile,edx=0):
    """Every ending the SDM permits (M1): each access to a page whose PTE was cleared without INVLPG may use the old
    translation or walk afresh. The first is the every-access-walks history (natemu's interpreter's)."""
    run=lambda stale:ending(model(profile,0,data(profile,[0]*0x400),edx,stale))
    found=[run(())]
    if profile in (34,35):
        for n in range(1,4):
            for k in range(1<<n):
                e=run(tuple(bool(k>>i&1) for i in range(n)))
                if e not in found:found.append(e)
    return found
