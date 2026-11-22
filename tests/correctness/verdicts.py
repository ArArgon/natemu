"""Each campaign's own verdict on one test (testkit/harness/campaign.py; plan
docs/superpowers/plans/2026-11-18-campaign-onboarding.md, Decision 2). A verdict reads the fields of the test's
`CT-RESULT <kind> <name> k=v ...` line (formats: the tools/correctness/run-*.sh headers) and `extra`, the lines that
belong to it (its CT-OUT/CT-LOG output, CT-CASE lines, the boot's CT-DMESG kernel log), and returns
(passed | failed | unsupported | unverified, message). A `CT-SKIP` line arrives as fields with `ct_skip = True`.
Nothing is compared with a reference run: each test decides by its own exit status, counts or model."""
import functools
import importlib.util
import pathlib
import re

from testkit import paths

CT = paths.repo_root() / 'tools' / 'correctness'
SHOWN = 12              # output lines quoted in a failure's message


@functools.cache
def _tool(name: str):
    """A tools/correctness script (hyphenated name) as a module."""
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), CT / f'{name}.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _output(extra, n=SHOWN) -> str:
    """The test's own CT-OUT / CT-LOG lines (their text after `|`), the last n of them."""
    body = [ln.split('|', 1)[1].strip() for ln in extra if ln.startswith(('CT-OUT ', 'CT-LOG ')) and '|' in ln]
    return ('\n' + '\n'.join(body[-n:])) if body else ''


def _fields(f, *keys) -> str:
    return ' '.join(f'{k}={f.get(k)}' for k in keys if k in f)


def openssl(name, f, extra):
    """run-openssl.sh: `CT-RESULT openssl MASK:RECIPE rc=EXIT ok=N notok=N`. Passes with rc 0 and no `not ok` subtest."""
    if f.get('rc') == '0' and f.get('notok') == '0':
        return 'passed', f'ok={f.get("ok")}'
    return 'failed', _fields(f, 'rc', 'ok', 'notok') + _output(extra)


def torture(name, f, extra):
    """run-torture.sh: `CT-RESULT torture NAME.OPT rc=EXIT out=MD5`. The programs abort on a wrong result: rc 0 passes."""
    if f.get('rc') == '0': return 'passed', ''
    return 'failed', _fields(f, 'rc', 'out')


def ltp(name, f, extra):
    """run-ltp.sh: `CT-RESULT ltp RUNTEST:NAME rc=EXIT sec=S`. 0 passes, 32 (TCONF) is unsupported, anything else
    (TFAIL 1, TBROK 2, TWARN 4, ored; 137 killed at the outer timeout) fails."""
    rc = f.get('rc')
    if rc == '0': return 'passed', ''
    if rc == '32': return 'unsupported', 'TCONF' + _output(extra)
    return 'failed', _fields(f, 'rc', 'sec') + _output(extra)


def _glibc(name, f, extra):
    rc = f.get('rc')
    if rc == '0': return 'passed', ''
    if rc == '77': return 'unsupported', 'UNSUPPORTED (exit 77)'
    if rc == 'skip': return 'unsupported', "in the image's skip list"
    return 'failed', f'rc={rc}' + _output(extra)


def libm(name, f, extra):
    """run-libm.sh: `CT-RESULT libm NAME rc=EXIT|skip`. glibc's verdicts: 0 PASS, 77 UNSUPPORTED (the AVX-512-only
    tests on natemu, by themselves), else FAIL."""
    return _glibc(name, f, extra)


def gstr(name, f, extra):
    """run-gstr.sh: `CT-RESULT gstr NAME rc=EXIT|skip`, judged as libm."""
    return _glibc(name, f, extra)


def x86st(name, f, extra):
    """run-x86st.sh: `CT-RESULT x86st NAME rc=EXIT ok=N fail=N skip=N out=MD5` ([OK]/[FAIL]/[SKIP] counts of the test's
    own lines), or `CT-SKIP x86st NAME` (in the image's skip list). Passes with rc 0 and fail=0; a test that only
    skipped (or exits 4, kselftest's KSFT_SKIP, with no failure) is unsupported."""
    if f.get('ct_skip'): return 'unsupported', "in the image's skip list: not run"
    rc, fail = f.get('rc'), f.get('fail')
    if fail == '0' and (rc == '4' or (rc == '0' and f.get('ok') == '0' and f.get('skip', '0') != '0')):
        return 'unsupported', 'skipped: ' + _fields(f, 'rc', 'ok', 'skip')
    if rc == '0' and fail == '0':
        return 'passed', _fields(f, 'ok', 'skip')
    return 'failed', _fields(f, 'rc', 'ok', 'fail', 'skip') + _output(extra)


