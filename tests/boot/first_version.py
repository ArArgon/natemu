"""The boot-time interpreter's first-version instruction list (plan section 1) as a check over ndisasm's output: the
exit test's firmware (tests/boot/test_exit_fw.py) and the real-code suites' 16- and 32-bit code (run_kut.py) use it.
Everything outside the list is a typed stop in the interpreter's first version."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import re, subprocess

# Plan section 1's first version, by ndisasm's mnemonics: every other instruction stops the interpreter. The sixteen
# conditions as ndisasm names them (70H-7FH, 0F 80H-8FH).
JCC = {'jo', 'jno', 'jc', 'jnc', 'jz', 'jnz', 'jna', 'ja', 'js', 'jns', 'jpe', 'jpo', 'jl', 'jnl', 'jng', 'jg'}
ALLOWED = JCC | {
    'mov', 'movzx', 'lea',
    'add', 'or', 'adc', 'sbb', 'and', 'sub', 'xor', 'cmp', 'test', 'inc', 'dec', 'shl', 'shr', 'sar',
    'bt', 'bts', 'btr', 'btc', 'xadd',
    'jmp', 'loop', 'call', 'ret', 'retf', 'retfd',
    'push', 'pop', 'pushf', 'pushfw', 'pushfd', 'popf', 'popfw', 'popfd',
    'lgdt', 'lidt', 'rdmsr', 'wrmsr', 'cpuid',
    'cli', 'cld', 'hlt', 'pause', 'wbinvd', 'nop',
}
HEX = r'0x[0-9a-f]+'
# JMP short and near (relative), far JMP ptr16:16 and ptr16:32, far JMP m16:32; CALL near (relative) only.
JMP = re.compile(rf'jmp {HEX}|jmp word {HEX}:(word|dword) {HEX}|jmp dword far \[[^]]*\]')
PREFIXES = {'o16', 'o32', 'a16', 'a32', 'lock'}
SREGS = {'cs', 'ds', 'es', 'ss', 'fs', 'gs'}


def disassemble(code, address, bits, tmp):
    """[(address, length, text)] of `code` by ndisasm; continuation lines (long instructions) join their instruction."""
    f = tmp / f'region-{address:x}.bin'
    f.write_bytes(code)
    out = subprocess.run(['ndisasm', '-b', str(bits), '-o', str(address), str(f)], capture_output=True, text=True,
                         check=True).stdout
    rows = []
    for line in out.splitlines():
        if m := re.fullmatch(r'([0-9A-F]+)\s+([0-9A-F]+)\s+(.*)', line):
            rows.append([int(m[1], 16), len(m[2]) // 2, m[3].strip()])
        elif m := re.fullmatch(r'\s+-([0-9A-F]+)', line):
            rows[-1][1] += len(m[1]) // 2
    return [tuple(r) for r in rows]


def refused(text, bits):
    """Why one instruction (ndisasm's text) is outside the first version, or None."""
    words = text.replace(',', ' ').split()
    prefixes = []
    while words and words[0] in PREFIXES: prefixes.append(words.pop(0))
    if not words: return 'a prefix alone'
    op, rest = words[0], words[1:]
    if op not in ALLOWED: return f'{op} is not in the first version'
    if op == 'mov' and any(re.fullmatch(r'cr\d+', w) and w not in ('cr0', 'cr3', 'cr4') for w in rest): return 'only CR0, CR3, CR4'
    if op == 'mov' and any(re.fullmatch(r'[dt]r\d+', w) for w in rest): return 'debug and test registers'
    if op == 'xadd' and 'lock' not in prefixes: return 'XADD only with LOCK'
    body = ' '.join([op, *rest])
    if op == 'jmp' and not JMP.fullmatch(body): return 'only relative, ptr16:16, ptr16:32 and m16:32 JMPs'
    if op in JCC | {'loop', 'call'} and not re.fullmatch(rf'{op} {HEX}', body): return f'{op}: relative forms only'
    if op in ('ret', 'retf', 'retfd') and rest: return 'RET with an immediate'
    if op == 'retf' and bits == 16: return 'far RET only with a 32-bit operand size'   # ndisasm: 16-bit retf; o32: retfd
    if op in ('push', 'pop') and (set(rest) & SREGS or any('[' in w for w in rest)): return 'PUSH/POP of a register or immediate only'
    return None
