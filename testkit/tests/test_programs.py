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
    from testkit import family
    fam = family.load(paths.repo_root() / 'tests' / 'fp' / 'family.toml')
    s = fam.suite('i8')
    vs = {v.id: v for v in family.expand(s)}
    tagged = 'i8-forms-001'
    assert fam.rules('i8', tagged, vs['afp=False,fp=fast'], {'afp': 'hidden'}) == 'nan-results'
    assert fam.rules('i8', tagged, vs['afp=False,fp=accurate'], {'afp': 'real'}) is None
    assert fam.rules('i8', tagged, vs['afp=True,fp=fast'], {'afp': 'real'}) is None
    assert fam.rules('i8', 'i8-forms-000', vs['afp=False,fp=fast'], {'afp': 'real'}) is None
