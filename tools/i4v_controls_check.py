#!/usr/bin/env python3
"""Independent oracle for bounded actual-source protected32 control transitions.

MOV CR is a resident semantic transaction. Scalar MOV instructions are actual
FEX bodies. Arithmetic flags retained across MOV CR are a declared emulator
choice: the architecture leaves those arithmetic flags undefined on success.
"""
import argparse
import copy
import json
from pathlib import Path

from check_i1 import resident_pc
from i4v_check import exact, require, uint, zero_frame
from check_i4v_alias import descriptor

PRIVATE = 0x0000800040000000
DATA = 0x100000
SOURCE = 0x102000
CODE = PRIVATE + 0x30000
RET = bytes.fromhex('c0035fd6')
NAMES = ('roundtrip','reload','wp','bad-pg','bad-nw','bad-cr4','cpl3','fetch-np',
         'ignored-cr3','operand-mod','lock','reserved-cr1','pge')


def immediate(value):
    return b'\xb8'+value.to_bytes(4,'little')


def program(profile):
    mov = immediate
    store, load = bytes.fromhex('8903'), bytes.fromhex('8b03')
    cr0, cr3, cr4 = [bytes.fromhex(s) for s in ('0f22c0','0f22d8','0f22e0')]
    prefix = [mov(0x10011),store]
    programs = [
        [*prefix,mov(0x80010011),cr0,load,mov(0x3000),cr3,load,mov(0x1000),cr3,load,mov(0x10011),cr0,load],
        [mov(0x80010011),cr0,load,mov(0x1000),cr3,load],
        [mov(0x80000011),cr0,mov(0x10011),store,mov(0x80010011),cr0,mov(0xffffffff),store],
        [*prefix,mov(0x80010010),cr0],
        [*prefix,mov(0x20010011),cr0],
        [*prefix,mov(0xffffffff),cr4],
        [*prefix,mov(0x80010011),cr0],
        [*prefix,mov(0x80010011),cr0,mov(0x3000),cr3,load],
        [mov(0x1003),cr3,mov(0x80010011),cr0,load],
        [mov(0x80010011),bytes.fromhex('660f2200'),load],
        [*prefix,mov(0x80010011),bytes.fromhex('f00f22c0')],
        [*prefix,mov(0x80010011),bytes.fromhex('0f22c8')],
        [mov(0x80010011),cr0,load,mov(0x680),cr4,load,mov(0x600),cr4,load],
    ]
    result=programs[profile]
    if profile==0:
        result.append(b'\xe9'+(-(sum(map(len,result))+5)).to_bytes(4,'little',signed=True))
    return result


def positions(profile):
    offsets=[0]
    for instruction in program(profile): offsets.append(offsets[-1]+len(instruction))
    return offsets


def body(instruction):
    if instruction==bytes.fromhex('8903'): return bytes.fromhex('1f2003d5c4fc9f88')
    if instruction==bytes.fromhex('8b03'): return bytes.fromhex('c4fcdf881f2003d5')
    require(instruction[:1]==b'\xb8' and len(instruction)==5,'only admitted FEX scalar forms')
    value=int.from_bytes(instruction[1:],'little')
    if value==0xffffffff: return bytes.fromhex('04008012')
    words=[0x52800004 | (value&0xffff)<<5]
    if value>>16: words.append(0x72a00004 | (value>>16)<<5)
    return b''.join(word.to_bytes(4,'little') for word in words)


def tables(profile):
    words={0:0x2007,0x1000+256*4:DATA|(5 if profile==2 else 7),
           0x1000+258*4:SOURCE|7,0x2000:0x4007,
           0x3000+256*4:(DATA+4096)|7,0x3000+258*4:0 if profile==7 else SOURCE|7}
    result=[0]*16384
    for offset,word in words.items(): result[offset:offset+4]=word.to_bytes(4,'little')
    return result


def word(memory,offset,value=None):
    if value is None: return int.from_bytes(bytes(memory[offset:offset+4]),'little')
    memory[offset:offset+4]=value.to_bytes(4,'little')