TORTURE32 = pathlib.Path(__file__).parent / 'torture32.txt'


def torture32(name, f, extra):
    """run-torture.sh on the i686 build of the torture tests: `CT-RESULT torture NAME.OPT rc=EXIT out=MD5`. Passes with
    the exit status and output hash of the x86 reference machine's native run (tests/correctness/torture32.txt, `NAME rc
    out` lines; a test that fails there, built with flags that do not suit i686, must fail the same way; for a test a
    signal ended only the status is compared, as for one whose output differed between two seeding runs, recorded as
    `rc *`). Under
    TESTKIT_SEED_ANSWERS=1 the result is the reference machine's and is recorded (testkit/harness/campaign.py, native)."""
    import os
    file = TORTURE32
    known = dict(ln.split(None, 1) for ln in file.read_text().splitlines()) if file.is_file() else {}
    rc = f.get('rc', '')
    mine = f'{rc} {f.get("out")}' if rc.isdigit() and int(rc) < 128 else rc    # killed by a signal: timeout's own message varies
    if os.environ.get('TESTKIT_SEED_ANSWERS'):          # a second seeding run finds output that varies (an address printed)
        known[name] = f'{rc} *' if known.get(name, mine) != mine and known[name].split()[0] == rc else mine
        file.write_text(''.join(f'{k} {v}\n' for k, v in sorted(known.items())))
        return 'unverified', 'recorded'
    if name not in known: return 'failed', f'no answer in {file.name} (seed it: docs/testing.md)'
    want = known[name].split()
    if want[:1] == [rc] and (len(mine.split()) == 1 or want[1:] in (['*'], mine.split()[1:])):
        return 'passed', '' if mine.startswith('0 ') else f'fails on the reference machine too (rc {rc})'
    return 'failed', f'rc and output {mine}, the x86 reference machine\'s {known[name]}'


LTP32 = pathlib.Path(__file__).parent / 'ltp32.txt'
LIBM32 = pathlib.Path(__file__).parent / 'libm32.txt'


def _x86_rc(name, f, extra, file, own):
    """`own`, the suite's own verdict from the test's exit status, beside the x86 reference machine's status for the
    same test of the same image (`file`: `NAME RC` lines, `*` where two seeding runs differed). A test natemu reports
    unsupported that passed there fails: natemu lacks something the real machine has. A failure's message gives the
    reference's status. Under TESTKIT_SEED_ANSWERS=1 the status is the reference machine's: it is recorded (under a
    lock: parts may seed at once), and the result is unverified."""
    import fcntl, os
    rc = f.get('rc', '')
    if os.environ.get('TESTKIT_SEED_ANSWERS'):
        with open(file, 'a+') as h:
            fcntl.flock(h, fcntl.LOCK_EX); h.seek(0)
            known = dict(ln.split(None, 1) for ln in h.read().splitlines())
            known[name] = rc if known.get(name, rc) == rc else '*'
            h.seek(0); h.truncate(); h.write(''.join(f'{k} {v}\n' for k, v in sorted(known.items())))
        return 'unverified', 'recorded'
    known = dict(ln.split(None, 1) for ln in file.read_text().splitlines()) if file.is_file() else {}
    if name not in known: return 'failed', f'no answer in {file.name} (seed it: docs/testing.md)'
    v, msg = own(name, f, extra)
    if v == 'unsupported' and known[name] == '0':
        return 'failed', f'{msg}: unsupported here, passed on the x86 reference machine'
    return v, f'{msg} (the x86 reference machine: rc={known[name]})' if v == 'failed' else msg


def ltp32(name, f, extra):
    """run-ltp.sh on the i686 build of LTP: judged as ltp, beside the x86 reference machine's status (tests/correctness/
    ltp32.txt; the same image and kernel booted there under KVM, testkit/harness/campaign.py native_vm)."""
    return _x86_rc(name, f, extra, LTP32, ltp)


def libm32(name, f, extra):
    """run-libm.sh on the i686 build of glibc's math tests: judged as libm, beside the x86 reference machine's status
    (tests/correctness/libm32.txt; the image run there in a chroot, testkit/harness/campaign.py native)."""
    return _x86_rc(name, f, extra, LIBM32, _glibc)


I386 = pathlib.Path(__file__).parent / 'i386'
I386_SEEDED = set()         # the programs this seeding process has recorded: their other builds must print the same


