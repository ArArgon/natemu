"""Run selected, unchanged compat-limits system images on AMD KVM and keep the observed fields."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv
import argparse
import hashlib
import json
import os
import shlex
import struct
import subprocess
import tarfile
import uuid

sys.path.insert(0, str(testenv.ROOT))
sys.path.insert(0, str(testenv.ROOT / 'tools' / 'bench' / 'smp'))
import answers
import compat_limits
import smp
from testkit import artifacts, inputs
from testkit.harness.campaign import REFERENCE_CPU, REF_HOST, _reference, _snap

SELECTED = {'fs-resident-wrapped-word', 'gs-before-unmapped-page', 'fs-string-repe-fault-flags', 'fs-string-repe-page-flags',
            'gs-maskstore-zero', 'gs-null-maskstore-zero', 'fs-null-gather-zero', 'fs-maskmovdqu-zero'}
# M2 checkpoint C, the `amd-generic` case: every generic operand-check case of compat_limits (`fs-generic-*`, `gs-generic-*`)
# but the one that pins natemu's recorded deviation: the fixture's expectations are the processor's.
GENERIC = ('fs-generic-', 'gs-generic-')
DEVIATIONS = {'fs-generic-flat-limit-offset-overrun-deviation'}
ARGS = ['-enable-kvm', '-cpu', REFERENCE_CPU, '-m', '64', '-machine', 'pc', '-nodefaults', '-display', 'none',
        '-serial', 'none', '-monitor', 'none', '-no-reboot', '-device', 'isa-debug-exit,iobase=0xf4,iosize=0x04']


def boot_images(out, host, images):
    name = 'natemu-compat-kvm-' + uuid.uuid4().hex[:12]
    pack = out / 'images.tar'
    with tarfile.open(pack, 'w') as t:
        for case in images:
            t.add(out / (case + '.bin'), arcname=case + '.bin')
    script = f"""set -eu
