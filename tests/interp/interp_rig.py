"""The reference rig of the boot-time interpreter's cases (docs/restructure/interp-cases.md): the capture blobs, the
image (interp_stub.asm and the case table), the two references and what each case's answer is.

References: `qemu`, the pinned QEMU 11.1.2 system emulator (TCG; testkit builds it, NATEMU_QEMU_SYSTEM) with natemu's
system CPU model (tools/bench/smp/smp.py SYSTEM_CPU); `kvm`, the x86 reference machine (NATEMU_X86_HOST) under KVM, the
instructions run by its processor, with Debian trixie's QEMU at the campaign snapshot in the builds' pinned container
image, as the framework's other KVM reference boots (testkit/harness/campaign.py native_vm). One image runs every case,
one QEMU run per reference.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402,E702
import functools
import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import tarfile
import tempfile
import uuid

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STUB = HERE / 'interp_stub.asm'
IMAGE_BASE, TABLE = 0x100000, 0x110000
REC, REC_SIZE = 0x6F00, 0x60
V_HOME, V_C32, L64_FP = 0x100020, 0x100028, 0x100038     # interp_stub.asm's fixed entry points
MODE_CODE = {'R': 0, 'P16': 1, 'P32': 2, 'C32': 3}
ID_MARK = 0xC0DE1D00
QEMU_CPU = 'max,-pcid,-smap'           # = tools/bench/smp/smp.py SYSTEM_CPU, natemu's system CPU model in QEMU
KVM_CPU = 'host,-pku'                  # = testkit/harness/campaign.py REFERENCE_CPU
KVM_IMAGE = 'docker.io/library/debian@sha256:8f6a88feef3ed01a300dafb87f208977f39dccda1fd120e878129463f7fa3b8f'   # = testkit/artifacts.py BENCH_BUILDER
SNAPSHOT_FILE = ROOT / 'tools/correctness/recipes/common.sh'                    # its SNAP: the KVM boots' QEMU
QEMU_PIN = 'qemu-system 11.1.2 src'
RAM_MB = 128


def machine_args(img, out):
    """The arguments of both references but the accelerator and CPU (one CPU, the board's COM1 into `out`)."""
    return ['-m', str(RAM_MB), '-machine', 'pc', '-display', 'none', '-nodefaults', '-monitor', 'none', '-serial', f'file:{out}',
            '-device', 'isa-debug-exit,iobase=0xf4,iosize=0x04', '-no-reboot', '-kernel', str(img)]


def qemu_args(img, out): return ['-accel', 'tcg', '-cpu', QEMU_CPU, *machine_args(img, out)]


def kvm_args(img, out): return ['-enable-kvm', '-cpu', KVM_CPU, *machine_args(img, out)]


# --- capture blobs: where a case stops, saving its final state into REC without touching its memory ---

CAPTURE = r'''
%define R(o) REC + o
{head}
        pushf{sfx}
        pop {ax}
        mov [{s}R(0x24)], eax
        mov [{s}R(0x08)], ecx
        mov [{s}R(0x0c)], edx
        mov [{s}R(0x10)], ebx
        mov [{s}R(0x18)], ebp
        mov [{s}R(0x1c)], esi
        mov [{s}R(0x20)], edi
{saved}
        mov [{s}R(0x28)], es
        mov [{s}R(0x2a)], cs
        mov [{s}R(0x2e)], ds
        mov [{s}R(0x30)], fs
        mov [{s}R(0x32)], gs
        mov dword [{s}R(0)], {mark}
        mov {ax}, cr0
        mov [{s}R(0x34)], eax
        mov {ax}, cr3
        mov [{s}R(0x38)], eax
        mov {ax}, cr4
        mov [{s}R(0x3c)], eax
        mov ecx, 0xC0000080
        rdmsr
        mov [{s}R(0x40)], eax
        mov [{s}R(0x44)], edx
        {o32}sgdt [{s}R(0x48)]
        {o32}sidt [{s}R(0x50)]
        {back}
'''
# 16- and 32-bit modes: EAX, ESP and SS go to DR0-DR2 first (a case may leave any data segments: only SS, reloaded with
# a known base-0 selector, addresses REC); then PUSHFD on a stack at REC. 64-bit mode ignores the data segments' bases, so
# it stores straight away.
HEAD = '''        mov dr0, eax
        mov eax, esp
        mov dr1, eax
        mov ax, ss
        mov dr2, eax
        mov ax, {ss}
        mov ss, ax
        mov esp, REC'''
SAVED = '''        mov eax, dr0
        mov [{s}R(0x04)], eax
        mov eax, dr1
        mov [{s}R(0x14)], eax
        mov eax, dr2
        mov [{s}R(0x2c)], ax'''
HEAD64 = '''        mov [abs R(0x04)], eax
        mov eax, esp
        mov [abs R(0x14)], eax
        mov ax, ss
        mov [abs R(0x2c)], ax
        mov ax, 0x10
        mov ss, ax
        mov esp, REC'''
ENDS = {      # end mode: (bits, the stack's selector, the way home)
    'R': (16, '0', 'jmp 0x0700:0x0000'),
    'P16': (16, '0x20', f'jmp dword 0x08:{V_HOME:#x}'),
    'P32': (32, '0x10', f'jmp 0x08:{V_HOME:#x}'),
    'C32': (32, '0x10', f'jmp 0x08:{V_C32:#x}'),
    'L64': (64, None, f'jmp far dword [abs {L64_FP:#x}]'),
}


@functools.cache
def capture_template(end: str) -> bytes:
    bits, ss, back = ENDS[end]
    if bits == 64:
        text = CAPTURE.format(head=HEAD64, sfx='q', ax='rax', s='abs ', saved='', mark=hex(ID_MARK), o32='', back=back)
    else:
        s = 'ss:'
        text = CAPTURE.format(head=HEAD.format(ss=ss), sfx='d', ax='eax', s=s, saved=SAVED.format(s=s), mark=hex(ID_MARK),
                              o32='o32 ' if bits == 16 else '', back=back)
    src = f'bits {bits}\nREC equ {REC:#x}\n{text}'
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d)
        (p / 'c.asm').write_text(src)
        r = subprocess.run(['nasm', '-f', 'bin', str(p / 'c.asm'), '-o', str(p / 'c.bin')], capture_output=True, text=True)
        if r.returncode: raise RuntimeError(f'nasm (capture {end}): {r.stderr}')
        out = (p / 'c.bin').read_bytes()
    assert out.count(ID_MARK.to_bytes(4, 'little')) == 1, end
    return out