def i386(name, f, extra):
    """run-i386.sh: `CT-RESULT i386 NAME rc=EXIT out=MD5` after the test's output as `CT-LOG NAME | LINE` lines. Passes
    with rc 0 and exactly the output in tests/correctness/i386/PROGRAM.txt, which the x86 reference machine printed
    (NAME.static, NAME.dyn and NAME.64 are one PROGRAM and share the file). Under TESTKIT_SEED_ANSWERS=1 the output is the
    reference machine's (testkit/harness/campaign.py, native): it is recorded instead, and the result is unverified."""
    import difflib, os
    head = f'CT-LOG {name} | '
    got = [ln[len(head):].rstrip() for ln in extra if ln.startswith(head.rstrip())]
    program, _, link = name.partition('.')
    want = I386 / f'{program}.txt'
    if f.get('rc') != '0': return 'failed', _fields(f, 'rc', 'out') + _output(extra)
    if os.environ.get('TESTKIT_SEED_ANSWERS'):
        text = '\n'.join(got) + '\n'
        if program in I386_SEEDED and want.read_text() != text:
            return 'failed', f'{name} and another build of {program} print different output on the reference machine'
        want.parent.mkdir(exist_ok=True); want.write_text(text); I386_SEEDED.add(program)
        return 'unverified', f'recorded {want.name}'
    if not want.is_file(): return 'failed', f'no expected output {want} (seed it: docs/testing.md)'
    exp = want.read_text().splitlines()
    if got == exp: return 'passed', ''
    diff = list(difflib.unified_diff(exp, got, 'x86 reference', 'natemu', lineterm='', n=0))
    return 'failed', 'output differs from the x86 reference machine\'s:\n' + '\n'.join(diff[:3 * SHOWN])


# Timing-sensitive cases (plan, format decision 3; Decision 4): exempt one by one, never their module. Each requires
# timerfd_gettime's remaining time to equal the requested one to 1 ms (or three decimals) without subtracting the
# execution before the call; the deficit is that execution (docs/restructure/correctness-cpython-string.md, "Every
# failed or errored case", and its targeted follow-up's clock brackets); QEMU fails the first one too.
CPY_FLAKY_CASES = {
    f'test.test_os.TimerfdTests.{c}': 'timerfd remaining-time assertion with under 1 ms for the execution before '
                                      'timerfd_gettime (docs/restructure/correctness-cpython-string.md, Every failed or errored case)'
    for c in ('test_timerfd_TFD_TIMER_ABSTIME', 'test_timerfd_ns_TFD_TIMER_ABSTIME', 'test_timerfd_initval')}


def cpyt(name, f, extra):
    """run-cpy.sh: `CT-RESULT cpy MODULE cases=C pass=P fail=F error=E skip=S sec=T` per regrtest module, with a
    `CT-CASE ID STATUS` line per case not passed. Fails on any failed or erroring case (named in the message); a module
    whose every case skipped is unsupported. A module whose only failures are assertion failures (status fail, never
    error: an error is the API itself failing) of cases in CPY_FLAKY_CASES, each named by a CT-CASE line and as many
    as the line's fail, with error=0, is `flaky`: the harness reruns the part once
    (verdict_flaky)."""
    bad = [ln[len('CT-CASE '):] for ln in extra if ln.startswith('CT-CASE ') and ln.rsplit(None, 1)[-1] in ('fail', 'error')]
    missing = [k for k in ('cases', 'fail', 'error') if not str(f.get(k, '')).isdigit()]
    if missing: return 'failed', 'incomplete result line (' + ', '.join(f'{k}={f.get(k, "missing")}' for k in missing) + ')'
    if f.get('fail') != '0' or f.get('error') != '0':
        shown = _fields(f, 'cases', 'fail', 'error') + ('\n' + '\n'.join(bad[:SHOWN]) if bad else '')
        fails = [b.rsplit(None, 1) for b in bad]           # (id, fail | error)
        if (f['error'] == '0' and fails and len(fails) == int(f['fail'])
                and all(st == 'fail' and i in CPY_FLAKY_CASES for i, st in fails)):
            return 'flaky', f'only timing-sensitive cases failed: {CPY_FLAKY_CASES[fails[0][0]]}; ' + shown
        return 'failed', shown
    if f.get('cases') not in (None, '0') and f.get('skip') == f.get('cases'):
        return 'unsupported', f'every case skipped ({f.get("skip")})'
    return 'passed', _fields(f, 'cases', 'skip')