def translate(table,cr0,cr3,linear,write=False,cpl=0):
    """Finite two-level x86 answer, no resident imports or reported routes."""
    if cr0>>31==0: return linear,None
    directory=(cr3&0xfffff000)-0x1000+(linear>>22)*4
    pde=word(table,directory)
    error=(2 if write else 0)|(4 if cpl else 0)
    if not pde&1: return None,error
    word(table,directory,pde|32)
    leaf=(pde&0xfffff000)-0x1000+((linear>>12)&1023)*4
    pte=word(table,leaf)
    if not pte&1:return None,error
    user=bool(pde&pte&4)
    writable=bool(pde&pte&2)
    if (cpl and not user) or (write and not writable and (cpl or cr0&0x10000)):
        return None,error|1
    word(table,leaf,pte|32|(64 if write else 0))
    return (pte&0xfffff000)|(linear&4095),None


def control(instruction,value,cr0,cr3,cr4,cpl):
    """Return changed control tuple or exact architectural #GP/#UD result."""
    raw=instruction
    if raw[:1]==b'\xf0':return None,6
    if raw[:1]==b'\x66':raw=raw[1:]
    require(len(raw)==3 and raw[:2]==b'\x0f\x22','admitted MOV-to-control encoding')
    selected=(raw[2]>>3)&7  # mod ignored, register width remains32
    if selected not in (0,3,4):return None,6
    if cpl:return None,13
    if selected==0:
        if (value&0x80000000 and not value&1) or (value&0x20000000 and not value&0x40000000):
            return None,13
        cr0=value
    elif selected==3:cr3=value
    else:
        # Only this finite instruction vector writes a reserved CR4 field.
        if value==0xffffffff:return None,13
        cr4=value
    return (cr0,cr3,cr4),None


def request(vm,generation,paged,rip,instruction,serial):
    native=body(instruction)
    return [0x49345651,1,vm,0,1,1,generation,rip,paged,
            int.from_bytes(instruction,'little'),len(instruction),rip,serial,len(native),0,0]


def reply(packet,instruction):
    out=list(packet);out[0]=0x49345652
    out[14]=int.from_bytes(body(instruction),'little')
    return out


def data_pages():
    result=[0xa5]*4096+[0x5a]*4096
    result[:4]=(0x11220000).to_bytes(4,'little')
    result[4096:4100]=(0x33440000).to_bytes(4,'little')
    return result


def model(profile,initial_eax):
    """Instruction-order architectural reference, independent of dispatch layout."""
    source=program(profile);offsets=positions(profile)
    memory=data_pages();table=tables(profile)
    cr0,cr3,cr4=0x10011,0x1000,0x600
    eax=initial_eax;generation=1;index=0;completed=0;records=[]
    mapping=None;walks=0
    while len(records)<(17 if profile==0 else len(source)):
        instruction=source[index];rip=SOURCE+offsets[index]
        rec=dict(index=index,rip=rip,source=instruction,before=(cr0,cr3,cr4),generation_before=generation,
                 eax_before=eax,vector=0,error=0,linear=0,native_fault=False,gpa=None)
        walks+=int(bool(cr0&0x80000000))
        fetch,error=translate(table,cr0,cr3,rip,cpl=3 if profile==6 else 0)
        rec['fetch_gpa']=fetch
        if error is not None:
            rec.update(vector=14,error=error,linear=rip)
        elif instruction[:1]==b'\xb8':
            eax=int.from_bytes(instruction[1:],'little')
        elif instruction in (bytes.fromhex('8903'),bytes.fromhex('8b03')):
            write=instruction[0]==0x89
            if mapping is None or (write and not mapping[1]):
                rec['native_fault']=True
                walks+=int(bool(cr0&0x80000000))
                gpa,error=translate(table,cr0,cr3,DATA,write=write,cpl=3 if profile==6 else 0)
                if error is not None:rec.update(vector=14,error=error,linear=DATA)
                else:
                    # Identity owns RAM directly; paging requires permission and D.
                    leaf=((cr3&0xfffff000)-0x1000+0x1000+256*4)
                    rights=not cr0&0x80000000 or bool(word(table,leaf)&64 and
                        ((word(table,leaf)&2) or not cr0&0x10000))
                    mapping=(gpa,rights)
            if not rec['vector']:
                rec['gpa']=mapping[0]
                at=mapping[0]-DATA
                rec['memory_before']=memory[at:at+16]
                if write:memory[at:at+4]=eax.to_bytes(4,'little')
                else:eax=int.from_bytes(bytes(memory[at:at+4]),'little')
                rec['memory_after']=memory[at:at+16]
        elif instruction[:1]==b'\xe9':
            pass
        else:
            controls,vector=control(instruction,eax,cr0,cr3,cr4,3 if profile==6 else 0)
            if vector:rec.update(vector=vector)
            else:
                cr0,cr3,cr4=controls
                generation+=1;mapping=None
        rec.update(after=(cr0,cr3,cr4),generation_after=generation,eax_after=eax,
                   next_rip=rip if rec['vector'] else (SOURCE if instruction[:1]==b'\xe9' else rip+len(instruction)))
        records.append(rec)
        if rec['vector']:break
        completed+=1
        if profile==1 and index==2:
            # Fixture authority changes memory without silently dropping the warm route.
            word(table,0x1400,(DATA+4096)|7)
        index=0 if instruction[:1]==b'\xe9' else index+1
        if index==len(source):break
    return dict(records=records,memory=memory,tables=table,completed=completed,walks=walks,
                generation=generation,mapping=mapping)


