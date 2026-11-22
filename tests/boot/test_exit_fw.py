"""The exit test's firmware and program (tests/boot/run_exit_fw.py): the images assemble with the layout the firmware
boot mode and the program driver rely on, their 16- and 32-bit code uses only the boot-time interpreter's first-version
instructions (plan section 1: anything else is a typed stop there), and the judges refuse wrong answers."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import re, subprocess

import pytest

import run_exit_fw as fw

# Plan section 1's first version, by ndisasm's mnemonics: every other instruction stops the interpreter.
ALLOWED = {
    'mov', 'movzx', 'lea',
    'add', 'or', 'adc', 'sbb', 'and', 'sub', 'xor', 'cmp', 'test', 'inc', 'dec', 'shl', 'shr', 'sar',
    'bt', 'bts', 'btr', 'btc', 'xadd',
    'ja', 'jae', 'jb', 'jbe', 'jc', 'jnc', 'jz', 'jnz', 'jo', 'jno', 'js', 'jns', 'jpe', 'jpo', 'jl', 'jnl', 'jg', 'jng',
    'jmp', 'loop', 'call', 'ret', 'retf', 'retfd',
    'push', 'pop', 'pushf', 'pushfw', 'pushfd', 'popf', 'popfw', 'popfd',
    'lgdt', 'lidt', 'rdmsr', 'wrmsr', 'cpuid',
    'cli', 'cld', 'hlt', 'pause', 'wbinvd', 'nop',
}
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
    if op == 'call' and (':' in text or 'far' in rest): return 'far CALL'
    if op == 'retf' and bits == 16: return 'far RET only with a 32-bit operand size'   # ndisasm: 16-bit retf; o32: retfd
    if op in ('push', 'pop') and (set(rest) & SREGS or any('[' in w for w in rest)): return 'PUSH/POP of a register or immediate only'
    return None


@pytest.fixture(scope='module')
def images(tmp_path_factory):
    return fw.build(tmp_path_factory.mktemp('exit-fw'))


def test_the_firmware_is_64_kib_with_its_reset_vector_at_fffffff0h(images):
    f = images['firmware']
    s, data = f['symbols'], f['bytes']
    assert len(data) == 0x10000 and s['reset'] == 0xFFFFFFF0
    assert data[0xFFF0] == 0xE9, 'jmp near at the reset vector'
    target = (0xFFF3 + int.from_bytes(data[0xFFF1:0xFFF3], 'little')) & 0xFFFF
    assert 0xFFFF0000 + target == s['fw16']


def test_the_ap_stub_fits_its_page_and_both_images_carry_the_same_stub(images):
    stubs = [fw.region(im, 'ap_src', 'ap_end')[0] for im in images.values()]
    assert stubs[0] == stubs[1] and 0 < len(stubs[0]) <= 0x1000
    p = images['program']
    assert p['symbols']['ap_src'] == fw.BODY and p['symbols']['start'] == fw.START


@pytest.mark.parametrize('variant', ['firmware', 'program'])
def test_16_and_32_bit_code_uses_only_first_version_instructions(images, variant, tmp_path):
    counts = {}
    for start, end, bits in fw.REGIONS[variant]:
        code, at = fw.region(images[variant], start, end)
        rows = disassemble(code, at, bits, tmp_path)
        assert sum(n for _, n, _ in rows) == len(code), f'{start}: an instruction runs past {end}'
        bad = [f'{a:#x} {t}: {why}' for a, _, t in rows if (why := refused(t, bits))]
        assert not bad, bad
        counts[start] = len(rows)
    assert all(counts.values()), counts


def test_the_instruction_check_refuses_what_the_first_version_lacks(tmp_path):
    """Negative control: forms outside section 1's list, as nasm assembles them, are refused."""
    src = tmp_path / 'bad.asm'
    cases = {16: ['in al, dx', 'out dx, al', 'rep movsb', 'mov eax, cr2', 'mov eax, dr7', 'iret', 'sti', 'push ds',
                  'call 0x10:0x20', 'retf', 'xadd [bx], ax', 'mul ecx', 'int 0x10', 'lmsw ax', 'pusha', 'xchg ax, bx'],
             32: ['in al, dx', 'movsd', 'o16 retf', 'pop dword [eax]', 'ltr ax', 'rol eax, 1', 'div ecx', 'leave']}
    for bits, lines in cases.items():
        for line in lines:
            src.write_text(f'bits {bits}\n{line}\n')
            out = tmp_path / 'bad.bin'
            subprocess.run(['nasm', '-f', 'bin', '-o', str(out), str(src)], check=True)
            (_, _, text), = disassemble(out.read_bytes(), 0, bits, tmp_path)
            assert refused(text, bits), f'{bits}-bit {line!r} ({text}) passed the check'
    for bits, line in ((16, 'o32 retf'), (32, 'retf'), (32, 'lock xadd [eax], ecx'), (16, 'jmp dword 0x10:0x8032')):
        src.write_text(f'bits {bits}\n{line}\n')
        subprocess.run(['nasm', '-f', 'bin', '-o', str(tmp_path / 'ok.bin'), str(src)], check=True)
        (_, _, text), = disassemble((tmp_path / 'ok.bin').read_bytes(), 0, bits, tmp_path)
        assert refused(text, bits) is None, (line, text)


@pytest.mark.parametrize('n', [1, 2, 4])
def test_the_judges_pass_the_expected_answers_and_refuse_wrong_ones(n):
    good = {'exit': 1, 'serial': fw.expected_serial(n)}
    assert fw.judge('firmware', n, good) == []
    for bad in ({**good, 'exit': 3}, {**good, 'serial': good['serial'].replace('status=0', 'status=1')},
                {**good, 'serial': good['serial'].replace(f'aps={n - 1}', f'aps={n}')},
                {**good, 'serial': good['serial'].splitlines(True)[1]}, {'exit': 1, 'serial': ''}):
        assert fw.judge('firmware', n, bad)
    g, f = fw.expected_registers(n)
    good = {'exit': 1, 'gpr': g, 'rflags': f, 'fault': None}
    assert fw.judge('program', n, good) == []
    for k in range(16):
        assert fw.judge('program', n, {**good, 'gpr': [x + (i == k) for i, x in enumerate(g)]})
    for bad in ({**good, 'rflags': 0}, {**good, 'fault': [13, 0, 0x8000, 0]}, {**good, 'exit': 'timeout'},
                {'exit': 1, 'dump': 'unreadable'}):
        assert fw.judge('program', n, bad)