# regrtest's closing verdicts (cpy-check.py's names) for a module that ran no case: skipped whole (the platform, a
# missing extension such as Tk, a resource run-cpy.sh does not enable) or with nothing to run.
CPY_UNSUPPORTED = {'skipped', 'skipped (resource denied)', 'run no tests'}
_PROGRESS = re.compile(r'\[\s*\d+/\d+(?:/\d+)?\] (\S+)(.*)$')


def cpyt_unreported(name, serial):
    """A listed module with no `CT-RESULT cpy` line (it reported no JUnit case): regrtest's own verdict for it, from
    the boot's `CT-LOG |` lines. When regrtest finished (its `== Tests result:` line), its closing summary decides, as
    cpy-check.py reads it: skipped whole (or "run no tests") is unsupported, ok passes, anything else (failed, env
    changed, ...) fails. Without that line (regrtest died) only the module's own progress line counts: `passed`
    passes, `skipped`/`run no tests` is unsupported, anything else fails. A progress line saying failed or crashed
    fails whatever the summary says. None (left missing) when regrtest named the module nowhere."""
    lines = serial.splitlines()
    log = [ln[ln.find('CT-LOG | ') + 9:] for ln in lines if 'CT-LOG | ' in ln]
    prog = [m.group(2).strip() for m in (_PROGRESS.search(b) for b in log if not b.startswith(' ')) if m and m.group(1) == name]
    said = [ln.strip() for ln in log if re.search(r'\] ' + re.escape(name) + r'(\s|$)', ln)][:2]
    finished = any(b.startswith('== Tests result:') for b in log)
    own = prog[-1] if prog else ''
    if finished:
        v = _tool('cpy-check').load_lines([lines])['verdict'].get(name)
        if v is None: return None
    elif prog:
        v = 'ok' if own.startswith('passed') else next((u for u in sorted(CPY_UNSUPPORTED, key=len, reverse=True)
                                                       if own.startswith(u)), own or 'no result')
    else:
        return None
    msg = f'regrtest: {v}, no JUnit case' + ('' if finished else ' (regrtest did not finish: no closing summary)') + \
          ''.join(f'\n{s}' for s in said)
    if own.startswith(('failed', 'crashed')): return 'failed', msg
    if v in CPY_UNSUPPORTED: return 'unsupported', msg
    if v == 'ok': return 'passed', msg
    return 'failed', msg


CPYB_BUILD_FAILED = re.compile(r'Could not build|failed to build|failed on import|could not be imported', re.I)
# make's closing summary, "Checked 112 modules (... 1 missing, 0 failed on import)": fails only on a nonzero count
# ("missing" is a module whose dependency configure did not find, as natively)
CPYB_SUMMARY = re.compile(r'Checked \d+ modules \(.*\b(\d+) failed on import\)')


def _cpyb_build_failed(line):
    m = CPYB_SUMMARY.search(line)
    return int(m.group(1)) > 0 if m else bool(CPYB_BUILD_FAILED.search(line))


def cpyb(name, f, extra):
    """cpy-build.sh (run-cpyb.sh): `CT-RESULT cpyb STEP rc=RC sec=S` per build step. Every step must exit 0, and make
    fails when its log names an extension module that could not be built (run-cpyb.sh's `CT-LOG make | ` lines). The
    smoke modules the built interpreter then runs (run-cpy.sh's `CT-RESULT cpy MODULE ...` lines) are judged as
    cpyt's, except that a smoke module whose every case skipped fails: they are there to exercise what the guest
    built."""
    if f.get('kind') == 'cpy':
        v, msg = cpyt(name, f, extra)
        if v == 'unsupported': return 'failed', f'smoke module not exercised: {msg}'
        return v, msg
    if f.get('rc') != '0': return 'failed', _fields(f, 'rc', 'sec')
    bad = [ln for ln in extra if ln.startswith('CT-LOG make | ') and _cpyb_build_failed(ln)]
    if name == 'make' and bad: return 'failed', 'rc=0, but:\n' + '\n'.join(bad[:SHOWN])
    return 'passed', ''


def cpyb_unreported(name, serial):
    """cpyt_unreported for a smoke module, except that skipped whole or "run no tests" fails (an extension the guest
    build lacks, test_ssl's "No module named '_ssl'", must not pass the part)."""
    r = cpyt_unreported(name, serial)
    if r and r[0] == 'unsupported': return 'failed', f'smoke module not exercised: {r[1]}'
    return r


