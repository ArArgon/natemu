import pathlib
import sys

from testkit import family, paths
from testkit.harness.programs import build_input

sys.path.insert(0, str(paths.repo_root() / 'tools' / 'models' / 'afp_fp'))
import approx, fp  # noqa: E402


def test_sdm_rules():
    assert approx.rule('rcp', 0x00000000) == ('exact', 0x7f800000)
    assert approx.rule('rcp', 0x80000001) == ('exact', 0xff800000)          # a denormal counts as -0.0
    assert approx.rule('rsqrt', 0xbf800000) == ('exact', 0xffc00000)        # negative: the default NaN
    assert approx.rule('rsqrt', 0xff800000) == ('exact', 0xffc00000)
    assert approx.rule('rcp', 0x7fa00000) == ('exact', 0x7fe00000)          # SNaN quieted
    assert approx.rule('rcp', 0xff800000) == ('exact', 0x80000000)
    assert approx.rule('rcp', 0x7f7fffff) == ('exact', 0)                   # tiny result, flushed
    assert approx.rule('rcp', 0x7e800000) == ('approx', True)               # 2^126: either 0 or a result in the bound
    assert approx.rule('rcp', 0x7e000000) == ('approx', False)              # 2^125: never tiny
    assert approx.rule('rsqrt', 0x40800000) == ('approx', False)


def test_bound():
    one = 0x3f800000
    assert approx.valid('rcp', one, one) is None
    assert approx.valid('rcp', one, one + 6) is None and approx.valid('rcp', one, one - 12) is None   # within 1.5 * 2^-12
    assert approx.valid('rcp', one, one + 3 * 2 ** 11) is not None                                     # 1.5 * 2^-11
    assert approx.valid('rsqrt', 0x40800000, 0x3f000000) is None                                      # 1/sqrt(4)
    assert approx.valid('rsqrt', 0x40800000, 0x3f000000 + 4000) is not None
    assert approx.valid('rcp', 0x7e800000, 0) is None and approx.valid('rcp', 0x7e000000, 0) is not None
    assert 'infinity' in approx.valid('rcp', 0x00400000, 0x7f000000)


def _program(suite, name):
    fam = family.load(paths.repo_root() / 'tests' / 'fp' / 'family.toml')
    _, ps = build_input(fam, fam.suite(suite).input)
    return next(p for p in ps if p['name'] == name)


def test_judge_accepts_any_valid_approximation_and_reads_consumed_ones_from_a_prefix():
    p = _program('i8b', 'i8-random-003')            # four RCPPS lanes consumed later: read from a prefix run
    trunc = lambda i, k, x, r: approx.sdm_or({})(i, k, x, r) & ~0x7ff if approx.rule(k, x)[0] == 'approx' else \
        approx.sdm_or({})(i, k, x, r)              # an AMD-like table: the low 11 bits dropped, within the bound
    st, ev = approx.run(p, fp.Q, trunc)
    runs = []

    def run_prefix(q):
        runs.append(q['name'])
        return list(approx.run(q, fp.Q, trunc)[0][2])
    got, problems = approx.judge(p, fp.Q, list(st[2]), approx.prefix_reader(p, fp.Q, run_prefix))
    assert problems == [] and got == st and runs
    bad = lambda i, k, x, r: trunc(i, k, x, r) ^ (0x2000 if approx.rule(k, x)[0] == 'approx' else 0)    # 2^-10 off
    st2, _ = approx.run(p, fp.Q, bad)
    _, problems = approx.judge(p, fp.Q, list(st2[2]),
                               approx.prefix_reader(p, fp.Q, lambda q: list(approx.run(q, fp.Q, bad)[0][2])))
    assert any('over 1.5 * 2^-12' in x for x in problems)