def seed():
    from i4v_off_check import native_seed,guest_seed
    frame,bank=native_seed()
    guest=guest_seed(frame,SOURCE)
    guest['control'][0]=0x10011
    return frame,bank,guest


def guest_before(profile):
    guest=seed()[2]
    if profile==6:
        guest['segments'][0]=[0xb,0,0xffffffff,0xc0fb]
        guest['segments'][1:]=[[0x13,0,0xffffffff,0xc0f3] for _ in range(7)]
    return guest


def guest_after(profile,answer):
    guest=guest_before(profile)
    last=answer['records'][-1]
    guest['gpr'][0]=last['eax_after']
    guest['control'][0],guest['control'][2],guest['control'][3]=last['after']
    if last['vector']==14:guest['control'][1]=last['linear']
    guest['rip']=last['next_rip']
    return guest


def expectations(profile,vm,capture):
    """Build full observations from literal instruction semantics and fixed seeds."""
    initial,bank,_=seed();guest=guest_before(profile)
    answer=model(profile,guest['gpr'][0])
    current=copy.deepcopy(initial);entered=copy.deepcopy(initial)
    visits=[];code=[0]*640;serial=0;packets=[];replies=[]
    deferred=late=rebound=None
    expected=[0]*16
    for number,rec in enumerate(answer['records']):
        instruction=rec['source'];scalar=instruction[:1]==b'\xb8' or instruction in (b'\x89\x03',b'\x8b\x03')
        fetched=rec['fetch_gpa'] is not None
        before=copy.deepcopy(guest)
        v=dict(rip=rec['rip'],bytes=len(instruction) if fetched else 0,
               kind=0 if scalar or not fetched else (2 if instruction[:1]==b'\xe9' else 1),
               source=int.from_bytes(instruction,'little') if fetched else 0,
               fetch_gpa=rec['fetch_gpa'] or 0,before=before['control'],after=None,
               generation_before=rec['generation_before'],generation_after=rec['generation_after'],
               gpr_before=before['gpr'][:4],gpr_after=None,flags_before=before['rflags'],flags_after=before['rflags'],
               frame=None,fault_frame=zero_frame(),exception=[0]*4,request=[0]*16,reply=[0]*16,
               entries=0,faults=int(rec['native_fault']),gpa=rec['gpa'] or 0,
               memory_before=rec.get('memory_before',[0]*16),memory_after=rec.get('memory_after',[0]*16),
               vector=rec['vector'],error=rec['error'],linear=rec['linear'])
        if scalar and fetched:
            serial+=1
            p=request(vm,rec['generation_before'],rec['before'][0]>>31,rec['rip'],instruction,serial)
            q=reply(p,instruction)
            packets.append(p);replies.append(q);expected=p
            if number==15 and profile==0:
                late=reply(deferred,program(0)[0]);rebound=list(late);rebound[12]=serial
            v['request'],v['reply'],v['entries']=p,q,1
            native=body(instruction)+RET
            code[number*32:number*32+len(native)]=native
            entered=copy.deepcopy(current);entered['x'][16]=CODE+number*32;entered['x'][30]=capture
            current=copy.deepcopy(entered)
            if rec['native_fault']:
                v['fault_frame']=copy.deepcopy(entered)
                v['exception']=[(0x25<<26)|(64 if instruction[0]==0x89 else 0)|7,DATA,
                                CODE+number*32+(4 if instruction[0]==0x89 else 0),entered['nzcv']|0x3c4]
            current['x'][4]=rec['eax_after']
            if number==0 and profile==0:
                serial+=1;deferred=request(vm,1,0,SOURCE,instruction,serial)
                packets.append(deferred);replies.append(reply(deferred,instruction))
                expected=deferred
        guest['control'][0],guest['control'][2],guest['control'][3]=rec['after']
        if rec['vector']==14:guest['control'][1]=rec['linear']
        guest['gpr'][0]=rec['eax_after'];guest['rip']=rec['next_rip']
        v['after']=list(guest['control']);v['gpr_after']=guest['gpr'][:4]
        v['frame']=copy.deepcopy(current);visits.append(v)
    empty={key:(zero_frame() if key in ('frame','fault_frame') else
                [0]*len(value) if isinstance(value,list) else 0) for key,value in visits[0].items()}
    visits.extend(copy.deepcopy(empty) for _ in range(20-len(visits)))
    state=dict(profile=profile,cpl=3 if profile==6 else 0,generation=answer['generation'],
               visits=len(answer['records']),completed=answer['completed'],transfers=serial,serial=serial,
               walks=answer['walks'],invalidations=answer['generation']-1,
               mapping=answer['mapping'][0] if answer['mapping'] else 0,
               fixture_mutated=int(profile==1),warm_before_reload=descriptor(0) if profile==1 else 0,
               stale_reply=2 if profile==0 else 0,stale_entry=int(profile==0),expected=expected,
               deferred=deferred or [0]*16,late_reply=late or [0]*16,rebound_reply=rebound or [0]*16,
               old_slot_context=int(profile==0),initial=initial,current=current,
               old_controls=guest_before(profile)['control'])
    # Common host setup retains a native RW/NX source-page alias. Controls
    # invalidate the operand leaf only; this source alias is not a guest PTE.
    lower_before=[0]*512
    lower_before[258]=descriptor(2,False)
    leaves=list(lower_before)
    if answer['mapping']:
        gpa,writable=answer['mapping'];leaves[256]=descriptor((gpa-DATA)//4096,not writable)
    source_page=[0xcc]*4096
    source=b''.join(program(profile));source_page[:len(source)]=source
    return dict(state=state,visits=visits,initial=initial,entered=entered,final_frame=current,
                guest_before=guest_before(profile),guest_after=guest,
                code_before=[0]*640,code_after=code,data_before=data_pages(),data_after=answer['memory'],
                tables_before=tables(profile),tables_after=answer['tables'],
                source_before=source_page,source_after=list(source_page),lower_leaf_before=lower_before,lower_leaf=leaves,
                compile_requests=packets,compile_replies=replies,bank=bank,
                stage=3 if answer['records'][-1]['vector'] else 2)


def check(record,expected_profile=None):
    require(type(record) is dict,'expected one controls report')
    profile=uint(record['profile'],32,'profile')
    require(profile<13,'finite transition profile')
    if expected_profile is not None:exact(profile,expected_profile,'requested program profile')
    vm=uint(record['vm_nonce'],64,'VM incarnation');require(vm>0,'nonzero VM identity')
    capture=uint(record['capture_pc'],64,'capture PC')
    require(resident_pc(capture),'capture outside admitted resident text')
    require(resident_pc(uint(record['service_pc'],64,'service PC')),'service outside admitted resident text')
    expected=expectations(profile,vm,capture)
    controls=expected['state']
    for field,value in (('case','controls32'),('rip',SOURCE),('status',1),('detail',0),('live',1),
                        ('current_el',4),('tcr',0x2b5103510),('ttbr1',0x40017000),
                        ('requests',controls['serial']),('request',[0]*16),('reply',[0]*16),
                        ('completed',controls['completed']),('stage',expected['stage']),('operand',0),('width',0)):
        exact(record[field],value,field)
    require(uint(record['sctlr'],64,'SCTLR')&0x1005==0x1005,'native MMU/cache settings')
    require(uint(record['sctlr'],64,'SCTLR')&2==0,'admitted native unaligned access setting')
    require(uint(record['actlr'],64,'ACTLR')&2==2,'native ordering setting')
    for field in ('initial','entered','final_frame','guest_before','guest_after','code_before','code_after',
                  'data_before','data_after','tables_before','tables_after','source_before','source_after'):
        exact(record[field],expected[field],field)
    exact(record['controls'],controls,'all context, cache, pending and progress state')
    exact(record['bank_before'],expected['bank'],'initial private bank')
    exact(record['compiler_requests'],expected['compile_requests'],'actual compile allocation order')
    exact(record['lower_leaf_before'],expected['lower_leaf_before'],'initial operand absent and source alias retained')
    exact(record['lower_leaf_after'],expected['lower_leaf'],'exact final native mapping rights and GPA')
    require(type(record['visits']) is list and len(record['visits'])==20,'complete bounded visit storage')
    last_exception=[0]*4
    for index,(observed,wanted) in enumerate(zip(record['visits'],expected['visits'])):
        if wanted['faults']:
            exception=observed['exception']
            require(type(exception) is list and len(exception)==4,'native exception tuple')
            esr,far,pc,spsr=[uint(v,64,'native exception word') for v in exception]
            exact(esr>>26,0x25,'same-EL data abort')
            write=(wanted['source']&255)==0x89
            exact((esr>>6)&1,int(write),'native read/write intent')
            require(esr&63 in range(4,8),'fault must use the invalidated/cold native mapping')
            exact(esr&((1<<10)|(1<<8)|(1<<7)),0,'direct valid native FAR')
            exact(far,DATA,'guest operand native address')
            exact(pc,CODE+index*32+(4 if write else 0),'actual generated memory instruction')
            exact(spsr&0xf0000000,expected['initial']['nzcv'],'native flags at fault')
            exact(spsr&0x3df,0x3c4,'masked native EL1t')
            wanted['exception']=list(exception)
            last_exception=list(exception)
        exact(observed,wanted,f'visit{index} precise source/context/control/native/memory effects')
    exact([record[field] for field in ('esr','far','elr','spsr')],last_exception,'last native exception association')
    last=expected['visits'][controls['visits']-1]
    recovery={'capture_pc':capture,'guest_before':expected['guest_before'],'guest_after':expected['guest_after'],
              'control':[0x1000,0,1,0,0],'faults':0,'walks':0,'retries':0,'traces':[],
              'vector':last['vector'],'error':last['error'],'linear':last['linear'],
              'rip':last['rip'] if last['vector'] else 0,
              'tables_before':expected['tables_before'],'tables_after':expected['tables_after'],
              'memory_after':expected['data_after'][:256],'bank_after':expected['bank']}
    exact(record['recovery'],recovery,'canonical exception and preserved full banks/tables')
    return expected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports',nargs='+',type=Path)
    parser.add_argument('--profile',type=int,choices=range(13))
    args=parser.parse_args()
    for path in args.reports:
        try:check(json.loads(path.read_text()),args.profile)
        except (OSError,ValueError,TypeError,KeyError) as error:
            raise SystemExit(f'{path}: FAIL: {error}') from error
        print(f'{path}: PASS (finite protected32 control transitions)')


if __name__=='__main__':main()
