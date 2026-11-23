"""The exit test's firmware and program (tests/boot/run_exit_fw.py): the images assemble with the layout the firmware
boot mode and the program driver rely on, their 16- and 32-bit code uses only the boot-time interpreter's first-version
instructions (plan section 1: anything else is a typed stop there), and the judges refuse wrong answers."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import re, subprocess

import pytest

import run_exit_fw as fw
from first_version import disassemble, refused


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


def test_an_ap_counts_only_from_64_bit_mode(images, tmp_path):
    """The AP's arrival byte and counter updates are RIP-relative: the same bytes run as 32-bit code (an AP left in
    compatibility mode or 32-bit mode after its far jump) address neither, so such an AP never arrives."""
    for im in images.values():
        s = im['symbols']
        code, at = fw.region(im, 'ap64', 'ap_gdt')
        to_page = lambda a: a - s['ap_src'] + fw.AP_BASE
        as64 = [t for _, _, t in disassemble(code, to_page(at), 64, tmp_path)]
        assert f"lea rax,[rel {to_page(s['ap_arrivals']):#x}]" in as64, as64
        assert f"lock inc dword [rel {to_page(s['ap_count']):#x}]" in as64, as64
        as32 = ' '.join(t for _, _, t in disassemble(code, to_page(at), 32, tmp_path))
        for name in ('ap_arrivals', 'ap_count'):
            assert f'{to_page(s[name]):#x}' not in as32, (name, as32)


def test_the_instruction_check_refuses_what_the_first_version_lacks(tmp_path):
    """Negative control: forms outside section 1's list, as nasm assembles them, are refused."""
    src = tmp_path / 'bad.asm'
    cases = {16: ['in al, dx', 'out dx, al', 'rep movsb', 'mov eax, cr2', 'mov eax, dr7', 'iret', 'sti', 'push ds',
                  'call 0x10:0x20', 'retf', 'xadd [bx], ax', 'mul ecx', 'int 0x10', 'lmsw ax', 'pusha', 'xchg ax, bx',
                  'call ax', 'call [bx]', 'call far [bx]', 'jmp ax', 'jmp [bx]', 'jmp far [bx]', 'ret 4', 'retf 4',
                  'jcxz $', 'loope $'],
             32: ['in al, dx', 'movsd', 'o16 retf', 'pop dword [eax]', 'ltr ax', 'rol eax, 1', 'div ecx', 'leave',
                  'jmp eax', 'jmp [eax]', 'jmp word far [eax]', 'call eax', 'retf 4', 'ret 8', 'jecxz $']}
    for bits, lines in cases.items():
        for line in lines:
            src.write_text(f'bits {bits}\n{line}\n')
            out = tmp_path / 'bad.bin'
            subprocess.run(['nasm', '-f', 'bin', '-o', str(out), str(src)], check=True)
            (_, _, text), = disassemble(out.read_bytes(), 0, bits, tmp_path)
            assert refused(text, bits), f'{bits}-bit {line!r} ({text}) passed the check'
    jcc = ['jo', 'jno', 'jb', 'jae', 'je', 'jne', 'jbe', 'ja', 'js', 'jns', 'jp', 'jnp', 'jl', 'jge', 'jle', 'jg']
    good = [(16, 'o32 retf'), (32, 'retf'), (32, 'lock xadd [eax], ecx'), (16, 'jmp dword 0x10:0x8032'),
            (16, 'jmp 0x10:0x20'), (16, 'jmp dword far [bx]'), (32, 'jmp far [eax]'), (32, 'jmp 0x8:0x806d'),
            (16, 'jmp short $'), (32, 'jmp near $'), (16, 'call $'), (32, 'loop $'), (16, 'ret'),
            *((b, f'{j} {w} $') for b in (16, 32) for j in jcc for w in ('short', 'near'))]
    for bits, line in good:
        src.write_text(f'bits {bits}\n{line}\n')
        subprocess.run(['nasm', '-f', 'bin', '-o', str(tmp_path / 'ok.bin'), str(src)], check=True)
        (_, _, text), = disassemble((tmp_path / 'ok.bin').read_bytes(), 0, bits, tmp_path)
        assert refused(text, bits) is None, (line, text)


@pytest.mark.parametrize('n', fw.CPUS)
def test_the_judges_pass_the_expected_answers_and_refuse_wrong_ones(n):
    good = {'exit': 1, 'serial': fw.expected_serial(n)}
    assert fw.judge('firmware', n, good) == []
    assert fw.judge('firmware', n, {**good, 'exit': 'poweroff'}, want_exit='poweroff') == []
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



def test_natemu_firmware_judge_and_bounds():
    """The firmware variant under natemu: a run to the power-off with the report passes `judge` and the bounds; the
    bounds refuse too many decode exits or shootdowns on any vCPU."""
    for n in fw.CPUS:
        done = {'exit': 'poweroff', 'serial': fw.expected_serial(n), 'vcpus': [{'decodes': 19, 'shootdowns': 1}] * n}
        assert fw.judge('firmware', n, done, want_exit='poweroff') + fw.judge_bounds(done) == []
        assert fw.judge('firmware', n, {**done, 'exit': 'resident'}, want_exit='poweroff')
    assert fw.judge_bounds({'vcpus': [{'decodes': 0, 'shootdowns': 0}, {'decodes': fw.DECODES_MAX + 1, 'shootdowns': 0}]})
    assert fw.judge_bounds({'vcpus': [{'decodes': 0, 'shootdowns': fw.SHOOTDOWNS_MAX + 1}]})
