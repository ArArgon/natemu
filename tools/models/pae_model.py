"""Independent legacy-PAE semantics for the finite control campaign.

The hidden four-entry snapshot belongs to guest state. Translation caches and
the backing PDPT bytes cannot substitute for it.
"""

DATA = 0x100000
SOURCE = 0x102000
ROOT = 0x1020
RESERVED = 0xffffff00000001e6  # Selected 40-bit physical-address profile.


def qword(memory, offset, value=None):
    if value is None:
        return int.from_bytes(bytes(memory[offset:offset + 8]), 'little')
    memory[offset:offset + 8] = value.to_bytes(8, 'little')


def tables(profile):
    memory = [0] * 20480
    entries = {0x20: 0x2001, 0x1000: 0x3007, 0x3000: 0x5007,
               0x2000 + 256 * 8: DATA | 7,
               0x2000 + 258 * 8: SOURCE | 7,
               0x4000 + 256 * 8: (DATA + 4096) | 7,
               0x4000 + 258 * 8: SOURCE | 7}
    if profile == 17:
        entries[0x38] = 0x21
    if profile == 20:
        entries[0x20] = 0xfffffffffffffffe
    for offset, value in entries.items():
        qword(memory, offset, value)
    return memory


def needs_reload(before, after, register):
    """CR tuples are (CR0, CR3, CR4), with EFER.LME fixed zero."""
    if not (after[0] & (1 << 31) and after[2] & (1 << 5)):
        return False
    return (register == 3 or bool((before[0] ^ after[0]) & 0xe0000000)
            or bool((before[2] ^ after[2]) & 0x1000b0))


def load(memory, cr3):
    """Selected read-all-then-validate policy, never a partial publication."""
    addresses = [(cr3 & 0xffffffe0) + i * 8 for i in range(4)]
    values = [qword(memory, address - 0x1000) for address in addresses]
    invalid = any(value & 1 and value & RESERVED for value in values)
    return addresses, values, 13 if invalid else None


def walk(memory, hidden, linear, write=False, wp=True, cpl=0, counts=None):
    """Only lower PDE/PTE reads and A/D effects; no PDPT memory access."""
    error = (2 if write else 0) | (4 if cpl else 0)
    root = hidden[linear >> 30]
    if not root & 1:
        return None, error
    directory = (root & 0xfffffff000) - 0x1000 + ((linear >> 21) & 511) * 8
    pde = qword(memory, directory)
    if counts is not None:
        counts['walk_reads'] += 1
    if not pde & 1:
        return None, error
    qword(memory, directory, pde | 32)
    if counts is not None and not pde & 32:
        counts['walk_writes'] += 1
    leaf = (pde & 0xfffffff000) - 0x1000 + ((linear >> 12) & 511) * 8
    pte = qword(memory, leaf)
    if counts is not None:
        counts['walk_reads'] += 1
    if not pte & 1:
        return None, error
    if ((cpl and not pde & pte & 4)
            or (write and not pde & pte & 2 and (wp or cpl))):
        return None, error | 1
    qword(memory, leaf, pte | 32 | (64 if write else 0))
    if counts is not None and pte != pte | 32 | (64 if write else 0):
        counts['walk_writes'] += 1
    return (pte & 0xfffffff000) | (linear & 4095), None


def writable(memory, hidden, linear, wp):
    root = hidden[linear >> 30]
    directory = (root & 0xfffffff000) - 0x1000 + ((linear >> 21) & 511) * 8
    pde = qword(memory, directory)
    leaf = (pde & 0xfffffff000) - 0x1000 + ((linear >> 12) & 511) * 8
    pte = qword(memory, leaf)
    return bool(pte & 64 and (pde & pte & 2 or not wp))


def empty_visit():
    return dict(before=dict(entries=[0]*4,generation=0),
                after=dict(entries=[0]*4,generation=0),load_addresses=[0]*4,
                attempts_before=0,attempts_after=0,reads_before=0,reads_after=0)


def empty_state():
    return dict(hidden=dict(entries=[0]*4,generation=0),attempts=0,reads=0,
                walk_reads=0,walk_writes=0,invlpg=0,evictions=0,mutations=0,
                handled_faults=0,visits=[empty_visit() for _ in range(20)])