def capture(end: str, stop_id: int) -> bytes:
    """The capture blob of a stop in end mode `end` with the stop's id in REC+0."""
    return capture_template(end).replace(ID_MARK.to_bytes(4, 'little'), stop_id.to_bytes(4, 'little'))


# --- the image ---

@functools.cache
def stub() -> bytes:
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d)
        r = subprocess.run(['nasm', '-f', 'bin', str(STUB), '-o', str(p / 'stub.bin')], capture_output=True, text=True)
        if r.returncode: raise RuntimeError(f'nasm ({STUB.name}): {r.stderr}')
        data = (p / 'stub.bin').read_bytes()
    assert len(data) == TABLE - IMAGE_BASE and data[0x40:0x48] == b'IRSTUB01', 'interp_stub.asm: layout changed'
    for v in (V_HOME, V_C32): assert data[v - IMAGE_BASE] == 0xe9, f'interp_stub.asm: no JMP at {v:#x}'
    return data


def num(x): return int(x, 16) if isinstance(x, str) else x


def image(doc: dict) -> bytes:
    """The Multiboot image: the stub and the table (interp_stub.asm's TABLE): a header, the common blocks' list, each
    case's record (its state as interp_stub.asm's PB, its memory list, its windows), then the blocks' bytes."""
    cases = doc['cases']
    blobs = bytearray()
    head = 16 + 4 * len(cases)

    def blob(data: bytes) -> int:
        """Offset (from TABLE) where `data` will sit, once the records are laid out: fixed below."""
        o = len(blobs); blobs.extend(data); blobs.extend(bytes(-len(blobs) % 4)); return o
    common = [(num(b['addr']), bytes.fromhex(b['data'])) for b in doc['common']]
    common_rec = struct.pack('<I', len(common))
    pend = []         # (record bytearray, [(position of src field, blob offset)])
    cl = [(a, len(d), blob(d)) for a, d in common]
    recs = []
    for c in cases:
        st = c['state']
        gpr = [num(st['gpr'][r]) for r in ('eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi')]
        gpr[4] = num(c['rig']['entry_esp'])
        seg = [num(st['seg'][s]['sel']) for s in ('es', 'cs', 'ss', 'ds', 'fs', 'gs')]
        efer = num(st['efer'])
        r = struct.pack('<8II6HIIIQI', *gpr, num(st['eflags']), *seg, num(st['cr0']), num(st['cr3']), num(st['cr4']), efer, 0)
        r += struct.pack('<HIH', num(st['gdtr']['limit']), num(st['gdtr']['base']), 0)
        r += struct.pack('<HIH', num(st['idtr']['limit']), num(st['idtr']['base']), 0)
        r += struct.pack('<IB3x', num(c['rig']['entry_eip']), MODE_CODE[c['mode']])
        assert len(r) == 0x60
        blocks = [(num(m['addr']), bytes.fromhex(m['data'])) for m in c['memory']]
        blocks.append((num(c['code']['addr']), bytes.fromhex(c['code']['data'])))
        blocks += [(num(b['addr']), bytes.fromhex(b['data'])) for b in c['rig']['bytes']]
        mems = [(a, len(d), blob(d)) for a, d in blocks if d]
        wins = [(num(w['addr']), w['len']) for w in c['compare']['windows']]
        recs.append((r, mems, wins))
    # layout: header, offsets, common list, records, blobs
    common_len = 4 + 12 * len(cl)
    rec_lens = [0x60 + 4 + 12 * len(m) + 4 + 8 * len(w) for _, m, w in recs]
    blob_base = head + common_len + sum(rec_lens)
    out = bytearray(b'IRCASES1' + struct.pack('<II', len(cases), head))
    o = head + common_len
    for n in rec_lens: out += struct.pack('<I', o); o += n
    out += struct.pack('<I', len(cl)) + b''.join(struct.pack('<III', a, n, blob_base + s) for a, n, s in cl)
    for r, mems, wins in recs:
        out += r + struct.pack('<I', len(mems)) + b''.join(struct.pack('<III', a, n, blob_base + s) for a, n, s in mems)
        out += struct.pack('<I', len(wins)) + b''.join(struct.pack('<II', a, n) for a, n in wins)
    assert len(out) == blob_base
    out += blobs
    assert TABLE + len(out) < RAM_MB << 20, 'the table outgrew the guest RAM'      # read with paging off: anywhere in RAM
    return stub() + bytes(out)


