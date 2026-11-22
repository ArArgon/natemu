"""Linux workload campaigns (plan docs/superpowers/plans/2026-11-18-campaign-onboarding.md, Task 6-7): each case boots one
part of a campaign image under natemu, with tools/correctness/boot.py's command line, and every test the image lists
for that part is judged by the campaign's own verdict on its `CT-RESULT` line. Nothing is compared with a reference
run and nothing is locked: no QEMU, no x86 machine. The exception is a suite with `reference = "x86"` (i386), whose
verdict compares each test's output with one recorded on the x86 reference machine: a run with TESTKIT_SEED_ANSWERS=1
and NATEMU_X86_HOST=user@host runs each part there instead of under natemu (native() below), and the verdict records.

    [[suite]] kind = "judged"  harness = "campaign"  tier = "campaign"
    parts = N                   cases part-0 ... part-(N-1): the image's parts, in the order its list names them
    cap = 3600                  seconds per boot (the guest's poweroff after CT-END ends it sooner)
    verdict = "verdicts:fn"     fn(name, fields, extra) -> (passed | failed | unsupported | unverified, message), from a
                                file in the family folder; a verdict that takes `image` also gets the image's folder
    expected = { test = "why (the document that records it)" }    known non-passes
    expected_without.FEAT = { test = "why" }    known non-passes only on a host lacking FEAT_<FEAT> (lowercased, as
                                testkit/host.py reads hw.optional.arm: libm's band-ue-flag tests without afp; or
                                hidden by NATEMU_FEXC_FEATURE_OFF, as a "host.afp" = false variant does); on a
                                host with it they are judged as any other test and must pass; `afp` entries also
                                not under NATEMU_FP=accurate, which makes those deviations exact (docs/floating-point.md),
                                except an entry whose reason starts with a deviation accurate mode keeps
                                (`x87-band-ue-flag: ...`: tests/lib/fp_deviations.py MODES has accurate for it).
                                A FEAT the host does not report at all fails part-0 (a misspelt name), unapplied
    flaky = { test = "why" }    timing-sensitive tests: a failure reruns the part once
    verdict_flaky = true        the verdict may itself return `flaky` (a test whose only failures are known
                                timing-sensitive cases, as CPython's TimerfdTests): the part is rerun once, as for a
                                `flaky` entry; the rerun's verdict counts if it passes, a second `flaky` stays flaky and
                                any other failure fails
    matrix = { "machine.vcpus" = [4] }   a part claims as many cores as its variant's vCPUs, at most needs.cores
    reference_boot = "kvm"      seeding a `reference = "x86"` suite boots each part on the x86 machine under KVM, with
                                the kernel and command line of a natemu boot (native_vm()), instead of a chroot on the
                                machine's own kernel: for a suite whose results depend on the kernel (ltp32). Seeding
                                writes `<suite>-reference.txt` beside the suite: the machine, kernel, QEMU and image
    answers = ["file", ...]     answer files in the family folder (`NAME ...` lines): part-0 reports an answer for a
                                test the image does not list as a stale-entry
    check_reuse = true          boots with NATEMU_CHECK_REUSE=1: every translated block natemu reuses (a kept answer at
                                another address, a copy made inside the guest) is compiled again and compared, and a
                                mismatch stops the boot, so its tests go missing (the 32-bit suites: torture32, libm32)
    "engine.fp" in the matrix   boots with NATEMU_FP set to the variant's value (fast, accurate); a verdict that takes
                                `mode` gets {'afp': AFP present and not hidden, 'fp': the mode}. A verdict may return
                                `changed` (a test that failed otherwise than recorded): a failure, expected or not
    ct = "word"                 the kernel's ct= word, which picks the guest's /ct/run-<word>.sh and is CT-END's suite=
                                (default: the suite's name; cpyt's is cpy)
    unreported = "verdicts:fn"  fn(name, serial) -> (verdict, message) | None, for a listed test with no result line:
                                the campaign's own record of it elsewhere in the boot's output (CPython: regrtest's
                                verdict for a module it skipped whole); None leaves it missing
    part_args = "k=v,..."       optional: appended to each part's word on the kernel command line (`part=WORD,ARGS`),
                                options of the campaign's runner (stress: scale=, timeout=); the list and the CT-END
                                check still name the part by its WORD. The environment variable TESTKIT_PART_ARGS,
                                when set, replaces it for the run: `SUITE:ARGS` entries, `;`-separated, for their suite
                                (`stress:scale=1,timeout=240`, a full-size run on request), a bare `ARGS` for every
                                suite no entry names; an empty ARGS means none (part_args() below)
    perf_parts = ["part-1"]     parts that run on performance cores only (`needs` with perf, case_needs()): a part whose
                                guest cap is wall-clock time and would not finish on an efficiency core

The image is `inputs.campaign_image(suite)`: a folder holding `<suite>.cpio.zst` and `<suite>-cases.txt`, every test the
image runs, one `PART TEST` line each, PART being the kernel's `part=` word for that boot (`0/4`, `mem1`, `7/1`). The
distinct PARTs, in the order the list first names them, are the parts; their number must equal `parts`. An image may
also hold `<suite>-boot.txt`, `PART [kernel=FILE] WORD...` per part: the kernel that part boots (FILE in the image's
folder, instead of the stock Debian kernel) and words added to its command line (public: `pubcap=300`, the guest cap).

Per listed test, one sub-result: its verdict, then adjusted (testkit/report.py's categories):
  - listed but not in the output (crashed or hung mid-part): `missing`, a failure;
  - in `expected` and failed: `expected-fail`; in `expected` and passed: `unexpected-pass`, a failure;
  - in `flaky` and failed (or missing): the part is booted once more and the rerun's verdict counts if it passes,
    else `flaky`;
  - unsupported and unverified are reported as such, never as passes.
A result the list does not name for this part is judged too and fails (the list is wrong). An `expected` or `flaky`
entry naming no test of the image is a `stale-entry` failure, reported once, on part-0. A part whose boot never printed
`CT-END` (hang, panic, cap reached) fails with the last 40 serial lines, whatever its results.
"""
import dataclasses
import functools
import inspect
import atexit
import threading
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import time
import uuid