TIMEOUT = 240           # run-stress.sh's default; its `to=` field gives the run's own (part option timeout=T)


def _hogs(extra) -> int | None:
    """The number of instances stress-ng started, from its first `dispatching hogs: 4 vm, 1 fork, ...` line (CT-LOG),
    or None without one. A list line naming other stressors besides its own (family mix: `--fork 1 ...`) starts more
    instances than its n, and its `passed:` count covers them all."""
    for ln in extra:
        if ln.startswith('CT-LOG ') and 'dispatching hogs:' in ln:
            got = re.findall(r'(\d+) [A-Za-z0-9_-]+', ln.split('dispatching hogs:', 1)[1])
            return sum(map(int, got)) if got else None
    return None


def stress(name, f, extra):
    """run-stress.sh: `CT-RESULT stress STRESSOR[:LABEL].rI rc=EXIT n=N pass=N skip=N fail=#fail-lines bogo=OPS sec=S
    to=T`, with the boot's kernel log as CT-DMESG lines. Passes with rc 0, no `fail:` lines and nothing in the kernel
    log that stress-check.py's scan flags (Oops, BUG, WARNING, lockups, stalls, ...; stress-ng's own OOM kills are expected).
    Every field must be there and pass + skip must account for all n instances (a line without stress-ng's summary
    fails), or for a line that starts several stressors at once, for every instance stress-ng dispatched. A stressor
    that ran to its timeout (T, 240 s by default) passes on its own verification; the timeout is noted (plan, format
    decision 2), unless it did no operation at all (bogo 0 or none reported): that run livelocked or never got going,
    and fails. The kernel log covers the whole boot, so a flagged line fails every stressor of the part."""
    why = []
    nums = {}
    for k in ('rc', 'n', 'pass', 'skip', 'fail'):
        try: nums[k] = int(f[k])
        except (KeyError, ValueError): why.append(f'{k}={f.get(k, "missing")}')
    if why: return 'failed', 'incomplete result line (' + ', '.join(why) + ')' + _output(extra, 4)
    if nums['rc'] != 0: why.append(f'rc={nums["rc"]}')
    if nums['fail']: why.append(f'{nums["fail"]} fail: line(s)')
    want, what = nums['n'], 'n'
    if hogs := _hogs(extra): want, what = hogs, 'the instances stress-ng dispatched'   # a run of several stressors (mix)
    if nums['pass'] + nums['skip'] != want: why.append(f'pass {nums["pass"]} + skip {nums["skip"]} != {what} {want}')
    try: sec, timeout = float(f.get('sec', '0')), float(f.get('to', TIMEOUT))
    except ValueError: sec, timeout = 0.0, float(TIMEOUT)
    if sec >= timeout and nums['pass'] and f.get('bogo', '-') in ('0', '-'):
        why.append(f'ran to its {timeout:g} s timeout with no operation done (bogo={f.get("bogo", "-")})')
    bad = _tool('stress-check').BAD
    kernel = [ln for ln in extra if ln.startswith('CT-DMESG') and bad.search(ln)]
    if kernel: why.append(f'{len(kernel)} kernel log line(s) of note in this boot:\n' + '\n'.join(kernel[:SHOWN]))
    if why:
        fails = [ln for ln in extra if ln.startswith('CT-LOG ') and ' fail: ' in ln]
        return 'failed', '; '.join(why) + ('\n' + '\n'.join(fails[:SHOWN]) if fails else '')
    if f.get('pass') == '0' and f.get('skip', '0') not in ('0', '-'):
        return 'unsupported', f'stress-ng skipped it ({_fields(f, "pass", "skip")})' + _output(extra, 4)
    return 'passed', _fields(f, 'bogo', 'sec') + (f' (ran to the {timeout:g} s timeout)' if sec >= timeout else '')


@functools.cache
def _oracle(d: str):
    return _tool('litmus-check').oracle(d)


def _state(s) -> str:
    return '; '.join(f'{k}={v}' for k, v in s)


