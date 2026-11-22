"""The documented floating-point deviations of fast mode on a host without FEAT_AFP (M1, M2), and how the runners judge them
(accurate-FP plan, docs/restructure/afp-accurate-plan.md section 2, chunk 0).

CLASSES is the one machine-readable list; README.md's section "Floating point on Macs without AFP (fast mode)" is the human
record, with the same names (tests/lib/test_fp_deviations.py keeps them in step).

The rule applies only when a run's own report says `afp` false and `fp` "fast" (`applies`): on an AFP host, in accurate mode
and for a report that does not say, every case is judged exactly. When it applies, a case tagged with a class is judged
against its documented deviated result and anything else fails, an exact result included (fast-mode code changed). Untagged
cases are judged exactly. `verdict` is that rule on one value.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import hashlib, json, pathlib, sys

DE = 2                    # MXCSR.DE
DM = 0x100                # MXCSR.DM, the DE mask (set: masked)

# name: (plan letter, one-line description for README and the startup note, the judging rule)
CLASSES = {
    'masked-sse-de': ('B', 'with DE masked, an SSE or AVX instruction on a denormal operand does not set MXCSR.DE (the result is right)',
                      'a runner case: the exact MXCSR with DE cleared, everything else exact'),
    'nan-results': ('A', "a NaN result can differ from x86 in sign or choice: the default NaN is positive, the one-instruction negated FMA forms "
                         "(the scalar ones, the packed FNMADD and the first multiplicand of FNMSUB) flip a NaN's sign, two-NaN and three-NaN choices follow Arm's order (the addend of the packed FMSUB and FNMSUB and the subtracted lanes of FMADDSUB and FMSUBADD arrive quieted, so an SNaN there takes no priority), RSQRT's NaN has the other sign",
                    "a corpus program: the final state the offline model predicts (tools/models/afp_fp)"),
    'fma-invalid-flag': ('D', 'an FMA of infinity times zero plus a QNaN, or plus an SNaN addend of the packed FMSUB, FNMSUB or the subtracted lanes of FMADDSUB and FMSUBADD, returns the default NaN and sets MXCSR.IE (x86: the NaN, quieted; no flag for a QNaN)',
                         'a corpus program: the model-predicted final state'),
    'band-ue-flag': ('C', 'with UE masked, a result just below the smallest normal number that rounds up to it can set MXCSR.UE (x86 judges tininess after rounding)',
                     'a case with MXCSR.UE set where x86 sets none (no tagged case yet)'),
    'daz-ftz-band': ('C', 'with DAZ and FTZ both set, a result just below the smallest normal number that rounds up to it is 0 instead of the smallest normal',
                     'a runner case: +-0 with UE and PE where x86 gives +-MIN_NORMAL with PE (run_i5c `daz-ftz-band-*`)'),
    'ftz-band': ('C', 'with FTZ set and DAZ clear, a result whose exact value lies just below the smallest normal number, which x86 flushes to 0 (tiny after rounding to the destination precision) but Arm\'s denormal rounding carries up to the smallest normal, is the smallest normal instead of 0 (the flags are right)',
                 'the smallest-normal result (no tagged case yet)'),
    'x87-nan-results': ('X', "an x87 NaN result can differ from x86 in sign or choice: an invalid operation (0/0, infinity minus infinity, infinity times 0, the square root of a negative number) gives the positive default NaN where x86 gives the negative one, and an operation whose first operand is a quiet NaN and whose second is a signalling NaN returns the signalling one quieted where x86 keeps the quiet one (the first operand is ST(0), or the memory operand of FSUBR and FDIVR; a signalling NaN reaches an operation as an m64 operand or in a register loaded by FLD m80, FRSTOR or FXRSTOR; a signalling m32 operand is quieted when it is converted and never shows it; the flags are right)",
                        "a runner case: the documented NaN in the register, everything else exact"),
    'masked-x87-de': ('X', 'with DE masked, an x87 instruction on a denormal m32 or m64 operand does not set the status word\'s DE flag (the result is right)',
                      'a runner case: the exact status word with DE cleared, everything else exact'),
}


# The x87 deviations that do not depend on the mode (docs/floating-point.md, "x87 deviations on every Mac" and the lists after it):
# registered here by name so a runner or a known-bugs entry can cite them; no runner case is judged by them today (the doubles
# items are the campaigns' expected failures; the two flag items lie outside what the x87 runners compare: UE and PE are not
# judged there, and no case computes on a register that holds a denormal double).
# name: (where it applies, description)
X87_OTHER = {
    'x87-doubles': ('every Mac, both modes', 'x87 registers hold 64-bit doubles, not the 80-bit format: `long double` has the precision and range of `double` (the list above)'),
    'x87-band-ue-flag': ('Macs without FEAT_AFP, both modes', 'with UE masked, an x87 result just below the smallest normal number of its format that rounds up to it sets the status word\'s UE flag: FST/FSTP m32 below the smallest normal float, arithmetic below the smallest normal double (Arm judges tininess before rounding)'),
    'x87-register-de': ('Macs with FEAT_AFP, both modes', 'an x87 instruction on a register that holds a denormal double sets the status word\'s DE flag; on x86 the register holds a normal 80-bit number and DE stays clear'),
}


# Which mode fixes each class on a host without AFP (plan section 3, "fixed by"): accurate mode, in the chunk named; fast mode never,
# (chunk 6's gate failed for UFE and IOE, so neither moved there: the classes stay registered for fast mode).
FIXED_BY = {'masked-sse-de': 'accurate (chunk 2)', 'nan-results': 'accurate (chunk 4: IOE trap, the handler patches NaN lanes; the FMA NaN check)',
            'fma-invalid-flag': 'accurate (chunk 4: the handler removes the IOC of infinity times zero beside a QNaN addend)',
            'band-ue-flag': 'accurate (chunk 3: UFE trap, handler)', 'daz-ftz-band': 'accurate (chunk 5: FZ = 0 with UFE under DAZ + FTZ, row 4\'s input flush, the handler gives x86\'s flushed result)',
            'ftz-band': 'accurate (chunk 3: the handler flushes the band lane under FTZ alone)',
            'x87-nan-results': 'accurate (the per-operation x87 wrapper and its NaN fix-up)',
            'masked-x87-de': 'accurate (the per-operation x87 wrapper and its software test of the memory operand)'}
assert FIXED_BY.keys() == CLASSES.keys()


# The corpus programs tagged with a NaN class, by corpus directory (names repeat between i8 and i8b, and the key ties the list to one
# corpus version), with the final state the offline model predicts for fast mode (tools/models/afp_fp/predict.py writes it).
PREDICTIONS = pathlib.Path(__file__).resolve().parents[2] / 'tools/models/afp_fp/predictions.json'


def corpus_deviations(key):
    """{program name: {'classes', 'gpr', 'rflags', 'rip', 'memory'}} of the corpus directory `key` (empty for any other corpus)."""
    return json.loads(PREDICTIONS.read_text()).get(key, {})


def prediction_diffs(pred, vm, expected):
    """Differences between a program's reported final state `vm` and the model's prediction `pred` (RFLAGS under QEMU's mask)."""
    num = lambda v: int(v, 0) if isinstance(v, str) else int(v)
    out = [f'{i}: predicted {pred["gpr"][i]} vs {vm["gpr"][i]}' for i in range(16) if num(pred['gpr'][i]) != num(vm['gpr'][i])]
    if (num(pred['rflags']) ^ num(vm['rflags'])) & num(expected['rflags_compare_mask']): out.append(f'rflags: predicted {pred["rflags"]} vs {vm["rflags"]}')
    if num(pred['rip']) != num(vm['rip']): out.append(f'rip: predicted {pred["rip"]} vs {vm["rip"]}')
    regions = [(num(m['addr']), m['sha256']) for m in pred['memory']]
    got = [(num(m['addr']), hashlib.sha256(bytes.fromhex(m['data'])).hexdigest()) for m in vm['memory']]
    if [r[0] for r in regions] != [g[0] for g in got]: out.append('memory regions differ')
    else: out += [f'memory at {a:#x} differs from the prediction' for (a, h), (_, g) in zip(regions, got) if h != g]
    return out


def applies(report):
    """The deviation rule applies to this run: its own report says no AFP and fast mode (a missing field: it does not)."""
    return report.get('afp') is False and report.get('fp') == 'fast'


def tag_mxcsr(simd, start, want):
    """The class of a runner case, derived and never written by hand: an SSE/AVX case whose expected MXCSR has DE set, DM masked,
    with DE clear in the starting MXCSR (a preset DE would let a bug that clears the sticky flag through). Cases with DE
    unmasked are exact in fast mode and never tagged. `want` may be a tuple of acceptable values: such a case is never tagged."""
    if not simd or not isinstance(want, int): return None
    return 'masked-sse-de' if want & DE and want & start & DM and not start & DE else None


def deviated_mxcsr(want):
    return want & ~DE


def verdict(tag, report, got, exact, deviated):
    """('ok' | 'deviated' | 'fail', problem or None) for a result `got` where `exact` is the SDM's and `deviated` the documented one."""
    if tag and applies(report):
        if got == deviated: return 'deviated', None
        if got == exact: return 'fail', f'tagged {tag} but exact: fast-mode code changed (documented deviation expected)'
        return 'fail', f'tagged {tag}: not the documented deviated result'
    return ('ok', None) if got == exact else ('fail', None)


def x87_verdict(tag, report, exact, deviated):
    """(kind, problems) of an x87 runner case with a class: `exact` and `deviated` are the problem lists of the case judged against
    x86's result and against the documented deviated one. The rule is `verdict`'s: where it applies only the deviated result passes."""
    if tag and applies(report):
        if not deviated: return 'deviated', []
        if not exact: return 'fail', [f'tagged {tag} but exact: fast-mode code changed (documented deviation expected)']
        return 'fail', [f'tagged {tag}: not the documented deviated result'] + list(deviated)
    return ('fail', list(exact)) if exact else ('ok', [])


def mxcsr_verdict(mx, report, got):
    """`mx` is a runner case's structured expected MXCSR {'start', 'want', 'tag'}; `got` the final MXCSR. (kind, problem)."""
    want = mx['want']
    if isinstance(want, tuple): return ('ok', None) if got in want else ('fail', f'mxcsr {got:#x}, want one of {[hex(w) for w in want]}')
    kind, why = verdict(mx['tag'], report, got, want, deviated_mxcsr(want))
    if kind == 'fail' and not why: why = f'mxcsr {got:#x}, want {want:#x}'
    elif kind == 'fail': why = f'mxcsr {got:#x}: {why} (want {deviated_mxcsr(want):#x} deviated, {want:#x} exact)'
    return kind, why


def make_mx(start, want, simd=True):
    return {'start': start, 'want': want, 'tag': tag_mxcsr(simd, start, want)}


class Counts:
    """A runner's tally: judged cases by outcome and the deviated ones by class."""
    def __init__(self): self.n = self.ok = self.bad = 0; self.dev = {}
    def add(self, kind, tag=None):
        self.n += 1
        if kind == 'fail': self.bad += 1
        elif kind == 'deviated': self.dev[tag] = self.dev.get(tag, 0) + 1
        else: self.ok += 1
    def line(self, unit='cases', extra=''):
        d = sum(self.dev.values())
        s = f'{self.n} {unit}, {self.ok} as expected, {self.bad} not{extra}'
        if d: s += f", {d} deviated as documented ({', '.join(f'{k}' if len(self.dev) == 1 else f'{k} {v}' for k, v in sorted(self.dev.items()))})"
        return s


def self_check(tagged, untagged, label):
    """Mutation checks of the rule on a real tagged case and a real untagged SIMD case (each `{'start', 'want', 'tag'}`): a tagged
    case that comes out exact fails, one with a different wrong result or an extra flag fails, one with the documented result is deviated, an
    untagged failure fails, the M4 (afp true) and accurate mode judge the tagged case strictly, and so does a report that does not
    say. Exits with a message on the first that does not hold."""
    m2 = {'afp': False, 'fp': 'fast'}
    def need(cond, what):
        if not cond: sys.exit(f'{label} self-check FAILED: {what}')
    t, u = tagged, untagged
    w, d = t['want'], deviated_mxcsr(t['want'])
    need(t['tag'] == 'masked-sse-de', f"tag {t['tag']}")
    need(mxcsr_verdict(t, m2, d)[0] == 'deviated', 'the documented deviated result is not accepted')
    need(mxcsr_verdict(t, m2, w)[0] == 'fail', 'a tagged case that comes out exact passes')
    need(mxcsr_verdict(t, m2, d ^ 1)[0] == 'fail', 'a different wrong result passes')
    need(mxcsr_verdict(t, m2, d | DE | 0x100)[0] == 'fail', 'an extra flag passes')
    for r in ({'afp': True, 'fp': 'fast'}, {'afp': False, 'fp': 'accurate'}, {}, {'afp': True, 'fp': 'accurate'}):
        need(mxcsr_verdict(t, r, w)[0] == 'ok' and mxcsr_verdict(t, r, d)[0] == 'fail', f'strict judging broken for {r}')
    need(u['tag'] is None, 'the untagged case is tagged')
    need(mxcsr_verdict(u, m2, u['want'])[0] == 'ok', 'an untagged exact case fails')
    need(mxcsr_verdict(u, m2, u['want'] ^ 1)[0] == 'fail', 'an untagged failure passes')
    need(mxcsr_verdict(u, m2, u['want'] ^ DE)[0] == 'fail', 'an untagged case accepts a changed DE')
    # A preset DE is not tagged: with DE set at the start, DE missing would hide a bug that clears the sticky flag.
    need(make_mx(DE | 0x1f80, DE | 0x1f80)['tag'] is None, 'a preset DE is tagged')
    need(make_mx(0x1e80, DE | 0x1e80)['tag'] is None, 'DE unmasked is tagged')
    need(make_mx(0x1f80, DE | 0x1f80, simd=False)['tag'] is None, 'a non-SIMD case is tagged')
    print(f'{label} self-check: ok')


def write_results(path, rows):
    """The framework's per-case file: {"results": [{name, outcome, diffs}]} from a runner's rows (name or case; ok; problems; outcome
    ok|deviated|fail; tag). A documented deviation is `excluded` with its class name, never asserted as an expected value;
    a row with mid_ok False (the STMXCSR in the middle of a sequence) fails."""
    out = []
    for r in rows:
        name = r.get('name') or r['case']
        kind = r.get('outcome') or ('ok' if r.get('ok', True) else 'fail')
        diffs = [str(p) for p in (r.get('problems') or [])]
        if r.get('mid_ok') is False: kind, diffs = 'fail', diffs + ['MXCSR in the middle of the sequence differs']
        if kind == 'deviated': out.append({'name': name, 'outcome': 'excluded', 'diffs': [f"deviated as documented: {r.get('tag')}"]})
        else: out.append({'name': name, 'outcome': 'fail' if kind == 'fail' else 'match', 'diffs': diffs})
    pathlib.Path(path).write_text(json.dumps({'results': out}, indent=1) + '\n')
