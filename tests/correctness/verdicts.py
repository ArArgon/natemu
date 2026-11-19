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


def cpyt(name, f, extra):
    """run-cpy.sh: `CT-RESULT cpy MODULE cases=C pass=P fail=F error=E skip=S sec=T` per regrtest module, with a
    `CT-CASE ID STATUS` line per case not passed. Fails on any failed or erroring case (named in the message); a module
    whose every case skipped is unsupported."""
    bad = [ln[len('CT-CASE '):] for ln in extra if ln.startswith('CT-CASE ') and ln.rsplit(None, 1)[-1] in ('fail', 'error')]
    missing = [k for k in ('cases', 'fail', 'error') if not str(f.get(k, '')).isdigit()]
    if missing: return 'failed', 'incomplete result line (' + ', '.join(f'{k}={f.get(k, "missing")}' for k in missing) + ')'
    if f.get('fail') != '0' or f.get('error') != '0':
        return 'failed', _fields(f, 'cases', 'fail', 'error') + ('\n' + '\n'.join(bad[:SHOWN]) if bad else '')
    if f.get('cases') not in (None, '0') and f.get('skip') == f.get('cases'):
        return 'unsupported', f'every case skipped ({f.get("skip")})'
    return 'passed', _fields(f, 'cases', 'skip')


def cpyb(name, f, extra):
    """cpy-build.sh (run-cpyb.sh): `CT-RESULT cpyb STEP rc=RC sec=S` per build step. Every step must exit 0."""
    if f.get('rc') == '0': return 'passed', ''
    return 'failed', _fields(f, 'rc', 'sec')


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
    them fails (FORBIDDEN); allowed states never observed are only noted."""
    if f.get('ct_skip'): return 'unsupported', f'needs {f.get("threads", "more")} threads: more than the guest has'
    if f.get('rc') != '0': return 'failed', _fields(f, 'rc', 'obs') + _output(extra)
    lc = _tool('litmus-check')
    hist = {}
    for ln in extra:
        m = re.match(r'CT-LOG (\S+) \| (.*)', ln)
        if not m or m.group(1) != name: continue
        h = re.match(r'(\d+)\s*[*:]>(.*)', m.group(2))
        if h:
            s = lc.norm(h.group(2))
            hist[s] = hist.get(s, 0) + int(h.group(1))
    if not hist: return 'failed', 'no histogram in the output' + _output(extra)
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


def public(name, f, extra):
    """run-public.sh, one test per boot: `PUB-START id=ID cap=S` ... `PUB-RESULT id=ID rc=N`; `extra` is the boot's
    whole serial output, judged by public-campaign.py's classify. Its "completed-needs-oracle-review" (ran to the end,
    nothing checked its output) is unverified, never passed; skip is unsupported; everything else (failure, broken,
    warning, identity-error, the caps, incomplete) fails."""
    text = '\n'.join(extra) + '\n'
    m = re.search(r'^CT-BEGIN suite=public part=(\d+)/1 ', text, re.M)
    case = dict(id=name, index=int(m.group(1)) if m else -1, family='ltp' if name.startswith('ltp-') else name.split('-')[0])
    c = _tool('public-campaign').classify(text, case)
    if c == 'completed-needs-oracle-review': return 'unverified', f'{c} (rc={f.get("rc")})'
    if c == 'skip': return 'unsupported', f'skip (rc={f.get("rc")})'
    return 'failed', f'{c} (rc={f.get("rc")})'