def litmus(name, f, extra, image=None):
    """run-litmus.sh: the litmus7 histogram as `CT-LOG FAMILY.NAME | COUNT[*:]>STATE` lines, then `CT-RESULT litmus
    FAMILY.NAME rc=EXIT obs=Never|Sometimes|Always sec=S`, or `CT-SKIP litmus FAMILY.NAME threads=T`. Judged against
    herd7's x86-TSO allowed states (the image's oracle/FAMILY.txt, read by litmus-check.py): an observed state outside
    them fails (FORBIDDEN); allowed states never observed are only noted. The histogram must be whole, or a garbled or
    lost line could hide a forbidden state: its state lines must number litmus7's `Histogram (N states)` and their
    counts sum to its `Positive: P, Negative: Q`."""
    if f.get('ct_skip'): return 'unsupported', f'needs {f.get("threads", "more")} threads: more than the guest has'
    if f.get('rc') != '0': return 'failed', _fields(f, 'rc', 'obs') + _output(extra)
    lc = _tool('litmus-check')
    hist, lines, total, declared, witnesses = {}, 0, 0, None, None
    for ln in extra:
        m = re.match(r'CT-LOG (\S+) \| (.*)', ln)
        if not m or m.group(1) != name: continue
        body = m.group(2)
        if h := re.match(r'(\d+)\s*[*:]>(.*)', body):
            s = lc.norm(h.group(2))
            hist[s] = hist.get(s, 0) + int(h.group(1))
            lines += 1; total += int(h.group(1))
        elif h := re.match(r'Histogram \((\d+) states?\)', body):
            declared = int(h.group(1))
        elif h := re.match(r'Positive: (\d+), Negative: (\d+)', body):
            witnesses = int(h.group(1)) + int(h.group(2))
    if not hist: return 'failed', 'no histogram in the output' + _output(extra)
    whole = []
    if declared is None: whole.append('no "Histogram (N states)" line')
    elif lines != declared: whole.append(f'{lines} state lines, the histogram declares {declared}')
    if witnesses is None: whole.append('no "Positive: P, Negative: Q" line')
    elif total != witnesses: whole.append(f'the state counts sum to {total}, Positive + Negative = {witnesses}')
    if whole: return 'failed', 'incomplete histogram: ' + '; '.join(whole) + _output(extra)
    if image is None: return 'failed', 'no image folder: the herd7 oracle is unknown'
    allowed, cond, kind = _oracle(str(pathlib.Path(image) / 'oracle'))
    if name not in allowed: return 'failed', f'no herd7 oracle for {name} in the image (oracle/{name.split(".")[0]}.txt)'
    forbidden = [(s, c) for s, c in sorted(hist.items()) if s not in allowed[name]]
    if forbidden:
        return 'failed', 'FORBIDDEN under x86-TSO: ' + ' | '.join(f'{_state(s)} x{c}' for s, c in forbidden)
    unseen = allowed[name] - set(hist)
    note = f'{len(unseen)} of {len(allowed[name])} allowed states never observed'
    if unseen: note += ': ' + ' | '.join(_state(s) for s in sorted(unseen))
    if kind.get(name) == 'Allowed' and cond.get(name) == 'Ok' and f.get('obs') == 'Never':
        note += '; the exists-condition is allowed under x86-TSO but was never observed'
    return 'passed', note


PUB_PROGRAMS = {'rseq', 'futex', 'membarrier', 'mm', 'ptrace', 'qemu'}     # kselftest and QEMU tests/tcg programs
PUB_SUCCESS = {   # the row family's own success signal, beyond rc 0 and no failing TAP line
    'ltp': (re.compile(r'^failed\s+0\s*$', re.M), re.compile(r'^broken\s+0\s*$', re.M)),     # tst_test's summary
    'kunit': (re.compile(r'^(?:\[[^\]]*\] )?ok \d+ \S+\s*$', re.M),),     # a top-level (unindented) suite result
    'locktorture': (re.compile(r'End of test: SUCCESS'),),
    'rcutorture': (re.compile(r'End of test: SUCCESS'),),
}
PUB_MODULE_SUCCESS = {'kernel-atomic64_test': re.compile(r'atomic64_test: passed'),
                      'kernel-test_bitmap': re.compile(r'test_bitmap: all \d+ tests passed')}
PUB_UNRUN = {'incomplete', 'identity-error', 'host-cap', 'guest-cap', 'killed-or-cap'}


LKMM_STATE = re.compile(r'(\d+)\s*([*:])>(.*)$')      # klitmus7's histogram line: COUNT, * (condition holds) or :, STATE


def _lkmm_norm(s: str) -> tuple:
    """A final state as klitmus7's histogram and herd7's States print it (`0:r0=1; 1:r1=0; x=2;`): its (location,
    value) pairs, sorted; a value is a number, or a location's name (a pointer: `1:r0=x`)."""
    kv = []
    for x in s.split(';'):
        x = x.strip()
        if not x: continue
        k, v = (w.strip() for w in x.split('=', 1))
        try: v = int(v, 0)
        except ValueError: pass
        kv.append((k.strip('[]'), v))
    return tuple(sorted(kv, key=lambda p: p[0]))