from testkit import artifacts, family, host, resources
from testkit.harness import CaseResult, Requirement, SubResult

AFTER_END = 30.0            # seconds the guest gets to power off after CT-END (boot.py's), then it is stopped
POLL = 0.5
PART_ARGS_ENV = 'TESTKIT_PART_ARGS'     # replaces suites' part_args at launch: SUITE:ARGS;... (part_args())
TAIL = 40                   # serial lines shown for a part that did not reach CT-END


@dataclasses.dataclass
class BootResult:
    reached: bool               # CT-END was printed
    serial: str
    note: str = ''              # how the boot ended (exit status, cap, time), for the case's message
    reference: dict = dataclasses.field(default_factory=dict)  # a reference run's machine (REF_LINE), for provenance


def image(suite: str) -> pathlib.Path:
    """The campaign image's folder (built on this machine from tools/correctness/recipes/<suite>, plan Task 5)."""
    from testkit import inputs
    make = getattr(inputs, 'campaign_image', None)
    if make is None:
        raise resources.Unpreparable(f'campaign-image {suite}', 'testkit.inputs has no campaign_image (plan Task 5)')
    return make(suite)


def _read(f: pathlib.Path) -> str:
    try: return f.read_text(errors='replace')
    except FileNotFoundError: return ''


def _cmdline(suite: str, part: str, args: tuple = ()) -> str:
    return ' '.join([f'console=ttyS0 mitigations=off nokaslr ct={suite} part={part}', *args])


def boot(image: pathlib.Path, suite: str, part: str, vcpus: int, work: pathlib.Path, cap: float,
         kernel: pathlib.Path | None = None, args: tuple = (), env: dict | None = None) -> BootResult:
    """One boot of `image` under natemu, as tools/correctness/boot.py runs it (the netboot 6.12.107 kernel, its command
    line, compile cache off), with the serial console and natemu's output in `work`, a folder no other boot uses. The
    image is only read. Ends AFTER_END seconds after CT-END, when natemu exits, or at `cap` seconds. `kernel` replaces
    the netboot kernel and `args` are added to the command line (the image's <suite>-boot.txt)."""
    n, fexc = artifacts.natemu(), artifacts.fexc()
    kernel = kernel or resources.fetch('debian-netboot-linux')
    work.mkdir(parents=True, exist_ok=True)
    serial = work / 'serial.txt'
    serial.unlink(missing_ok=True)
    cmdline = _cmdline(suite, part, args)
    cmd = [str(n.stub), '--runtime', str(n.runtime), '--fexc', str(fexc), '--kernel', str(kernel), '--initrd', str(image),
           '--cmdline', cmdline, '--vcpus', str(vcpus), '--serial', str(serial)]
    (work / 'cmd.json').write_text(json.dumps(cmd) + '\n')
    env = dict(os.environ, **(env or {}), NATEMU_COMPILE_CACHE='off')
    env.pop('NATEMU_PERF_LOG', None)
    t0 = time.monotonic()
    end_at, how = None, None
    with open(work / 'stdout.txt', 'wb') as out, open(work / 'stderr.txt', 'wb') as err:
        _on_term()                      # before the VM starts: a SIGTERM right after Popen still stops it
        p = subprocess.Popen(cmd, env=env, stdout=out, stderr=err, start_new_session=True)
        _LIVE.add(p); _record_group(p, cmd[0])
        try:
            while p.poll() is None:
                now = time.monotonic()
                if now - t0 >= cap:
                    how = f'stopped at the cap of {cap:.0f} s'; break
                if end_at is None and end_of(_read(serial)) is not None: end_at = now
                if end_at is not None and now - end_at > AFTER_END:
                    how = f'stopped {AFTER_END:.0f} s after CT-END'; break
                time.sleep(POLL)
        finally:
            _stop(p); _LIVE.discard(p); _record_group(p, '')     # stopped: its pid may be reused
    took = time.monotonic() - t0
    text = _read(serial)
    note = f'{how or f"natemu exited with status {p.returncode}"} after {took:.0f} s'
    if end_of(text) is None:
        tail = _read(work / 'stderr.txt').strip()[-400:]
        if tail: note += f'; natemu stderr: {tail}'
    return BootResult(end_of(text) is not None, text, note)


