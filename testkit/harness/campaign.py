"""Linux workload campaigns (plan docs/superpowers/plans/2026-11-18-campaign-onboarding.md, Task 6-7): each case boots one
part of a campaign image under natemu, with tools/correctness/boot.py's command line, and every test the image lists
for that part is judged by the campaign's own verdict on its `CT-RESULT` line. Nothing is compared with a reference
run and nothing is locked: no QEMU, no x86 machine.

    [[suite]] kind = "judged"  harness = "campaign"  tier = "campaign"
    parts = N                   cases part-0 ... part-(N-1): the image's parts, in the order its list names them
    cap = 3600                  seconds per boot (the guest's poweroff after CT-END ends it sooner)
    verdict = "verdicts:fn"     fn(name, fields, extra) -> (passed | failed | unsupported | unverified, message), from a
                                file in the family folder; a verdict that takes `image` also gets the image's folder
    expected = { test = "why (the document that records it)" }    known non-passes
    flaky = { test = "why" }    timing-sensitive tests: a failure reruns the part once
    matrix = { "machine.vcpus" = [4] }

The image is `inputs.campaign_image(suite)`: a folder holding `<suite>.cpio.zst` and `<suite>-list.txt`, every test the
image runs, one `PART TEST` line each, PART being the kernel's `part=` word for that boot (`0/4`, `mem1`, `7/1`). The
distinct PARTs, in the order the list first names them, are the parts; their number must equal `parts`.

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
import json
import os
import pathlib
import re
import signal
import subprocess
import time

from testkit import artifacts, family, resources
from testkit.harness import CaseResult, Requirement, SubResult

AFTER_END = 30.0            # seconds the guest gets to power off after CT-END (boot.py's), then it is stopped
POLL = 0.5
TAIL = 40                   # serial lines shown for a part that did not reach CT-END


@dataclasses.dataclass
class BootResult:
    reached: bool               # CT-END was printed
    serial: str
    note: str = ''              # how the boot ended (exit status, cap, time), for the case's message


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


def boot(image: pathlib.Path, suite: str, part: str, vcpus: int, work: pathlib.Path, cap: float) -> BootResult:
    """One boot of `image` under natemu, as tools/correctness/boot.py runs it (the netboot 6.12.107 kernel, its command
    line, compile cache off), with the serial console and natemu's output in `work`, a folder no other boot uses. The
    image is only read. Ends AFTER_END seconds after CT-END, when natemu exits, or at `cap` seconds."""
    n, fexc = artifacts.natemu(), artifacts.fexc()
    kernel = resources.fetch('debian-netboot-linux')
    work.mkdir(parents=True, exist_ok=True)
    serial = work / 'serial.txt'
    serial.unlink(missing_ok=True)
    cmdline = f'console=ttyS0 mitigations=off nokaslr ct={suite} part={part}'
    cmd = [str(n.stub), '--runtime', str(n.runtime), '--fexc', str(fexc), '--kernel', str(kernel), '--initrd', str(image),
           '--cmdline', cmdline, '--vcpus', str(vcpus), '--serial', str(serial)]
    (work / 'cmd.json').write_text(json.dumps(cmd) + '\n')
    env = dict(os.environ, NATEMU_COMPILE_CACHE='off')
    env.pop('NATEMU_PERF_LOG', None)
    t0 = time.monotonic()
    end_at, how = None, None
    with open(work / 'stdout.txt', 'wb') as out, open(work / 'stderr.txt', 'wb') as err:
        p = subprocess.Popen(cmd, env=env, stdout=out, stderr=err, start_new_session=True)
        try:
            while p.poll() is None:
                now = time.monotonic()
                if now - t0 >= cap:
                    how = f'stopped at the cap of {cap:.0f} s'; break
                if end_at is None and 'CT-END' in _read(serial): end_at = now
                if end_at is not None and now - end_at > AFTER_END:
                    how = f'stopped {AFTER_END:.0f} s after CT-END'; break
                time.sleep(POLL)
        finally:
            _stop(p)
    took = time.monotonic() - t0
    text = _read(serial)
    note = f'{how or f"natemu exited with status {p.returncode}"} after {took:.0f} s'
    if end_of(text) is None:
        tail = _read(work / 'stderr.txt').strip()[-400:]
        if tail: note += f'; natemu stderr: {tail}'
    return BootResult(end_of(text) is not None, text, note)


def _stop(p: subprocess.Popen) -> None:
    """SIGINT first (natemu's clean stop, as boot.py does), then SIGKILL to the whole group (fexc workers included)."""
    for sig, wait in ((signal.SIGINT, 10), (signal.SIGKILL, 5)):
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


def end_of(serial: str) -> dict | None:
    """The fields of the boot's last `CT-END k=v ...` line, or None when it has none."""
    for raw in reversed(serial.splitlines()):
        i = raw.find('CT-END ')
        if i >= 0: return _kv(raw[i:].split()[1:])
    return None


def read_list(img: pathlib.Path, suite: str) -> list[tuple[str, list[str]]]:
    """The image's `<suite>-list.txt`: [(part word, [tests])] in the order the list first names each part."""
    parts = {}
    for line in (img / f'{suite}-list.txt').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        w = line.split(None, 1)
        if len(w) != 2: raise ValueError(f'{img / f"{suite}-list.txt"}: not "PART TEST": {line!r}')
        parts.setdefault(w[0], []).append(w[1].strip())
    return list(parts.items())


# --- verdicts ---

RANK = {'passed': 0, 'unsupported': 1, 'unverified': 2, 'failed': 3}


@functools.lru_cache(maxsize=None)
def _verdict(fam_dir: str, fam_name: str, spec: str):
    mod, fn = spec.split(':')
    return getattr(family._load_module(pathlib.Path(fam_dir) / f'{mod}.py', f'testkit_verdicts_{fam_name}_{mod}'), fn)


def judge(br: BootResult, fn, img: pathlib.Path) -> dict[str, tuple[str, str]]:
    """name -> (verdict, message) for every result of one boot; a test reported twice keeps its worse verdict."""
    wants_image = 'image' in inspect.signature(fn).parameters
    out = {}
    for name, f in parse(br.serial):
        extra = f.pop('extra')
        try:
            v, msg = fn(name, f, extra, image=img) if wants_image else fn(name, f, extra)
        except Exception as e:          # a verdict that cannot read a result does not pass it
            v, msg = 'failed', f'the verdict raised {e!r} on {f}'
        if v not in RANK: v, msg = 'failed', f'the verdict returned {v!r}: {msg}'
        if name in out and RANK[out[name][0]] >= RANK[v]: continue
        out[name] = (v, msg)
    return out


def adjust(name: str, verdict: str, msg: str, expected: dict) -> SubResult:
    if verdict == 'missing': return SubResult(name, 'failed', msg, category='missing')
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


class Campaign:
    def cases(self, fam, suite):
        return [f'part-{k}' for k in range(int(suite.extra.get('parts', 1)))]

    def case_timeout(self, fam, suite, case):
        """Room for every boot a part may take (a flaky rerun is a second one)."""
        boots = 2 if suite.extra.get('flaky') else 1
        return boots * (float(suite.extra.get('cap', 3600)) + AFTER_END + 30) + 120

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
        img = image(suite.name)
        parts = read_list(img, suite.name)
        declared = int(ex.get('parts', 1))
        if len(parts) != declared:
            return CaseResult('failed', f'{img / f"{suite.name}-list.txt"} has {len(parts)} parts '
                                        f'({", ".join(p for p, _ in parts[:8])}), the suite declares parts = {declared}')
        part, tests = parts[k]
        fn = _verdict(str(fam.dir), fam.name, ex['verdict'])
        work = rundir / 'campaign' / fam.name / suite.name / (variant.id or 'default') / case
        cpio = img / f'{suite.name}.cpio.zst'

        first = boot(cpio, suite.name, part, vcpus, work / 'boot-1', cap)
        got = judge(first, fn, img)
        listed = set(tests)
        missing = 'listed in the image for part ' + part + ' but not in the output' + \
                  ('' if first.reached else ' (the boot did not reach CT-END)')
        verdicts = {t: got.get(t, ('missing', missing)) for t in tests}

        notes = []
        retry = [t for t in tests if t in flaky and verdicts[t][0] in ('failed', 'missing')]
        if retry:
            second = boot(cpio, suite.name, part, vcpus, work / 'boot-2', cap)
            again = judge(second, fn, img)
            notes.append(f'rerun for {len(retry)} flaky test(s): {second.note or ("CT-END" if second.reached else "no CT-END")}')
            for t in retry:
                v2 = again.get(t, ('missing', 'not in the rerun\'s output'))
                if v2[0] in ('passed', 'unsupported', 'unverified'):
                    verdicts[t] = (v2[0], f'{v2[1]} (on the rerun; the first boot: {verdicts[t][1]})'.strip())
                else:
                    verdicts[t] = ('flaky', f'{flaky[t]}; failed on both boots: {verdicts[t][1]} | rerun: {v2[1]}')

        sub = [adjust(t, *verdicts[t], expected) for t in tests]
        for name, (v, msg) in got.items():
            if name not in listed:
                sub.append(SubResult(name, 'failed', f'not in the image\'s list for part {part} ({v}: {msg})'))
        if k == 0:
            every = {t for _, ts in parts for t in ts}
            for kind, d in (('expected', expected), ('flaky', flaky)):
                for name, why in d.items():
                    if name not in every:
                        sub.append(SubResult(f'{kind}:{name}', 'failed', f'the {kind} entry names no test the image '
                                             f'lists (renamed or removed?): {why}', category='stale-entry'))

        problems = []
        end = end_of(first.serial)
        if not first.reached or end is None:
            tail = '\n'.join(first.serial.splitlines()[-TAIL:])
            problems.append(f'no CT-END: {first.note or "the boot ended without it"}; the last {TAIL} serial lines:\n{tail}')
        elif end.get('part') != part or end.get('suite') != suite.name:
            problems.append(f'CT-END is for suite={end.get("suite")} part={end.get("part")}, not suite={suite.name} part={part}')
        counts = {}
        for s in sub:
            c = s.category or s.outcome
            counts[c] = counts.get(c, 0) + 1
        summary = f'part {part} at {vcpus} vCPUs: ' + ', '.join(f'{n} {c}' for c, n in sorted(counts.items())) + \
                  (f'; {first.note}' if first.note else '') + ''.join(f'; {n}' for n in notes)
        failed = [s for s in sub if s.outcome == 'failed']
        if failed: problems.insert(0, f'{len(failed)} test(s) failed: ' + ', '.join(s.name for s in failed[:10]) +
                                   (' ...' if len(failed) > 10 else ''))
        if problems:
            return CaseResult('failed', summary + '\n' + '\n'.join(problems) + f'\n(serial output: {work})', sub=sub)
        return CaseResult('passed', summary, sub=sub)


HARNESS = Campaign()
