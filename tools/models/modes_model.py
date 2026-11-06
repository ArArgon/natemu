"""Independent bounded compatibility/long-mode transition expectations.

Vendor profile: Intel. WRMSR EFER raises #GP(0) for a nonzero EDX (EFER bits 63:32 are reserved) and for any EAX bit
outside EFER_DEFINED (SCE, LME, LMA, NXE; AMD would add SVME, LMSLE, FFXSR and TCE); a write to LMA is ignored. These
match the resident's WRMSR (`crates/runtime/src/execution.rs` `system`) and `tools/system_fault_model.py`.

The programs start in 32-bit protected mode (contracts 28-31), which the general path does not run: owner decision 3
moved 32-bit support to the boot phase, so the rest of contracts §3's fixes wait for it there. Open for that reuse:
profiles 34 and 35 require a fresh walk after a PTE write without INVLPG (§3 item 3), where a TLB may keep the old
entry, so a checker must accept either history (`coherence_model.access_outcomes`/`check_observation`); the 16-bit-TSS
rule on IA-32e activation is an Intel-profile choice (§3 item 15).
"""
import copy

from models import pae_model as pae

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
    ]
    result=cases[profile-22]
    offset=0
    for index,instruction in enumerate(result):
        offset+=len(instruction)
        if instruction[:1]==b'\xea':
            result[index]=b'\xea'+(SOURCE+offset).to_bytes(4,'little')+instruction[5:]
        elif instruction[:1]==b'\xe9':
            result[index]=b'\xe9'+(-offset).to_bytes(4,'little',signed=True)
    return result


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
    memory[0x200:0x238]=[0]*0x38
    for selector,value in descriptors.items():
        pae.qword(memory,0x200+selector,value)
    offset=0;target=SOURCE
    for instruction in programs(profile):
        offset+=len(instruction)
        if instruction==b'\x48\xff\x2e':target=SOURCE+offset;break
    if profile==38:target=0x800000000000
    memory[0x100:0x10a]=target.to_bytes(8,'little')+(0x18 if profile==38 else 8).to_bytes(2,'little')
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