rm -f /etc/apt/sources.list.d/*
echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/{_snap()}/ trixie main' > /etc/apt/sources.list
timeout 600 apt-get -o Acquire::Retries=3 update -qq >&2
timeout 600 apt-get -o Acquire::Retries=3 install -y -qq --no-install-recommends qemu-system-x86 >&2
echo "ref-qemu: $(qemu-system-x86_64 --version | head -1)" >&2
mkdir /tmp/results
cd /tmp/results
for f in /b/*.bin; do
    base=$(basename "$f" .bin)
    rc=0
    timeout -s KILL 30 qemu-system-x86_64 {shlex.join(ARGS)} -debugcon "file:$base.dump" -kernel "$f" >&2 || rc=$?
    echo "$rc" > "$base.exit"
done
tar cf - .
"""
    remote = (f'set -e; d=$(mktemp -d); trap \'docker kill {name} >/dev/null 2>&1 || true; rm -rf "$d"\' EXIT HUP TERM INT; '
              f'tar xf - -C "$d"; {REF_HOST}echo "ref-vendor: $(grep -m1 vendor_id /proc/cpuinfo | cut -d: -f2 | xargs)" >&2; '
              "awk -F: '/^(cpu family|model[[:space:]]|stepping|microcode)[[:space:]]*:/ && !seen[$1]++ "
              "{k=$1; gsub(/[[:space:]]/, \"\", k); print \"ref-\" k \":\" $2}' /proc/cpuinfo >&2; "
              'echo "ref-virt: $(systemd-detect-virt || true)" >&2; '
              f'{inputs._slot_wait()}docker run --rm -i --device /dev/kvm --name {name} -v "$d:/b:ro" '
              f'{artifacts.BENCH_BUILDER} sh -c {shlex.quote(script)} </dev/null')
    with pack.open('rb') as data, (out / 'dumps.tar').open('wb') as dumps, (out / 'kvm.log').open('wb') as log:
        try:
            r = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, remote], stdin=data, stdout=dumps, stderr=log, timeout=1800)
        except subprocess.TimeoutExpired:
            subprocess.run(['ssh', '-o', 'BatchMode=yes', host, f'docker kill {name}'], timeout=60, capture_output=True)
            raise
    pack.unlink()
    log = (out / 'kvm.log').read_bytes()
    if r.returncode:
        raise RuntimeError(f'KVM container failed ({r.returncode}): {log[-700:].decode(errors="replace")}')
    info = _reference(log)
    if info['vendor'] != 'AuthenticAMD':
        raise RuntimeError(f'AMD reference requires an AMD processor: {info}')
    return info


def projection(program, expected, dump):
    memory = smp.parse_dump(program, dump)['memory']
    got = {}
    for addr, value in expected.items():
        got[hex(addr)] = next(r['data'][addr - r['addr']:addr - r['addr'] + len(value)].hex() for r in memory
                              if r['addr'] <= addr and addr + len(value) <= r['addr'] + len(r['data']))
    return got


def main(out, generic=False):
    out.mkdir(parents=True, exist_ok=True)
    stub = smp.build_stub(out)
    cases = {p['name']: (p, e) for p, e in compat_limits.cases(reference_control=True)
             if (p['name'].startswith(GENERIC) and p['name'] not in DEVIATIONS if generic else p['name'] in SELECTED)}
    assert len(cases) > 50 if generic else cases.keys() == SELECTED
    keys = {}
    for name, (program, expected) in cases.items():
        img = smp.build_image(program, stub)
        (out / (name + '.bin')).write_bytes(img)
        fields = sorted((addr, len(value)) for addr, value in expected.items())
        ident = ['amd-compat-segments', artifacts.BENCH_BUILDER, _snap(), ARGS, hashlib.sha256(img).hexdigest(), fields]
        keys[name] = answers.digest(answers.canon(ident))
    observed = {}
    if answers.mode() == 'seed':
        info = boot_images(out, os.environ['NATEMU_X86_HOST'], cases)
        print(info, flush=True)
        (out / 'reference.json').write_text(json.dumps(info, indent=2))
        with tarfile.open(out / 'dumps.tar') as t:
            for name, (program, expected) in cases.items():
                rc = int(t.extractfile('./' + name + '.exit').read())
                if rc != 1: raise RuntimeError(f'{name} QEMU exit {rc}, expected isa-debug-exit 1')
                dump = t.extractfile('./' + name + '.dump').read()
                observed[name] = projection(program, expected, dump)
                answers.record(keys[name], answers.canon(observed[name]), dict(info, supplier='amd-kvm', program=name, image_sha256=hashlib.sha256((out / (name + '.bin')).read_bytes()).hexdigest()))
    else:
        observed = {name: json.loads(answers.lookup(key).value) for name, key in keys.items()}
    # The original fixture remains the Intel-oriented comparison. These AMD exceptions to it
    # are explicit, with the observed fields retained next to both verdicts in the report.
    controls = {}
    for name in () if generic else ('gs-null-maskstore-zero', 'fs-null-gather-zero'):
        program, _ = cases[name]
        code = next(bytes.fromhex(r['data']) for r in program['memory'] if smp.num(r['addr']) == compat_limits.C.U)
        assert code.endswith(bytes.fromhex('cd80'))
        controls[name] = {compat_limits.C.REC: struct.pack('<4Q', 128, 0, compat_limits.C.U + len(code), 0xdb)}
    for name in () if generic else ('fs-string-repe-fault-flags', 'fs-string-repe-page-flags'):
        controls[name] = {compat_limits.C.REC + 0x20: struct.pack('<Q', 0x10044)}
    results = []
    for name, (_, expected) in cases.items():
        diffs = [f'{addr:#x}: fixture {value.hex()}, AMD {observed[name][hex(addr)]}' for addr, value in expected.items()
                 if observed[name][hex(addr)] != value.hex()]
        wanted = expected | controls.get(name, {})
        bad = [f'{addr:#x}: AMD reference {value.hex()}, actual {observed[name][hex(addr)]}' for addr, value in wanted.items()
               if observed[name][hex(addr)] != value.hex()]
        print(name, 'compatibility fixture:', diffs, 'AMD reference:', bad, flush=True)
        results.append(dict(name=name, outcome='fail' if bad else 'match', diffs=bad, fixture_differences=diffs))
    (out / 'observed.json').write_text(json.dumps(observed, indent=2))
    (out / 'results.json').write_text(json.dumps(dict(results=results), indent=2))
    return int(any(r['diffs'] for r in results))


if __name__ == '__main__':
    testenv.require_framework(__file__)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=pathlib.Path)
    parser.add_argument('--generic', action='store_true', help='the generic operand-check cases instead of the selected ones')
    a = parser.parse_args()
    sys.exit(main(a.out, a.generic))