def native(image: pathlib.Path, suite: str, part: str, host: str, cap: float, work: pathlib.Path) -> BootResult:
    """One part run on the x86 machine `host` (ssh, passwordless sudo) instead of under natemu: the image unpacked into
    a temporary folder there, the guest's runner run in it as root (chroot, with /proc and /dev), the folder removed.
    The output has the lines a boot's serial console has, and is kept as `work`/native.txt."""
    # The folder is removed only once /proc and /dev are unmounted, and never across a mount: rm would otherwise follow
    # a /dev bind mount that failed to unmount into the machine's own /dev.
    clean = ('for m in proc dev; do ! mountpoint -q "$d/r/$m" || sudo umount "$d/r/$m" || sudo umount -l "$d/r/$m"; done; '
             'if mountpoint -q "$d/r/proc" || mountpoint -q "$d/r/dev"; then echo "left $d: still mounted" >&2; '
             'else sudo rm -rf --one-file-system "$d"; fi')
    script = (f"set -e; d=$(mktemp -d); trap '{clean}' EXIT; {REF_HOST}echo 'ref-boot: chroot' >&2; "
              'mkdir "$d/r"; zstd -dc | (cd "$d/r" && sudo cpio -idm --quiet); '
              'sudo mount -t proc proc "$d/r/proc"; sudo mount --bind /dev "$d/r/dev"; '
              f'echo "CT-BEGIN suite={suite} part={part} native kernel=$(uname -r)"; '
              'sudo chroot "$d/r" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C TZ=UTC '
              f'/ct/run-{suite}.sh /ct {part} 2>&1 || true; echo "CT-END suite={suite} part={part} rc=0"')
    with open(image, 'rb') as f:
        r = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, script], stdin=f, capture_output=True, timeout=cap)
    text = r.stdout.decode(errors='replace')
    work.mkdir(parents=True, exist_ok=True); (work / 'native.txt').write_text(text)
    return BootResult(end_of(text) is not None, text, f'run natively on {host}' + (f': {r.stderr.decode(errors="replace")[-400:]}' if r.returncode else ''),
                      _reference(r.stderr))


def _snap() -> str:
    """tools/correctness/recipes/common.sh's SNAP, the campaign images' Debian snapshot: native_vm()'s QEMU comes from it."""
    from testkit import paths
    m = re.search(r'^SNAP=(\S+)$', (paths.repo_root() / 'tools/correctness/recipes/common.sh').read_text(), re.M)
    return m[1]


# The reference CPU: the host's less protection keys, which natemu's profile has not. Not pmu=off as well: that leaves
# perf_event_open02 passing (software events) and makes cve-2015-3290 break (TBROK, munmap EINVAL) where it reported TCONF.
REFERENCE_CPU = 'host,-pku'
REF_HOST = ("echo \"ref-cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')\" >&2; "
            'echo "ref-kernel: $(uname -r)" >&2; ')
REF_LINE = re.compile(r'^ref-(\w+): (.*)$', re.M)
APT_TIMEOUT = 900


def _reference(stderr: bytes) -> dict:
    return {k: v.strip() for k, v in REF_LINE.findall(stderr.decode(errors='replace'))}


