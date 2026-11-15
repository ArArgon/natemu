"""The gate cross-check (cpu-features plan sec. 2.3): the catalogue (tools/models/cpu_profile.json), fexc's shim and the host
decode agree on which features are gated where.

- a `GF_<NAME>` used by a consumer must be a catalogue feature whose `gates` lists that consumer;
- a feature whose `gates` lists a consumer must have its `GF_<NAME>` gate in that consumer's sources.

Consumers: `shim` = tools/fexc/shim/*.inc|*.h (not the generated fexc_features.h) and tools/fexc/fexc.cpp; `system` = the
host decode, crates/host/src/system.rs (its production code, before the first `#[cfg(test)]`); `resident` = crates/runtime/src
and the resident's logic in crates/abi/src (not the generated tables).
"""
import copy
import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from models import cpu_profile as cp
from models import gen_cpu_profile_rs as gen

ROOT = cp.ROOT
GATE = re.compile(r'\bGF_([A-Z0-9_]+)\b')
GENERATED = {gen.OUT, gen.OUT_GATES, gen.OUT_HEADER}


def production(path):
    text = path.read_text()
    return text.split('#[cfg(test)]')[0] if path.suffix == '.rs' else text


def sources():
    shim = sorted(p for pat in ('*.inc', '*.h') for p in (ROOT / 'tools/fexc/shim').glob(pat)) + [ROOT / 'tools/fexc/fexc.cpp']
    resident = sorted(list((ROOT / 'crates/runtime/src').rglob('*.rs')) + list((ROOT / 'crates/abi/src').glob('*.rs')))
    files = {'shim': shim, 'system': [ROOT / 'crates/host/src/system.rs'], 'resident': resident}
    return {c: '\n'.join(production(p) for p in ps if p not in GENERATED) for c, ps in files.items()}


def gate_problems(raw, texts):
    feats = cp.Profile(raw).features()
    used = {c: set(GATE.findall(t)) for c, t in texts.items()}
    out = []
    for c, names in used.items():
        for n in sorted(names):
            if n not in feats: out.append(f'{c}: GF_{n} is not a catalogue feature')
            elif c not in feats[n][0].get('gates', []): out.append(f'{c}: GF_{n} is used, but {n}\'s gates do not list {c}')
    for n, fl in feats.items():
        for c in fl[0].get('gates', []):
            if n not in used.get(c, ()): out.append(f'{n}: gates lists {c}, which has no GF_{n} gate')
    return out


RAW = json.loads(cp.PATH.read_text())
TEXTS = sources()


class Gates(unittest.TestCase):
    @staticmethod
    def entry(raw, name):
        for e in raw['cpuid']:
            for fs in e.get('fields', {}).values():
                for f in fs.values():
                    if f['name'] == name: return f

    def test_the_repository_agrees(self):
        self.assertEqual(gate_problems(RAW, TEXTS), [])
        for c in ('shim', 'system'): self.assertGreater(len(set(GATE.findall(TEXTS[c]))), 15, c)
        self.assertEqual(set(GATE.findall(TEXTS['resident'])), set())

    def test_a_gate_the_shim_lacks_fails(self):
        # The negative control: a scratch optional entry (WFxT above the floor: not core; not built) lists the shim, whose
        # rows have no gate for it.
        raw = copy.deepcopy(RAW)
        self.entry(raw, 'AMX_TILE')['gates'] = ['shim']
        self.assertEqual(gate_problems(raw, TEXTS), ['AMX_TILE: gates lists shim, which has no GF_AMX_TILE gate'])
        raw = copy.deepcopy(RAW)
        f = self.entry(raw, 'WAITPKG')
        self.assertEqual(f['gates'], ['shim'])
        f['built'] = True                                         # optional: built, host.required wfxt (outside the floor)
        texts = dict(TEXTS, shim=TEXTS['shim'].replace('GF_WAITPKG', 'GF_WAITPKG_TYPO'))
        self.assertEqual(sorted(gate_problems(raw, texts)),
                         ['WAITPKG: gates lists shim, which has no GF_WAITPKG gate', 'shim: GF_WAITPKG_TYPO is not a catalogue feature'])

    def test_a_gate_the_catalogue_lacks_fails(self):
        texts = dict(TEXTS, system=TEXTS['system'] + '\nlet x = GF_NOPE;')
        self.assertEqual(gate_problems(RAW, texts), ['system: GF_NOPE is not a catalogue feature'])

    def test_a_gate_without_a_gates_entry_fails(self):
        raw = copy.deepcopy(RAW)
        self.entry(raw, 'SHA')['gates'] = ['system']
        self.assertEqual(gate_problems(raw, TEXTS), ['shim: GF_SHA is used, but SHA\'s gates do not list shim'])

    def test_every_consumer_gate_is_listed(self):
        for c in ('shim', 'system'):
            for f in cp.load().gated(c): self.assertIn(c, f['gates'])

    def test_the_shim_gates_the_vex_features(self):
        # FEX's `x86_avx` follows the catalogue: on whenever a VEX feature is advertised (fexc.cpp), as a gate use.
        text = (ROOT / 'tools/fexc/fexc.cpp').read_text()
        self.assertIn('GF_AVX || GF_BMI1 || GF_BMI2 || GF_FMA || GF_F16C', text)
        vex = ('AVX', 'BMI1', 'BMI2', 'FMA', 'F16C')
        self.assertTrue(all('shim' in cp.load().features()[n][0]['gates'] for n in vex))


if __name__ == '__main__':
    unittest.main()