def model(profile,initial_eax,memory,edx=0):
    """`edx`: EDX throughout the program (WRMSR's high half)."""
    source=programs(profile);offsets=[0]
    for instruction in source:offsets.append(offsets[-1]+len(instruction))
    table=tables();pt=pae.tables(13);ps=pae.empty_state();ms=empty_state()
    ms.update(gdtr=[DATA+0x200,0x37],tr=[0x28,0,0x67,0x83 if profile==36 else 0x8b],pointer=DATA+0x100)
    cr0,cr3,cr4,efer=0x10011,0x6000 if profile==24 else 0x1020,0x620 if profile==24 else 0x600,0x100 if profile==24 else 0
    cs=list(CS32);eax=initial_eax;generation=1;index=0;completed=0
    records=[];mapping=None;walks=0

    def route(linear,write=False):
        nonlocal walks
        active=mode(cr0,cr4,efer,cs)
        if not active:return linear,None
        walks+=1
        if active==2:return pae.walk(pt,ps['hidden']['entries'],linear,write,bool(cr0&0x10000),counts=ps)
        return walk(table,linear,write,bool(cr0&0x10000))

    while len(records)<(11 if profile==24 else len(source)):
        instruction=source[index];rip=SOURCE+offsets[index]
        pv=pae.empty_visit();pv.update(before=copy.deepcopy(ps['hidden']),attempts_before=ps['attempts'],reads_before=ps['reads'])
        mv=empty_visit();mv.update(cs_before=list(cs),mode_before=mode(cr0,cr4,efer,cs))
        rec=dict(index=index,rip=rip,source=instruction,before=(cr0,cr3,cr4),efer_before=efer,
                 generation_before=generation,eax_before=eax,vector=0,error=0,linear=0,
                 native_fault=False,native_permission=False,gpa=None,cs_before=list(cs))
        fetch,error=route(rip);rec['fetch_gpa']=fetch
        if error is not None:
            rec.update(vector=14,error=error,linear=rip)
        elif instruction[:1]==b'\xb8':eax=int.from_bytes(instruction[1:],'little')
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
                efer=(eax&~0x400)|(efer&0x400);generation+=1;mapping=None
        elif instruction[:2]==b'\x0f\x22':
            reg=(instruction[2]>>3)&7
            proposed=list((cr0,cr3,cr4));proposed[{0:0,3:1,4:2}[reg]]=eax
            pefer=efer
            if reg==0 and not cr0&0x80000000 and eax&0x80000000 and efer&0x100:
                if not cr4&32 or cs[3]&0x2000 or ms['tr'][3]&15 in (1,3):rec['vector']=13
                else:pefer|=0x400
            if reg==0 and cr0&0x80000000 and not eax&0x80000000:
                if cs[3]&0x2000:rec['vector']=13
                else:pefer&=~0x400
            if reg==4 and efer&0x400 and (eax^cr4)&32:rec['vector']=13
            if not rec['vector']:
                if not pefer&0x100 and pae.needs_reload((cr0,cr3,cr4),proposed,reg):
                    addresses,values,error=pae.load(pt,proposed[1]);pv['load_addresses']=addresses
                    ps['attempts']+=1;ps['reads']+=4
                    if error is not None:rec['vector']=error
                    else:ps['hidden']=dict(entries=values,generation=ps['hidden']['generation']+1)
                if not rec['vector']:
                    cr0,cr3,cr4=proposed;efer=pefer;generation+=1;mapping=None
        elif instruction[:1]==b'\xe9':pass
        else:
            target=selector=None
            if instruction[:1]==b'\xea':
                if mode(cr0,cr4,efer,cs)==4:rec['vector']=6
                else:target=int.from_bytes(instruction[1:5],'little');selector=int.from_bytes(instruction[5:7],'little')
            else:
                mv['pointer_address']=DATA+0x100
                gpa,error=route(DATA+0x100)
                if error is not None:rec.update(vector=14,error=error,linear=DATA+0x100)
                else:
                    pointer=memory[gpa-DATA:gpa-DATA+10];mv['pointer_bytes'][:10]=pointer
                    ms['reads']+=1;ms['pointer_reads']+=1
                    target=int.from_bytes(bytes(pointer[:8]),'little');selector=int.from_bytes(bytes(pointer[8:]),'little')
            if not rec['vector']:
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
                        if not attr&0x10 or not attr&8:rec.update(vector=13,error=selector&~3)
                        elif attr&0x6000==0x6000:rec.update(vector=13,error=selector&~3)
                        elif not attr&0x80:rec.update(vector=11,error=selector&~3)
                        elif target>=0x800000000000 and target<0xffff800000000000:
                            rec.update(vector=13,error=0)
                        else:
                            cs=[selector&~3,0,0xffffffff,attr];generation+=1;mapping=None
            rec['far_target']=target
        next_rip=rip if rec['vector'] else (SOURCE if instruction[:1]==b'\xe9' else
                   rec['far_target'] if 'far_target' in rec else rip+len(instruction))
        rec.update(after=(cr0,cr3,cr4),efer_after=efer,cs_after=list(cs),generation_after=generation,
                   eax_after=eax,next_rip=next_rip)
        pv.update(after=copy.deepcopy(ps['hidden']),attempts_after=ps['attempts'],reads_after=ps['reads'])
        ps['visits'][len(records)]=pv
        mv.update(cs_after=list(cs),mode_after=mode(cr0,cr4,efer,cs));ms['visits'][len(records)]=mv
        records.append(rec)
        if rec['vector']:break
        completed+=1
        if profile==34 and instruction==b'\x48\x8b\x03':
            pae.qword(table,0x3000+256*8,0);ms['mutations']+=1
        if profile==35 and instruction[:1]==b'\xea':
            pae.qword(table,0x3000+258*8,0);ms['mutations']+=1
        index=0 if instruction[:1]==b'\xe9' else index+1
        if index==len(source):break
    return dict(records=records,memory=memory,pae_tables=pt,pae=ps,modes=ms,mode_tables=table,
                completed=completed,walks=walks,generation=generation,mapping=mapping)