def native_vm(image: pathlib.Path, suite: str, part: str, vcpus: int, host: str, cap: float, work: pathlib.Path,
              kernel: pathlib.Path | None = None, args: tuple = ()) -> BootResult:
    """One part booted on the x86 machine `host` under KVM instead of under natemu: the kernel, command line, vCPUs and
    2 GiB of RAM (natemu's default) of boot(), the serial console on stdout, kept as `work`/native.txt. The CPU is the
    host's less protection keys, which natemu's profile has not (REFERENCE_CPU; a named Intel model on an AMD machine
    would have KVM emulate SYSENTER and drop features natemu has). QEMU is Debian trixie's at the campaign
    images' snapshot, installed in the builds' pinned container image for each boot, so the machine needs only Docker
    and /dev/kvm; the boot takes one of the machine's build slots (its RAM). The container is named and killed when the
    ssh session ends, however it ends. The result's `reference` names the machine, its kernel and the QEMU."""
    import tarfile
    from testkit import inputs
    work.mkdir(parents=True, exist_ok=True)
    pack = work / 'boot.tar'
    with tarfile.open(pack, 'w') as t:
        t.add(kernel or resources.fetch('debian-netboot-linux'), 'kernel'); t.add(image, 'initrd')
    name = f'natemu-ref-{suite}-{uuid.uuid4().hex[:12]}-{re.sub(r"[^A-Za-z0-9]", "-", part)}'     # two Macs never clash
    apt = f'http://snapshot.debian.org/archive/debian/{_snap()}/'
    guest = (f"rm -f /etc/apt/sources.list.d/*; echo 'deb [check-valid-until=no] {apt} trixie main' > /etc/apt/sources.list; "
             f'timeout {APT_TIMEOUT} apt-get -o Acquire::Retries=5 update -qq >&2 && '
             f'timeout {APT_TIMEOUT} apt-get -o Acquire::Retries=5 install -y -qq --no-install-recommends qemu-system-x86 >&2 && '
             'echo "ref-qemu: $(qemu-system-x86_64 --version | head -1)" >&2 && '
             f'exec timeout -s KILL {int(cap)} qemu-system-x86_64 -enable-kvm -cpu {REFERENCE_CPU} -m 2048 -smp {vcpus} '
             f'-nodefaults -display none -serial stdio -no-reboot -kernel /b/kernel -initrd /b/initrd '
             f'-append "{_cmdline(suite, part, args)}"')
    script = (f'set -e; d=$(mktemp -d); trap \'docker kill {name} >/dev/null 2>&1 || true; rm -rf "$d"\' EXIT HUP TERM INT; '
              f'tar xf - -C "$d"; {REF_HOST}echo "ref-boot: KVM, -cpu {REFERENCE_CPU}" >&2; {inputs._slot_wait()}'
              f'docker run --rm -i --name {name} --device /dev/kvm -v "$d:/b:ro" {artifacts.BENCH_BUILDER} '
              f'sh -c {shlex.quote(guest)} < /dev/null || true')
    try:
        with open(pack, 'rb') as f:
            r = subprocess.run(['ssh', '-o', 'BatchMode=yes', host, script], stdin=f, capture_output=True,
                               timeout=cap + 2 * APT_TIMEOUT + 300)
    except subprocess.TimeoutExpired:
        subprocess.run(['ssh', '-o', 'BatchMode=yes', host, f'docker kill {name}'], capture_output=True, timeout=60)
        raise
    finally:
        pack.unlink(missing_ok=True)
    text = r.stdout.decode(errors='replace')
    (work / 'native.txt').write_text(text)
    return BootResult(end_of(text) is not None, text, f'booted under KVM on {host}' +
                      ('' if end_of(text) else f': {r.stderr.decode(errors="replace")[-400:]}'), _reference(r.stderr))


def write_reference(fam_dir: pathlib.Path, suite: str, img: pathlib.Path, br: BootResult) -> None:
    """`<suite>-reference.txt` beside the suite: the machine a seeding run recorded its answers on (CPU, kernel, how it
    ran: chroot, or KVM with its CPU model and QEMU) and the image it ran (its sha256 and cache key)."""
    sha = json.loads((img / 'manifest.json').read_text())['image']['sha256'] if (img / 'manifest.json').is_file() else '?'
    lines = [f'# The x86 reference machine of the {suite} answers (testkit/harness/campaign.py writes it when seeding).',
             *(f'{k}: {v}' for k, v in sorted(br.reference.items())), f'image: sha256 {sha}, cache entry {img.name}']
    (fam_dir / f'{suite}-reference.txt').write_text('\n'.join(lines) + '\n')


_LIVE: set = set()          # natemu processes this worker started (each in its own session: a worker's group kill misses them)
VM_GROUPS_ENV = 'TESTKIT_VM_GROUPS'


def _record_group(p: subprocess.Popen, program: str) -> None:
    """Note a VM's group (its own session) in the unit's file (VM_GROUPS_ENV, set by the run loop), which stops a group
    still there when the unit ends: a worker killed outright runs no handler (testkit.runloop.reap_vm_groups)."""
    if f := os.environ.get(VM_GROUPS_ENV):
        with open(f, 'a') as out: out.write(f'{p.pid}\t{program}\n')


def _on_term() -> None:
    """Once per process: on SIGTERM (the run loop stopping this worker: Ctrl-C, a unit timeout, the controller ending
    or dying) stop every natemu this worker started before exiting, so no VM outlives its unit or its cap. It then
    raises KeyboardInterrupt, which ends the pytest session (a SystemExit inside a test only fails that test, and the
    next one would boot another VM)."""
    if getattr(_on_term, 'done', False) or threading.current_thread() is not threading.main_thread(): return
    prev = signal.getsignal(signal.SIGTERM)

    def handler(signum, frame):
        for p in list(_LIVE): _stop(p)
        if callable(prev): prev(signum, frame)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handler)
    atexit.register(lambda: [_stop(p) for p in list(_LIVE)])
    _on_term.done = True


STOPS = ((signal.SIGINT, 10), (signal.SIGKILL, 5))      # _stop's signals and the seconds it waits after each
STOP_WAIT = sum(w for _, w in STOPS)                   # the longest a stop takes (testkit.runloop waits longer)


