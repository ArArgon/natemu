"""Finite scalar comparison; no resident/runtime implementation imports."""
import struct

BASE, RIP = 0x100000, 0x102000
REGS = ('rax','rcx','rdx','rbx','rsp','rbp','rsi','rdi','r8','r9','r10','r11','r12','r13','r14','r15')
SOURCES = {'load': '488b03', 'stack': '68bbbcbdbe6668525166686261488b42f88b5af4',
           'xchg': '87038902', 'lfence': '0faee8', 'sfence': '0faef8', 'mfence': '0faef0'}
PROFILES = {'xchg':7, 'lfence':10, 'sfence':11, 'mfence':12}
NATIVE_ROOTS = (4,7,5,6,8,9,10,11)

def require(ok, why):
    if not ok: raise ValueError(why)

def expected(case, registers, memory):
    g, m = list(registers), bytearray(memory)
    def read(address, width):
        offset = address - BASE
        require(0 <= offset <= len(m)-width, 'read range')
        return int.from_bytes(m[offset:offset+width], 'little')
    def write(address, value, width):
        offset = address - BASE
        require(0 <= offset <= len(m)-width, 'write range')
        m[offset:offset+width] = value.to_bytes(width, 'little')
    if case == 'load': g[0] = read(g[3], 8)
    elif case == 'stack':
        for value,width in ((0xffffffffbebdbcbb,8),(0x5152,2),(0x6162,2)):
            g[4] -= width; write(g[4],value,width)
        g[0],g[3] = read(g[2]-8,8),read(g[2]-12,4)
    elif case == 'xchg':
        old = read(g[3],4); write(g[3],g[0]&0xffffffff,4); g[0] = old
        write(g[2],g[0],4)
    else: require(case in PROFILES, 'unknown scalar case')
    return g, bytes(m)

def project(case, r):
    source = bytes.fromhex(SOURCES[case])
    if case in ('load','stack'):
        require(r['status']==1 and r['detail']==0 and r['stage']==2, 'resident successful completion')
        require(r['rip']==RIP and r['completed']==(1 if case=='load' else 5), 'resident RIP/count')
        before,after = r['recovery']['guest_before'],r['recovery']['guest_after']
        packet=r['request']
        require(packet[7:9]==[RIP,64], 'source RIP/bitness')
        raw = b''.join(x.to_bytes(8,'little') for x in packet[10:13])
        require(packet[9]==len(source) and raw[:len(source)]==source, 'literal source packet')
        g=list(before['gpr']); memory=bytes(r['memory_before']); span=len(memory)
        require(span==(256 if case=='load' else 8192), 'resident memory span')
        memory += bytes([0xa5])*(8192-span)
        final_memory=bytes(r['recovery']['memory_after'] if case=='load' else r['memory_after'])
        final_g=list(after['gpr']); indices=list(range(16))
        require(before['rip']==RIP and after['rip']==RIP+len(source), 'canonical final RIP')
        require(before['rflags']==0x202 and after['rflags']==0x202, 'canonical preserved flags')
        flags_compared=True
    else:
        require((r['profile'],r['n'],r['rotation'],r['seed'])==(PROFILES[case],1,0,1), 'resident ordering identity')
        o=r['owners'][0]; t=o['trials'][0]; packet=o['request']
        require(o['status']==1 and o['detail']==0 and t['completed']==1 and t['faults']==0, 'resident scalar completion')
        require(packet[7:9]==[RIP,64] and packet[11]==len(source), 'ordering source identity')
        require(b''.join(x.to_bytes(8,'little') for x in packet[9:11])[:len(source)]==source, 'ordering literal bytes')
        require(t['token']==65553, 'trial0 token')
        g=[o['initial']['x'][x] for x in NATIVE_ROOTS]+[0x1234500000000000+i for i in range(8,16)]
        g[0]=t['token']; indices=list(range(8)); final_g=[t['captured']['x'][x] for x in NATIVE_ROOTS]
        memory=bytearray([0xa5])*8192
        for offset in (0,128,256,4096): memory[offset:offset+4]=bytes(4)
        memory=bytes(memory); span=8192; final_memory=bytes.fromhex(r['images'][0])
        require(o['initial']['nzcv']==t['captured']['nzcv'], 'native flags preservation')
        flags_compared=False
    eg,em=expected(case,g,memory)
    require([eg[i] for i in indices]==final_g, 'resident register result differs from independent semantics')
    require(em[:span]==final_memory, 'resident memory result differs from independent semantics')
    return {'case':case,'source':source.hex(),'rip':RIP,'initial_gpr':g,'initial_rflags':0x202,
            'expected_gpr':eg,'expected_rip':RIP+len(source),'expected_rflags':0x202,
            'resident_gpr_indices':indices,'resident_memory_bytes':span,'resident_canonical_flags':flags_compared,
            'memory_before':memory.hex(),'memory_after':em.hex()}