# --- what a reference says: the serial lines, parsed, and each case's answer ---

LINE = re.compile(r'^C ([0-9a-f]{8}) ([0-9a-f]+)((?: [0-9a-f]*)*)$')


def parse(text: str, doc: dict) -> tuple[dict, list]:
    """{case index: (record bytes, [window bytes])} from a run's COM1 output, and its problems (a missing END, a
    malformed line)."""
    got, problems = {}, []
    for line in text.replace('\r', '').splitlines():
        m = LINE.match(line)
        if not m: continue
        i, rec = int(m[1], 16), bytes.fromhex(m[2])
        wins = [bytes.fromhex(w) for w in m[3].split(' ')[1:]]
        if len(rec) != REC_SIZE: problems.append(f'case {i}: record of {len(rec)} bytes'); continue
        got[i] = (rec, wins)
    if f'END {len(doc["cases"]):08x}' not in text: problems.append('no END line for every case')
    return got, problems


def fields(rec: bytes, wins: list) -> dict:
    """The record's fields, unmasked."""
    w = struct.unpack_from('<9I', rec, 4)
    out = {'stop': struct.unpack_from('<I', rec, 0)[0], **dict(zip(('eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi'), w[:8]))}
    out['eflags'] = w[8]
    out.update(dict(zip(('es', 'cs', 'ss', 'ds', 'fs', 'gs'), struct.unpack_from('<6H', rec, 0x28))))
    out['cr0'], out['cr3'], out['cr4'] = struct.unpack_from('<3I', rec, 0x34)
    out['efer'] = struct.unpack_from('<Q', rec, 0x40)[0]
    for k, o in (('gdtr', 0x48), ('idtr', 0x50)):
        lim, base = struct.unpack_from('<HI', rec, o); out[k] = [lim, base]
    for i, b in enumerate(wins): out[f'window{i}'] = b.hex()
    return out