def _stop(p: subprocess.Popen) -> None:
    """SIGINT first (natemu's clean stop, as boot.py does), then SIGKILL to the whole group (fexc workers included)."""
    for sig, wait in STOPS:
        if p.poll() is not None: return
        try: os.killpg(p.pid, sig)
        except ProcessLookupError: return
        try: p.wait(wait)
        except subprocess.TimeoutExpired: continue


# --- the serial output ---

TAGS = re.compile(r'(CT-RESULT|CT-SKIP|CT-OUT|CT-LOG|CT-CASE|CT-DMESG|PUB-RESULT) ')


def _kv(words) -> dict:
    return dict(w.split('=', 1) for w in words if '=' in w)


def parse(serial: str) -> list[tuple[str, dict]]:
    """The results of one boot, in output order: (test name, fields). Fields are the `k=v` words of its
    `CT-RESULT <kind> <name> k=v ...` line, plus `kind`, plus `extra`: the lines its verdict may need, namely its own
    `CT-OUT <name> | ...` and `CT-LOG <name> | ...` lines (a stress result `NAME.rI` gets NAME's), the `CT-CASE ID STATUS`
    lines of its test cases (CPython: ID starts with the module), then every `CT-DMESG` line of the boot.
    `CT-SKIP <kind> <name> k=v ...` gives a result with `ct_skip = True`. `PUB-RESULT id=NAME rc=N` (one public test
    per boot) gives a result of kind `public` whose `extra` is the whole serial output, which its classifier reads.
    A tag may follow other text on the line (console noise); everything before it is dropped."""
    recs, logs, cases, dmesg = [], {}, [], []
    lines = serial.splitlines()
    for raw in lines:
        m = TAGS.search(raw)
        if not m: continue
        line, tag = raw[m.start():], m.group(1)
        if tag in ('CT-RESULT', 'CT-SKIP'):
            w = line.split()
            if len(w) < 3: continue
            f = {'kind': w[1], **_kv(w[3:])}
            if tag == 'CT-SKIP': f['ct_skip'] = True
            recs.append((w[2], f))
        elif tag in ('CT-OUT', 'CT-LOG'):
            w = line.split(None, 2)
            if len(w) >= 2: logs.setdefault(w[1], []).append(line)
        elif tag == 'CT-CASE':
            body = line[len('CT-CASE '):].rsplit(None, 1)       # a case id may contain spaces (subtest parameters)
            if len(body) == 2: cases.append((body[0], line))
        elif tag == 'CT-DMESG':
            dmesg.append(line)
        elif tag == 'PUB-RESULT':
            f = _kv(line.split()[1:])
            if 'id' in f:
                recs.append((f.pop('id'), {'kind': 'public', **f, 'public': True}))
    out = []
    for name, f in recs:
        if f.pop('public', False):
            f['extra'] = list(lines)
        else:
            own = logs.get(name) or logs.get(re.sub(r'\.r\d+$', '', name), [])
            mine = [ln for cid, ln in cases if cid == name or cid.startswith(name + '.') or cid == 'test.' + name
                    or cid.startswith('test.' + name + '.')]
            f['extra'] = own + mine + dmesg
        out.append((name, f))
    return out


END = re.compile(r'^[^A-Za-z0-9]*CT-END (.*)$')      # at the start of a line (console noise before it allowed)


def end_of(serial: str) -> dict | None:
    """The fields of the boot's last line starting `CT-END k=v ...`, or None when it has none. A `CT-END` inside
    another line (a test's own output, a CT-LOG line) is not the end."""
    for raw in reversed(serial.splitlines()):
        if m := END.match(raw): return _kv(m.group(1).split())
    return None


def read_list(img: pathlib.Path, suite: str) -> list[tuple[str, list[str]]]:
    """The image's `<suite>-cases.txt`: [(part word, [tests])] in the order the list first names each part."""
    parts = {}
    for line in (img / f'{suite}-cases.txt').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        w = line.split(None, 1)
        if len(w) != 2: raise ValueError(f'{img / f"{suite}-cases.txt"}: not "PART TEST": {line!r}')
        parts.setdefault(w[0], []).append(w[1].strip())
    return list(parts.items())


def read_boot(img: pathlib.Path, suite: str) -> dict[str, dict]:
    """The image's optional `<suite>-boot.txt`: part word -> {'kernel': the image's kernel file (absent: the stock
    kernel), 'args': the other words, for the command line}. No file: {}."""
    f = img / f'{suite}-boot.txt'
    if not f.is_file(): return {}
    out = {}
    for line in f.read_text().splitlines():
        w = line.split()
        if not w or w[0].startswith('#'): continue
        opts = {'args': tuple(x for x in w[1:] if not x.startswith('kernel='))}
        for x in w[1:]:
            if x.startswith('kernel='):
                k = img / x.split('=', 1)[1]
                if k.parent != img or not k.is_file(): raise ValueError(f'{f}: {line!r}: no kernel {k.name} in the image')
                opts['kernel'] = k
        out[w[0]] = opts
    return out


