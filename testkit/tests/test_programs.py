import copy
import json
import types

from testkit import paths
from testkit.harness import programs

FIX = paths.repo_root() / 'fixtures' / 'slice1'


def load():
    ps = json.loads((FIX / 'programs.json').read_text())['programs']
    ex = {r['name']: r for r in json.loads((FIX / 'expected.json').read_text())['results']}
    return [(p, ex[p['name']]) for p in ps if p['name'] in ex]


def as_vm(result):
    q = result['qemu']
    ref, _ = programs._ref()
    return {'gpr': [ref.num(x) for x in q['gpr']], 'rflags': ref.num(q['rflags']), 'rip': ref.num(q['rip']), 'memory': q['memory']}


def test_normalize_matches_compare():
    ref, _ = programs._ref()
    pairs = load()
    assert len(pairs) > 40
    for p, res in pairs:
        assert programs.mask_of(p) == ref.num(res['rflags_compare_mask']), p['name']
        vm = as_vm(res)
        assert ref.compare(res, vm['gpr'], vm['rflags'], vm['rip'], vm['memory']) == []
        assert programs.answer(p, res['qemu']) == programs.answer(p, vm)
        undefined = ~programs.mask_of(p) & 0x8d5           # CF PF AF ZF SF OF: flip one the program leaves undefined
        if undefined:
            bit = undefined & -undefined
            v2 = dict(vm, rflags=vm['rflags'] ^ bit)
            assert ref.compare(res, v2['gpr'], v2['rflags'], v2['rip'], v2['memory']) == []
            assert programs.answer(p, v2) == programs.answer(p, vm)
        v3 = copy.deepcopy(vm); v3['gpr'][3] ^= 1
        assert ref.compare(res, v3['gpr'], v3['rflags'], v3['rip'], v3['memory']) != []
        assert programs.answer(p, v3) != programs.answer(p, vm)


def test_corpus_hash_ignores_header():
    ps = json.loads((FIX / 'programs.json').read_text())['programs']
    assert programs.corpus_hash(ps) == programs.corpus_hash(copy.deepcopy(ps))
    ps2 = copy.deepcopy(ps); ps2[0]['code'] = ps2[0]['code'][:-2] + '90'
    assert programs.corpus_hash(ps2) != programs.corpus_hash(ps)


def test_rules_exclude_tagged_fast_no_afp():
    import sys
    from testkit import family
    sys.path.insert(0, str(paths.repo_root() / 'tools'))
    import fp_deviations
    fam = family.load(paths.repo_root() / 'tests' / 'fp' / 'family.toml')
    vs = {v.id: v for v in family.expand(fam.suite('i8'))}
    tags = fp_deviations.corpus_deviations('i8-corpus-20261030-01')
    tagged = sorted(tags)[0]
    fast_off = next(i for i, v in vs.items() if 'afp=False' in i and 'fp=fast' in i)
    acc_off = next(i for i, v in vs.items() if 'afp=False' in i and 'fp=accurate' in i)
    fast_on = next(i for i, v in vs.items() if 'afp=True' in i and 'fp=fast' in i)
    assert fam.rules('i8', tagged, vs[fast_off], {'afp': 'hidden'}) == '+'.join(tags[tagged]['classes'])
    assert fam.rules('i8', tagged, vs[acc_off], {'afp': 'real'}) is None
    assert fam.rules('i8', tagged, vs[fast_on], {'afp': 'real'}) is None
    untagged = next(n for n in ('i8-addr-00', 'i8-addr-01', 'i8-addr-02') if n not in tags)
    assert fam.rules('i8', untagged, vs[fast_off], {'afp': 'real'}) is None