def assembly(p):
    source=bytes.fromhex(p['source']); require(source==bytes.fromhex(SOURCES[p['case']]), 'assembly source')
    lines=['BITS 64','ORG 0x100000','incbin "memory.bin"','guest:', 'db '+','.join(hex(x) for x in source),'capture:']
    lines += [f'mov [0x104000+{i*8}], {reg}' for i,reg in enumerate(REGS)]
    lines += ['mov rsp, 0x107000','pushfq','pop qword [0x104080]',
              'lea rax, [rel capture]','mov [0x104088], rax',
              'mov eax, 1','mov edi, 1','mov esi, 0x104000','mov edx, 144','syscall',
              'mov eax, 1','mov edi, 1','mov esi, 0x100000','mov edx, 8192','syscall',
              'mov eax, 60','xor edi, edi','syscall','times 0x3000-($-$$) db 0','entry:',
              'mov rsp, 0x107000','push qword 0x202','popfq']
    lines += [f'mov {reg}, 0x{p["initial_gpr"][i]:x}' for i,reg in enumerate(REGS) if reg!='rsp']
    lines += [f'mov rsp, 0x{p["initial_gpr"][4]:x}','jmp guest','times 0x7000-($-$$) db 0xa5']
    return '\n'.join(lines)+'\n'

def elf(payload, p):
    source=bytes.fromhex(p['source'])
    require(len(payload)==0x7000 and payload[:8192]==bytes.fromhex(p['memory_before']), 'assembled payload memory')
    require(payload[0x2000:0x2000+len(source)]==source, 'assembled exact guest source')
    header=struct.pack('<16sHHIQQQIHHHHHH',b'\x7fELF\x02\x01\x01'+bytes(9),2,62,1,0x103000,64,0,0,64,56,1,0,0,0)
    ph=struct.pack('<IIQQQQQQ',1,7,0x1000,BASE,BASE,len(payload),len(payload),0x1000)
    return (header+ph).ljust(0x1000,b'\0')+payload

def compare(p, output):
    require(len(output)==144+8192, 'exact binary output length')
    values=struct.unpack('<18Q',output[:144])
    require(list(values[:16])==p['expected_gpr'], 'QEMU GPR mismatch')
    require(values[16]==p['expected_rflags'], 'QEMU full RFLAGS mismatch')
    require(values[17]==p['expected_rip'], 'QEMU final RIP mismatch')
    require(output[144:]==bytes.fromhex(p['memory_after']), 'QEMU memory/canary mismatch')
    return {'case':p['case'],'status':'passed','gpr_count':16,'resident_gpr_count':len(p['resident_gpr_indices']),
            'resident_memory_bytes':p['resident_memory_bytes'],'qemu_memory_bytes':8192,
            'rflags':values[16],'rflags_comparison':'exact fixed Linux-user profile','final_rip':values[17]}