# --- verdicts ---

RANK = {'passed': 0, 'unsupported': 1, 'unverified': 2, 'flaky': 3, 'failed': 4, 'changed': 4}


@functools.lru_cache(maxsize=None)
def _verdict(fam_dir: str, fam_name: str, spec: str):
    mod, fn = spec.split(':')
    return getattr(family._load_module(pathlib.Path(fam_dir) / f'{mod}.py', f'testkit_verdicts_{fam_name}_{mod}'), fn)


def judge(br: BootResult, fn, img: pathlib.Path, mode: dict | None = None) -> dict[str, tuple[str, str]]:
    """name -> (verdict, message) for every result of one boot; a test reported twice keeps its worse verdict. A
    verdict that takes `image` gets the image's folder, one that takes `mode` the run's floating-point mode
    ({'afp': present and not hidden, 'fp': fast or accurate})."""
    params = inspect.signature(fn).parameters
    kw = {**({'image': img} if 'image' in params else {}), **({'mode': mode or {}} if 'mode' in params else {})}
    out = {}
    for name, f in parse(br.serial):
        extra = f.pop('extra')
        try:
            v, msg = fn(name, f, extra, **kw)
        except Exception as e:          # a verdict that cannot read a result does not pass it
            v, msg = 'failed', f'the verdict raised {e!r} on {f}'
        if v not in RANK: v, msg = 'failed', f'the verdict returned {v!r}: {msg}'
        if name in out and RANK[out[name][0]] >= RANK[v]: continue
        out[name] = (v, msg)
    return out


def unreported(br: BootResult, fill, tests, got: dict) -> dict[str, tuple[str, str]]:
    """name -> (verdict, message) from the suite's `unreported` function, for the listed tests of this part that no
    result line reported (`got` lacks them). A test it returns None for stays missing."""
    out = {}
    for t in tests:
        if t in got: continue
        try:
            r = fill(t, br.serial)
        except Exception as e:          # as judge(): a record that cannot be read does not pass the test
            r = ('failed', f'the unreported verdict raised {e!r}')
        if r is None: continue
        v, msg = r
        if v not in RANK: v, msg = 'failed', f'the unreported verdict returned {v!r}: {msg}'
        out[t] = (v, msg)
    return out


def adjust(name: str, verdict: str, msg: str, expected: dict) -> SubResult:
    if verdict == 'missing': return SubResult(name, 'failed', msg, category='missing')
    if verdict == 'changed': return SubResult(name, 'failed', msg, category='changed')     # expected or not
    if verdict == 'flaky': return SubResult(name, 'skipped', msg, category='flaky')
    if name in expected:
        why = expected[name]
        if verdict == 'failed':
            return SubResult(name, 'skipped', f'expected: {why}' + (f' ({msg})' if msg else ''), category='expected-fail')
        if verdict == 'passed':
            return SubResult(name, 'failed', f'passed, but listed in expected ({why}): remove the entry',
                             category='unexpected-pass')
        msg = f'{msg} (listed in expected: {why})'
    if verdict == 'passed': return SubResult(name, 'passed', msg)
    if verdict in ('unsupported', 'unverified'): return SubResult(name, 'skipped', msg, category=verdict)
    return SubResult(name, 'failed', msg)


def part_args(suite: str, declared: str, env: str | None) -> str:
    """The options added to a part's word: the suite's `part_args`, or what TESTKIT_PART_ARGS (`env`) gives it.
    `env` is `;`-separated entries: `SUITE:ARGS` for that suite only, or a bare `ARGS` for every suite no entry names
    (an empty ARGS: none, the runner's defaults). An entry is a suite's when the text before its first `:` is a plain
    name (no `=` or `,`), so a value holding a colon stays bare. A suite named twice, or two bare entries, is an error."""
    if env is None: return declared.strip(',')
    named, bare = {}, None
    for entry in [e for e in env.split(';') if e.strip()] or ['']:     # empty entries (a trailing `;`) name nothing
        name, sep, rest = entry.partition(':')
        if sep and name.strip() and not re.search(r'[=,\s]', name.strip()):
            name = name.strip()
            if name in named: raise ValueError(f'{PART_ARGS_ENV}={env!r} names suite {name} twice')
            named[name] = rest.strip()
        else:
            if bare is not None: raise ValueError(f'{PART_ARGS_ENV}={env!r} has two entries for every suite')
            bare = entry.strip()
    got = named.get(suite, bare)
    return (declared if got is None else got).strip(',')


def kept_in_accurate(why: str) -> bool:
    """Whether an expected_without.afp reason names a deviation that accurate mode keeps: its leading `name:` is one
    whose mode list (tests/lib/fp_deviations.py MODES) has accurate."""
    from testkit import paths
    paths.use_test_scripts()
    import fp_deviations
    return 'accurate' in fp_deviations.MODES.get(why.split(':', 1)[0].strip(), ())


