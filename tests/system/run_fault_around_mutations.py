#!/usr/bin/env python3
"""Deliberate mutations of the shadow fault-around fill (docs/restructure/fault-around-plan.md section 7's M1-M16 and review 9.1's
B1, B2): each is applied to the resident's or the abi's sources, built into its own target directory and run against the K1 fill
programs that must turn red (tests/system/run_k1.py `fa_fill_setup`), then reverted (restored in a `finally`, checked clean at the end).

    python3 tools/vmlock.py shared python3 tests/system/run_fault_around_mutations.py --fexc FEXC --out DIR [--only NAME[,NAME]] [--list]
        [--host HOST]      (a release host built from the unmutated sources, default target/release/natemu-host; mutations change the resident only)

The result of each is `red` (the programs failed, as required), `GREEN` (they passed: the mutation survived, a defect of the tests),
`build failed` or `pattern not found` (the sources changed: update the table). The baseline (the same programs unmutated) must be green
first (`--only baseline`). Exit status 1 when any mutation is not red.

Not here, with where they are caught: M10 (a valid slot replaced, nothing published) is `Space::fill`'s, caught by its abi unit test
`fill_writes_only_empty_slots_and_publishes_once` (which also refuses M3's kept-half non-global entries: `m3-kept-non-global` removes
both that refusal and the resident's check before it); M12 (`Translation`'s parents' bits from the leaf) by the abi unit test
`translation_hands_over_the_leaf_table_and_the_parents_bits`; M4 (a device page mapped as RAM) has no form here: the target IPA comes
from guest RAM's own range, which refuses any other page (`fa-fill-mmio` checks the device's answer); M16 is chunk 0's study build."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
FILL = 'crates/runtime/src/execution/fault_around.rs'
RULE = 'crates/abi/src/fault_around.rs'
TABLES = 'crates/runtime/src/tables.rs'
SHADOW = 'crates/abi/src/shadow.rs'
SOURCES = (FILL, RULE, TABLES, SHADOW)

# name: ([(file, old, new)], programs that must go red)
MUTATIONS = {
    'm1-ignores-a': ([(RULE, 'if entry & crate::paging::ACCESSED == 0 {', 'if false {')], 'fa-fill-ad'),
    'm2-writable-without-d': ([(RULE, 'writable: grant && entry & crate::paging::DIRTY != 0,', 'writable: grant,')], 'fa-fill-ad'),
    'm2-large-writable-without-d': ([(FILL, 'writable: t.writable && t.dirty }', 'writable: t.writable }')], 'fa-fill-large'),
    'm2-ignores-wt': ([(FILL, "if !matches!(kind, cpu::memory_type::WB | cpu::memory_type::WC) {", 'if false {'),
                       (FILL, 'cpu::writable_on_read(true, true, true, kind,', 'cpu::writable_on_read(true, true, true, cpu::memory_type::WB,')], 'fa-fill-wt'),
    'm2-fills-uc': ([(FILL, 'if kind == cpu::memory_type::UC {', 'if false {')], 'fa-fill-wt'),
    'm5-no-writable-bit': ([(FILL, '|| {\n                cache::shadowed_writable(page.gpa)\n            });', '|| {});')], 'fa-fill-smc'),
    'b1-no-reverse-map': ([(FILL, '            cache::writable_installed(user, va, gpa);\n', '')], 'fa-fill-smc'),
    'm6-skips-translated': ([(FILL, '|| cache::translated(page.gpa)', '|| false')], 'fa-fill-smc'),
    'm7-ignores-us': ([(RULE, 'if c.cpl == 3 && !user {', 'if false {')], 'fa-fill-user'),
    'm8-ignores-rw': ([(RULE, 'let rw = parent_rw && entry & 2 != 0;', 'let rw = true;')], 'fa-fill-rw'),
    'm9-disabled': ([(FILL, '    if w == 0 {\n        return;\n    }', '    if true {\n        return;\n    }')], 'fa-fill-counts'),
    'm9-off-ignored': ([(FILL, 'let w = cache::control().fault_around;', 'let w = 16;')], 'fa-fill-counts'),
    'm14-ignores-present': ([(RULE, 'if entry & 1 == 0 {\n        return Err(count::NOT_PRESENT);', 'if false {\n        return Err(count::NOT_PRESENT);')], 'fa-fill-none'),
    'm14-ignores-reserved': ([(RULE, 'if entry & 0x000f_ff00_0000_0000 != 0 || (c.nxe == 0 && entry >> 63 != 0) {', 'if false {')], 'fa-fill-none'),
    'm15-wrong-space': ([(FILL, 'match tables::fill(cpl, region, batch) {', 'match tables::fill(3, region, batch) {')], 'fa-fill-user'),
    'm15-both-spaces': ([(FILL, 'match tables::fill(cpl, region, batch) {', 'let _ = tables::fill(3, region, batch);\n    match tables::fill(cpl, region, batch) {')], 'fa-fill-user'),
    'b2-supervisor-writable': ([(FILL, '        if page.writable && !user && !fa::SUPERVISOR_WRITABLE {', '        if false {')], 'fa-fill-user'),   # (counter-only: the rule is not guest-visible)
    'm3-kept-non-global': ([(FILL, 'if !user && va >> 47 != 0 && !global {', 'if false {'), (SHADOW, '&& !(kept && !e.global)', '&& true')], 'fa-fill-kept'),
    'batch-skips-tlbi': ([(TABLES, '                unsafe { asm!("tlbi vae1, {0}", in(reg) page | self.asid) };\n', '')], 'fa-fill-invlpg'),
}


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def run_one(name, a):
    edits, programs = MUTATIONS[name] if name != 'baseline' else ([], 'fa-fill')
    saved = {}
    try:
        for path, old, new in edits:
            text = (ROOT / path).read_text()
            saved.setdefault(path, text)
            if text.count(old) != 1:
                return 'pattern not found or not unique (the source changed: update this table): ' + path
            (ROOT / path).write_text(text.replace(old, new, 1))
        target = ROOT / 'target' / 'mut-fa'
        r = sh(['cargo', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'],
               env=dict(os.environ, CARGO_TARGET_DIR=str(target)))
        if r.returncode != 0:
            return 'build failed: ' + r.stderr[-400:]
        out = a.out / name
        r = sh([sys.executable, 'tests/system/run_k1.py', '--fexc', str(a.fexc), '--out', str(out), '--cpus', '1', '--programs', programs,
                '--host', str(a.host), '--runtime', str(target / 'aarch64-unknown-none/release/natemu-runtime')])
        (a.out / f'{name}.log').write_text(r.stdout[-8000:] + r.stderr[-2000:])
        if name == 'baseline':
            return 'green' if r.returncode == 0 else 'RED (the baseline fails)'
        return 'red' if r.returncode != 0 else 'GREEN'
    finally:
        for path, text in saved.items():
            (ROOT / path).write_text(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path)
    ap.add_argument('--out', type=pathlib.Path)
    ap.add_argument('--host', type=pathlib.Path, default=ROOT / 'target/release/natemu-host')
    ap.add_argument('--only', type=lambda s: s.split(','))
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args()
    if a.list:
        for n, (edits, programs) in MUTATIONS.items():
            print(f'{n}: {programs}')
        return 0
    if not (a.fexc and a.out):
        ap.error('--fexc and --out are required')
    a.out.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in a.only or ['baseline', *MUTATIONS]:
        results[name] = run_one(name, a)
        print(f'{name}: {results[name]}', flush=True)
    (a.out / 'mutations.json').write_text(json.dumps(results, indent=1))
    dirty = sh(['git', 'status', '--porcelain', '--', *SOURCES]).stdout
    if dirty.strip():
        print('sources not restored:\n' + dirty)
        return 1
    return 0 if all(v in ('red', 'green') for v in results.values()) else 1


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