def answer(case: dict, rec: bytes, wins: list, ref: str) -> dict:
    """The case's answer from reference `ref`: every field it answers (by: both or `ref`), under the case's masks."""
    return project(case, fields(rec, wins), ref)


def project(case: dict, f: dict, ref: str) -> dict:
    """`answer` from the fields themselves (`fields()`'s form, which natemu's program driver also reports: its
    `machine` output): the fields `ref` answers, under the case's masks."""
    cmp = case['compare']
    by = cmp['by']
    masks = {**{r: num(m) for r, m in cmp['gpr'].items()}, 'eflags': num(cmp['eflags']), 'cr0': num(cmp['cr0']),
             'cr3': num(cmp['cr3']), 'cr4': num(cmp['cr4']), 'efer': num(cmp['efer'])}
    out = {}
    for k, v in f.items():
        if by.get(k, 'both') not in ('both', ref): continue
        out[k] = v & masks[k] if k in masks and isinstance(v, int) else v
    return out


def answered_by(case: dict, ref: str) -> list:
    """The fields reference `ref` answers for `case`."""
    names = ['stop', 'eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi', 'eflags', 'es', 'cs', 'ss', 'ds', 'fs', 'gs',
             'cr0', 'cr3', 'cr4', 'efer', 'gdtr', 'idtr', *(f'window{i}' for i in range(len(case['compare']['windows'])))]
    return [n for n in names if case['compare']['by'].get(n, 'both') in ('both', ref)]


def disagreements(case: dict, qemu: dict, kvm: dict) -> list:
    """The fields both references answer and give different values for."""
    both = [n for n in answered_by(case, 'qemu') if case['compare']['by'].get(n, 'both') == 'both']
    return [f'{n}: qemu {_show(qemu.get(n))}, kvm {_show(kvm.get(n))}' for n in both if qemu.get(n) != kvm.get(n)]


def _show(v): return hex(v) if isinstance(v, int) else v


def key(case: dict, ref: str, common: list) -> str:
    """The lock key of a case's answer from `ref`: what decides it, machine-independent (tools/reference/answers.py, as
    `system_key`): the case (its hash), the common blocks, the rig (`rig_digest`) and the reference's identity; not the
    case's name, the rest of the corpus or the machine."""
    import answers
    return answers.make_key('interp-ref', ref, case['hash'], rig_digest(ref, common))


def rig_digest(ref: str, common: list) -> str:
    """What decides every answer of `ref` besides the case: the stub, the capture blobs, the common blocks and the
    reference (QEMU: its pin, CPU model and arguments; KVM: the CPU flag, the arguments, the container image and the Debian
    snapshot its QEMU comes from; the machine's CPU is in the answers' repro, not here)."""
    import answers
    ident = {'qemu': [QEMU_PIN, *qemu_args('IMG', 'COM1')], 'kvm': [*kvm_args('IMG', 'COM1'), KVM_IMAGE, snapshot()]}[ref]
    return answers.digest(answers.canon([answers.digest(stub()), [capture_template(e).hex() for e in ENDS],
                                         [[b['addr'], answers.digest(b['data'])] for b in common], ident]))


# --- running the references ---

def snapshot() -> str:
    return re.search(r'^SNAP=(\S+)$', SNAPSHOT_FILE.read_text(), re.M)[1]