class Campaign:
    def cases(self, fam, suite):
        return [f'part-{k}' for k in range(int(suite.extra.get('parts', 1)))]

    def case_timeout(self, fam, suite, case):
        """Room for every boot a part may take (a flaky rerun is a second one)."""
        boots = 2 if suite.extra.get('flaky') or suite.extra.get('verdict_flaky') else 1
        return boots * (float(suite.extra.get('cap', 3600)) + AFTER_END + 30) + 120

    def case_needs(self, fam, suite, case, variant=None):
        """The suite's needs, with `perf` (performance cores only) for a part named in `perf_parts`, and as many cores as
        the variant has vCPUs, at most the suite's (a 1-vCPU part of public claims one): the VM's other threads (timer,
        console, disk) mostly sleep, and a part's claim is what lets the others run beside it."""
        n = suite.needs
        if case in suite.extra.get('perf_parts', ()): n = dataclasses.replace(n, perf=True)
        vcpus = variant.get('machine.vcpus') if variant else None
        if vcpus and isinstance(n.cores, int): n = dataclasses.replace(n, cores=min(n.cores, int(vcpus)))
        return n

    def requirements(self, fam, suite, variant):
        return [Requirement('artifact', 'natemu'), Requirement('artifact', 'fexc'),
                Requirement('resource', 'debian-netboot-linux'), Requirement('campaign-image', suite.name)]

    def resolve(self, req):
        if req.kind == 'resource': return [resources.fetch(req.name)]
        if req.kind == 'campaign-image': return [image(req.name)]
        return artifacts.resolve(req.name)

    def run(self, batch, config):
        from testkit import runloop
        rundir = runloop.run_dir(config)
        out = {}
        for it in batch:
            try:
                out[it.nodeid] = self.run_part(it.fam, it.suite, it.case, it.variant, rundir)
            except Exception as e:          # a bad list or verdict module fails this part, not the batch
                out[it.nodeid] = CaseResult('failed', f'{type(e).__name__}: {e}')
        return out

    def run_part(self, fam, suite, case, variant, rundir: pathlib.Path) -> CaseResult:
        ex = suite.extra
        k = int(case.removeprefix('part-'))
        cap = float(ex.get('cap', 3600))
        vcpus = int(variant.get('machine.vcpus', 4))
        expected, flaky = dict(ex.get('expected', {})), dict(ex.get('flaky', {}))
        without = {f: dict(d) for f, d in ex.get('expected_without', {}).items()}
        off = os.environ.get('NATEMU_FEXC_FEATURE_OFF', '').split(',')      # hidden (a host.afp = false variant)
        have = {f: v and f not in off for f, v in host.detect().features.items()}
        fp = variant.get('engine.fp') or os.environ.get('NATEMU_FP') or 'fast'   # an engine.fp variant sets NATEMU_FP
        accurate = fp == 'accurate'
        mode = {'afp': bool(have.get('afp')), 'fp': fp}
        env = {'NATEMU_FP': fp} if variant.get('engine.fp') else {}
        if ex.get('check_reuse'): env['NATEMU_CHECK_REUSE'] = '1'
        boot_env = {'env': env} if env else {}
        for f, d in without.items():
            if f in have and not have[f]:
                expected.update({t: why for t, why in d.items() if not (f == 'afp' and accurate) or kept_in_accurate(why)})
        img = image(suite.name)
        parts = read_list(img, suite.name)
        declared = int(ex.get('parts', 1))
        if len(parts) != declared:
            return CaseResult('failed', f'{img / f"{suite.name}-cases.txt"} has {len(parts)} parts '
                                        f'({", ".join(p for p, _ in parts[:8])}), the suite declares parts = {declared}')
        part, tests = parts[k]
        fn = _verdict(str(fam.dir), fam.name, ex['verdict'])
        fill = _verdict(str(fam.dir), fam.name, ex['unreported']) if ex.get('unreported') else None
        ct = str(ex.get('ct', suite.name))
        work = rundir / 'campaign' / fam.name / suite.name / (variant.id or 'default') / case
        cpio = img / f'{suite.name}.cpio.zst'
        how = read_boot(img, suite.name).get(part, {})      # the part's own kernel and command-line words, if any

        args = part_args(suite.name, str(ex.get('part_args', '')), os.environ.get(PART_ARGS_ENV))
        booted = f'{part},{args}' if args else part         # the kernel's part= word: the list's, plus the suite's options

        def results(br: BootResult) -> dict[str, tuple[str, str]]:
            got = judge(br, fn, img, mode)
            return got if fill is None else {**unreported(br, fill, tests, got), **got}

        x86 = os.environ.get('NATEMU_X86_HOST')
        seed = suite.reference == 'x86' and os.environ.get('TESTKIT_SEED_ANSWERS')     # record, on the reference machine
        if seed and not x86: return CaseResult('failed', 'seeding needs NATEMU_X86_HOST=user@host')

        def once(w: pathlib.Path) -> BootResult:
            if not seed: return boot(cpio, ct, booted, vcpus, w, cap, **how, **boot_env)
            if ex.get('reference_boot') == 'kvm': return native_vm(cpio, ct, booted, vcpus, x86, cap, w, **how)
            return native(cpio, ct, booted, x86, cap, w)

        first = once(work / 'boot-1')
        if seed and first.reached: write_reference(fam.dir, suite.name, img, first)
        got = results(first)
        listed = set(tests)
        missing = 'listed in the image for part ' + part + ' but not in the output' + \
                  ('' if first.reached else ' (the boot did not reach CT-END)')
        verdicts = {t: got.get(t, ('missing', missing)) for t in tests}

        notes = []
        if not ex.get('verdict_flaky'):     # a verdict's own flaky counts only where the suite allows it
            verdicts = {t: ('failed', f'{m} (the verdict said flaky; the suite has no verdict_flaky)') if v == 'flaky'
                        else (v, m) for t, (v, m) in verdicts.items()}
        retry = [t for t in tests if (t in flaky and verdicts[t][0] in ('failed', 'missing')) or verdicts[t][0] == 'flaky']
        if retry:
            second = once(work / 'boot-2')
            again = results(second)
            notes.append(f'rerun for {len(retry)} flaky test(s): {second.note or ("CT-END" if second.reached else "no CT-END")}')
            for t in retry:
                v2 = again.get(t, ('missing', 'not in the rerun\'s output'))
                if v2[0] in ('passed', 'unsupported', 'unverified'):
                    verdicts[t] = (v2[0], f'{v2[1]} (on the rerun; the first boot: {verdicts[t][1]})'.strip())
                elif t not in flaky:        # the verdict's own flaky: only a second flaky stays flaky
                    verdicts[t] = (v2[0] if v2[0] == 'flaky' else 'failed',
                                   f'first boot: {verdicts[t][1]} | rerun: {v2[1]}')
                else:
                    verdicts[t] = ('flaky', f'{flaky[t]}; failed on both boots: {verdicts[t][1]} | rerun: {v2[1]}')

        sub = [adjust(t, *verdicts[t], expected) for t in tests]
        for name, (v, msg) in got.items():
            if name not in listed:
                sub.append(SubResult(name, 'failed', f'not in the image\'s list for part {part} ({v}: {msg})'))
        if k == 0:
            every = {t for _, ts in parts for t in ts}
            for kind, d in (('expected', dict(ex.get('expected', {}))), ('flaky', flaky),
                            *((f'expected_without.{f}', d) for f, d in without.items())):
                for name, why in d.items():
                    if name not in every:
                        sub.append(SubResult(f'{kind}:{name}', 'failed', f'the {kind} entry names no test the image '
                                             f'lists (renamed or removed?): {why}', category='stale-entry'))
            for a in ex.get('answers', []):         # answer files: a name the image does not list is stale
                af = fam.dir / a
                names = {ln.split()[0] for ln in af.read_text().splitlines() if ln.strip() and not ln.startswith('#')} \
                    if af.is_file() else set()
                for name in sorted(names - every):
                    sub.append(SubResult(f'answers:{a}:{name}', 'failed', f'{a} has an answer for {name}, which the image '
                                         'does not list (renamed or removed? reseed: docs/testing.md)', category='stale-entry'))
            for f in without:
                if f not in have:
                    sub.append(SubResult(f'expected_without.{f}', 'failed', f'this host reports no FEAT_{f.upper()} '
                                         f'(hw.optional.arm; misspelt?): its entries are not applied', category='stale-entry'))

        problems = []
        end = end_of(first.serial)
        if not first.reached or end is None:
            tail = '\n'.join(first.serial.splitlines()[-TAIL:])
            problems.append(f'no CT-END: {first.note or "the boot ended without it"}; the last {TAIL} serial lines:\n{tail}')
        elif end.get('part') not in (part, booted) or end.get('suite') != ct:
            problems.append(f'CT-END is for suite={end.get("suite")} part={end.get("part")}, not suite={ct} part={part}')
        counts = {}
        for s in sub:
            c = s.category or s.outcome
            counts[c] = counts.get(c, 0) + 1
        summary = f'part {part}' + (f' ({args})' if args else '') + f' at {vcpus} vCPUs: ' + \
                  ', '.join(f'{n} {c}' for c, n in sorted(counts.items())) + \
                  (f'; {first.note}' if first.note else '') + ''.join(f'; {n}' for n in notes)
        failed = [s for s in sub if s.outcome == 'failed']
        if failed: problems.insert(0, f'{len(failed)} test(s) failed: ' + ', '.join(s.name for s in failed[:10]) +
                                   (' ...' if len(failed) > 10 else ''))
        if problems:   # own: the part failed for more than its tests (no CT-END, another part's), which no known bug covers
            return CaseResult('failed', summary + '\n' + '\n'.join(problems) + f'\n(serial output: {work})', sub=sub,
                              own=len(problems) > (1 if failed else 0))
        return CaseResult('passed', summary, sub=sub)


HARNESS = Campaign()