def model(profile, initial_eax, source, memory):
    """Literal selected program semantics; no runtime or observed-state imports."""
    import copy
    offsets=[0]
    for instruction in source:
        offsets.append(offsets[-1]+len(instruction))
    table=tables(profile)
    state=empty_state()
    cr0,cr3,cr4=0x10011,ROOT,0x600
    eax=initial_eax;generation=1;index=0;completed=0;records=[]
    mapping=None;walks=0;handled=None
    while len(records)<(12 if profile==21 else len(source)):
        instruction=source[index];rip=SOURCE+offsets[index]
        pv=empty_visit();pv['before']=copy.deepcopy(state['hidden'])
        pv['attempts_before']=state['attempts'];pv['reads_before']=state['reads']
        rec=dict(index=index,rip=rip,source=instruction,before=(cr0,cr3,cr4),
                 generation_before=generation,eax_before=eax,vector=0,error=0,
                 linear=0,native_fault=False,native_permission=False,gpa=None)
        active=bool(cr0&0x80000000)
        if active:
            walks+=1
            fetch,error=walk(table,state['hidden']['entries'],rip,counts=state)
        else:
            fetch,error=rip,None
        rec['fetch_gpa']=fetch
        if error is not None:
            rec.update(vector=14,error=error,linear=rip)
        elif instruction[:1]==b'\xb8':
            eax=int.from_bytes(instruction[1:],'little')
        elif instruction in (b'\x89\x03',b'\x8b\x03'):
            write=instruction[0]==0x89
            if mapping is None or (write and not mapping[1]):
                rec['native_fault']=True
                rec['native_permission']=mapping is not None
                if active:
                    walks+=1
                    gpa,error=walk(table,state['hidden']['entries'],DATA,write=write,
                                   wp=bool(cr0&0x10000),counts=state)
                else:
                    gpa,error=DATA,None
                if error is not None:
                    rec.update(vector=14,error=error,linear=DATA)
                else:
                    rights=not active or writable(table,state['hidden']['entries'],DATA,bool(cr0&0x10000))
                    mapping=(gpa,rights)
            if not rec['vector']:
                rec['gpa']=mapping[0];at=mapping[0]-DATA
                rec['memory_before']=memory[at:at+16]
                if write:memory[at:at+4]=eax.to_bytes(4,'little')
                else:eax=int.from_bytes(bytes(memory[at:at+4]),'little')
                rec['memory_after']=memory[at:at+16]
        elif instruction==b'\x0f\x01\x3b':
            state['invlpg']+=1;generation+=1;mapping=None
        elif instruction[:1]!=b'\xe9':
            register=(instruction[2]>>3)&7
            proposed=list((cr0,cr3,cr4));proposed[{0:0,3:1,4:2}[register]]=eax
            if needs_reload((cr0,cr3,cr4),proposed,register):
                addresses,values,error=load(table,proposed[1])
                pv['load_addresses']=addresses
                state['attempts']+=1;state['reads']+=4
                if error is not None:
                    rec['vector']=error
                else:
                    state['hidden']=dict(entries=values,generation=state['hidden']['generation']+1)
            if not rec['vector']:
                cr0,cr3,cr4=proposed;generation+=1;mapping=None
        rec.update(after=(cr0,cr3,cr4),generation_after=generation,eax_after=eax,
                   next_rip=rip if rec['vector'] else
                   (SOURCE+16 if instruction[:1]==b'\xe9' else rip+len(instruction)))
        pv['after']=copy.deepcopy(state['hidden'])
        pv['attempts_after']=state['attempts'];pv['reads_after']=state['reads']
        state['visits'][len(records)]=pv
        records.append(rec)
        if rec['vector']:
            if profile in (18,19) and rec['vector']==13:
                # Explicit bounded exception-handler fixture, not IDT emulation.
                handled=rec;state['handled_faults']+=1;state['evictions']+=1;mapping=None
                index+=1
                continue
            break
        completed+=1
        if profile in (14,15,16,21) and completed==5:
            qword(table,0x20,0x4001);state['mutations']+=1
            if profile==15:
                mapping=None;state['evictions']+=1
        if profile in (18,19) and completed==7:
            qword(table,0x20,0x4001);qword(table,0x38,0x21);state['mutations']+=1
        if profile==21 and completed==8:
            qword(table,0x20,0x2001);state['mutations']+=1
        index=4 if instruction[:1]==b'\xe9' else index+1
        if index==len(source):break
    return dict(records=records,memory=memory,pae_tables=table,pae=state,
                completed=completed,walks=walks,generation=generation,mapping=mapping,
                handled=handled)