def run_qemu(img: bytes, work: pathlib.Path, timeout: int = 600) -> tuple:
    """(exit status or 'timeout', COM1 output, the reference's description) of the pinned QEMU (TCG) here."""
    qemu = os.environ.get('NATEMU_QEMU_SYSTEM')
    if not qemu: raise RuntimeError('NATEMU_QEMU_SYSTEM is not set: the framework gives the pinned QEMU build')
    work.mkdir(parents=True, exist_ok=True)
    p, out = work / 'image.bin', work / 'qemu-com1.txt'
    p.write_bytes(img); out.unlink(missing_ok=True)
    try:
        r = subprocess.run([qemu, *qemu_args(p, out)], capture_output=True, timeout=timeout)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = 'timeout'
    return rc, out.read_text(errors='replace') if out.exists() else '', {'reference': 'qemu', 'pin': QEMU_PIN, 'cpu': QEMU_CPU}


def run_kvm(img: bytes, work: pathlib.Path, host: str, timeout: int = 900) -> tuple:
    """(exit status or 'timeout', COM1 output, the machine's description) of the image under KVM on `host`: Debian's QEMU
    (the campaign snapshot) installed in the pinned container image for the run, the container named and killed when the
    ssh session ends, nothing left on the machine. The run takes one of the machine's build slots, as a KVM boot does."""
    work.mkdir(parents=True, exist_ok=True)
    pack = work / 'kvm.tar'
    p = work / 'image.bin'; p.write_bytes(img)
    with tarfile.open(pack, 'w') as t: t.add(p, 'image.bin')
    name = f'natemu-ref-interp-{uuid.uuid4().hex[:12]}'
    apt = f'http://snapshot.debian.org/archive/debian/{snapshot()}/'
    qemu = shlex.join(['qemu-system-x86_64', *kvm_args('/b/image.bin', '/tmp/com1.txt')])
    guest = (f"rm -f /etc/apt/sources.list.d/*; echo 'deb [check-valid-until=no] {apt} trixie main' > /etc/apt/sources.list; "
             'timeout 900 apt-get -o Acquire::Retries=5 update -qq >&2 && '
             'timeout 900 apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends qemu-system-x86 >&2 && '
             'echo "ref-qemu: $(qemu-system-x86_64 --version | head -1)" >&2; '
             f'timeout -s KILL {timeout} {qemu}; echo "ref-exit: $?" >&2; cat /tmp/com1.txt')
    slots = ('got=; while [ -z "$got" ]; do for i in $(seq 0 $(( $(nproc) / 4 > 1 ? $(nproc) / 4 - 1 : 0 ))); do '
             'exec 9>/tmp/natemu-build-slot-$i; if flock -n 9; then got=$i; break; fi; done; [ -n "$got" ] || sleep 5; done; ')
    script = (f'set -e; d=$(mktemp -d); trap \'docker kill {name} >/dev/null 2>&1 || true; rm -rf "$d"\' EXIT HUP TERM INT; '
              'tar xf - -C "$d"; '
              "echo \"ref-cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')\" >&2; "
              'echo "ref-kernel: $(uname -r)" >&2; echo "ref-virt: $(systemd-detect-virt 2>/dev/null || echo unknown)" >&2; '
              f'{slots}docker run --rm -i --name {name} --device /dev/kvm -v "$d:/b:ro" {KVM_IMAGE} sh -c {shlex.quote(guest)} < /dev/null')
    try:
        with open(pack, 'rb') as f:
            r = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, script], stdin=f, capture_output=True, timeout=timeout + 2000)
    except subprocess.TimeoutExpired:
        subprocess.run(['ssh', '-o', 'BatchMode=yes', host, f'docker kill {name}'], capture_output=True, timeout=60)
        return 'timeout', '', {'reference': 'kvm', 'host': host}
    finally:
        pack.unlink(missing_ok=True)
    err = r.stderr.decode(errors='replace')
    info = {k: v.strip() for k, v in re.findall(r'^ref-(\w+): (.*)$', err, re.M)}
    rc = int(info.pop('exit')) if info.get('exit', '').isdigit() else f'ssh {r.returncode}: {err[-400:]}'
    (work / 'kvm-stderr.txt').write_text(err)
    return rc, r.stdout.decode(errors='replace'), {'reference': 'kvm', 'cpu_flag': KVM_CPU, **info}


def corpus_digest(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