@functools.cache
def _lkmm_oracle(d: str):
    """The image's LKMM oracle (recipes/public/build.sh): oracle/lkmm.txt, herd7's output, gives NAME -> (allowed final
    states, Ok | No: whether the exists-condition is reachable); oracle/lkmm-tests.txt, `MODULE THREADS SIZE RUNS NAME`
    lines, gives NAME -> (threads, size, runs)."""
    lines, states = (pathlib.Path(d) / 'lkmm.txt').read_text().splitlines(), {}
    for i, ln in enumerate(lines):
        if m := re.match(r'Test (\S+) \S+$', ln):
            n = int(re.fullmatch(r'States (\d+)', lines[i + 1]).group(1))
            states[m.group(1)] = ({_lkmm_norm(s) for s in lines[i + 2:i + 2 + n]}, lines[i + 2 + n].strip())
    meta = {w[4]: tuple(map(int, w[1:4])) for w in map(str.split, (pathlib.Path(d) / 'lkmm-tests.txt').read_text().splitlines())}
    return states, meta


def _lkmm(name, text, image):
    """One LKMM row (`lkmm-NAME`): klitmus7's /proc/litmus output for NAME, judged by herd7's LKMM states. It passes
    when the run is whole and every observed final state is one herd7 allows: one `Test NAME` block whose histogram
    lines number its `Histogram (N states)`, whose counts sum to Positive + Negative, and that sum is the samples the
    module takes, instances x size x runs (an instance per `threads` online CPUs, from CT-BEGIN's cpus=). A state herd7
    does not allow fails (FORBIDDEN), whatever the count; allowed states never observed are only noted. `NAME:
    skipped` (no instance fits) is unsupported when the test has more threads than the guest has CPUs, else failed."""
    test = name.removeprefix('lkmm-')
    if image is None: return 'failed', 'no image folder: the herd7 oracle is unknown'
    states, meta = _lkmm_oracle(str(pathlib.Path(image) / 'oracle'))
    if test not in states or test not in meta: return 'failed', f'no herd7 oracle for {test} in the image (oracle/lkmm.txt)'
    (allowed, cond), (threads, size, runs) = states[test], meta[test]
    m = re.search(r'^CT-BEGIN suite=public part=\S+ cpus=(\d+) ', text, re.M)
    if not m: return 'failed', 'no CT-BEGIN cpus= line: the online CPUs are unknown'
    cpus = int(m.group(1))
    lines = [ln.rstrip('\r') for ln in text.splitlines()]
    if f'{test}: skipped' in lines:
        if threads > cpus: return 'unsupported', f'{threads} threads, the guest has {cpus} CPUs: klitmus7 runs no instance (skipped)'
        return 'failed', f'klitmus7 skipped the test, yet its {threads} threads fit {cpus} CPUs'
    if threads > cpus: return 'failed', f'{threads} threads, the guest has {cpus} CPUs, yet no `{test}: skipped` line'
    starts = [i for i, ln in enumerate(lines) if re.fullmatch(rf'Test {re.escape(test)} \S+', ln)]
    if len(starts) != 1: return 'failed', f'{len(starts)} "Test {test}" blocks in the output, not 1'
    rest = iter(lines[starts[0] + 1:])
    h = re.fullmatch(r'Histogram \((\d+) states?\)', next(rest, ''))
    if not h: return 'failed', 'no "Histogram (N states)" line after the Test line'
    hist, nlines, garbled, ln = {}, 0, [], ''
    for ln in rest:
        if s := LKMM_STATE.fullmatch(ln):
            try: st = _lkmm_norm(s.group(3))
            except ValueError: garbled.append(ln); continue
            hist[st] = hist.get(st, 0) + int(s.group(1)); nlines += 1
        elif re.match(r'\[\s*\d+\.\d+\]', ln) or not ln.strip(): continue      # the kernel's log, between lines
        elif ln in ('Ok', 'No'): break
        else: garbled.append(ln)
    w = re.search(r'^Positive: (\d+), Negative: (\d+)$', '\n'.join(lines[starts[0]:]), re.M)
    total, want = sum(hist.values()), (cpus // threads) * size * runs
    whole = []
    if garbled: whole.append(f'{len(garbled)} lines in the histogram are not states ({garbled[0]!r})')
    if nlines != int(h.group(1)): whole.append(f'{nlines} state lines, the histogram declares {h.group(1)}')
    if w is None: whole.append('no "Positive: P, Negative: Q" line')
    elif total != int(w.group(1)) + int(w.group(2)): whole.append(f'the state counts sum to {total}, Positive + Negative = '
                                                                  f'{int(w.group(1)) + int(w.group(2))}')
    if total != want: whole.append(f'{total} samples, the module takes {want} ({cpus // threads} instances x {size} x {runs})')
    if whole: return 'failed', 'incomplete klitmus7 output: ' + '; '.join(whole)
    forbidden = [(s, c) for s, c in sorted(hist.items(), key=str) if s not in allowed]
    if forbidden:
        return 'failed', 'FORBIDDEN under the LKMM (herd7): ' + ' | '.join(f'{_state(s)} x{c}' for s, c in forbidden)
    unseen = allowed - set(hist)
    shown = re.search(rf'^Observation {re.escape(test)} (\S+) ', text, re.M)
    note = (f'{total} samples ({cpus // threads} x {size} x {runs}), {len(hist)} states, all allowed by herd7\'s LKMM; '
            f'{len(unseen)} of {len(allowed)} allowed states never observed; the condition: herd7 {cond}, observed '
            f'{shown.group(1) if shown else "?"}')
    if unseen: note += ' (unseen: ' + ' | '.join(_state(s) for s in sorted(unseen, key=str)) + ')'
    return 'passed', note


def public(name, f, extra, image=None):
    """run-public.sh, one test per boot: `PUB-START id=ID cap=S` ... `PUB-RESULT id=ID rc=N`; `extra` is the boot's
    whole serial output, judged by public-campaign.py's classify, then by the row's own success signal:
      - a kernel splat anywhere in the boot (public-campaign.py's SPLAT: Oops, BUG, WARNING, panic, RCU stall,
        `End of test: FAILURE`, Call Trace) fails, whatever the exit status;
      - skip is unsupported; failure, broken, warning, identity-error, the caps and incomplete fail;
      - a row that ran to the end passes on its program's own success signal: rc 0 and no failing TAP line for the
        selftests and QEMU's tests (PUB_PROGRAMS); LTP's `failed 0` and `broken 0`; a top-level KUnit `ok N SUITE`;
        `atomic64_test: passed`; `test_bitmap: all N tests passed`; torture's `End of test: SUCCESS`. Without it the
        row is `unverified`, never passed;
      - vmalloc-*: test_vmalloc returns -EAGAIN after its results by design, so modprobe's status says nothing; it
        passes with at least one `Summary: ...` line and every one `failed: 0`, and fails otherwise;
      - lkmm-*: a row that ran to the end is judged by herd7's LKMM states in the image folder (_lkmm)."""
    text = '\n'.join(extra) + '\n'
    rc = f.get('rc')
    m = re.search(r'^CT-BEGIN suite=public part=(\d+)/1 ', text, re.M)
    family = name.split('-')[0]
    pc = _tool('public-campaign')
    c = pc.classify(text, dict(id=name, index=int(m.group(1)) if m else -1, family=family))
    splat = [ln for ln in text.splitlines() if pc.SPLAT.search(ln)]
    if splat: return 'failed', f'{c} (rc={rc}); the kernel log:\n' + '\n'.join(splat[:SHOWN])
    if family == 'vmalloc' and c not in PUB_UNRUN:
        sums = re.findall(r'Summary: (\S+) .*?failed: (\d+)', text)
        bad = [t for t, n in sums if n != '0']
        if sums and not bad: return 'passed', f'{len(sums)} test_vmalloc summaries, none failed (rc={rc}: -EAGAIN by design)'
        return 'failed', (f'test_vmalloc failed: {", ".join(bad)}' if bad else 'no test_vmalloc Summary: line') + f' (rc={rc})'
    if c == 'skip': return 'unsupported', f'skip (rc={rc})'
    if c != 'completed-needs-oracle-review': return 'failed', f'{c} (rc={rc})'
    if family == 'lkmm': return _lkmm(name, text, image)
    if family in PUB_PROGRAMS: return 'passed', 'rc 0, no failing TAP line'
    signals = PUB_SUCCESS.get(family) or ((PUB_MODULE_SUCCESS[name],) if name in PUB_MODULE_SUCCESS else ())
    if signals and all(s.search(text) for s in signals):
        return 'passed', 'rc 0 and the success line: ' + ' / '.join(s.search(text).group(0).strip() for s in signals)
    return 'unverified', f'{c} (rc={rc})' + ('; its success line is missing' if signals else '')
