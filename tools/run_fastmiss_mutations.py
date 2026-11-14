#!/usr/bin/env python3
"""Deliberate mutations of the page record and the fast miss path (page-currency plan section 8, fast-miss plan section 5): each is applied
to the resident's (or the abi's pure-function) sources, built into its own target directory and run against the K1 programs that must
turn red, then reverted (restored in a `finally`, checked clean at the end).

    tools/vmlock.py shared tools/with-env python3 tools/run_fastmiss_mutations.py --fexc FEXC --out DIR [--only NAME[,NAME]] [--list]
        [--host HOST]      (a release host built from the unmutated sources, default target/release/natemu-host; mutations change the resident only)

The result of each is `red` (the programs failed, as required), `GREEN` (they passed: the mutation survived, a defect of the tests),
`build failed` or `pattern not found` (the sources changed: update the table). The baseline (the same programs unmutated) must be green
first (`--only baseline`). Exit status 1 when any mutation is not red."""
import argparse, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RES = 'crates/runtime/src/execution.rs'
REC = 'crates/runtime/src/execution/page_record.rs'
ABI = 'crates/abi/src/page_study.rs'
SOURCES = (RES, REC, ABI)

# name: ([(file, old, new)], programs that must go red)
MUTATIONS = {
    # ---- the fast miss path
    'fm-record-ignores-frame': ([(REC, '    if differ {\n        Mapped::No', '    if false {\n        Mapped::No')], 'cr3-switch,remap-cr3,remap-invlpg,page-share,page-cross'),
    'fm-unknown-is-yes': ([(RES, 'Mapped::Unknown => {\n            unknown = true;\n            Mapped::Unknown\n        }', 'Mapped::Unknown => Mapped::Yes(0)')], 'page-share,page-cross,cross'),
    'fm-entry-not-full-entry': ([(RES, 'cache::code_of(target).full_entry(),\n            BLOCK_SPSR | frame.nzcv', 'cache::code_of(target).entry(),\n            BLOCK_SPSR | frame.nzcv')], 'miss-pic'),
    'fm-ignores-tf-debug': ([(RES, 'if s.debug != 0 || s.cpu.rflags & RFLAGS_TF != 0 || s.cpu.dr[5] & DR7_ENABLES != 0 {', 'if s.cpu.dr[5] & DR7_ENABLES != 0 {')], 'miss-step'),
    'fm-ignores-dr7': ([(RES, 'if s.debug != 0 || s.cpu.rflags & RFLAGS_TF != 0 || s.cpu.dr[5] & DR7_ENABLES != 0 {', 'if s.debug != 0 || s.cpu.rflags & RFLAGS_TF != 0 {')], 'miss-dr7'),
    'fm-skips-fill-l1': ([(RES, 'c.fast_taken += 1;\n    cache::fill_l1(rip, target);', 'c.fast_taken += 1;')], 'miss-counts'),
    'fm-resumes-without-alignment-check': ([(RES, 'BLOCK_SPSR | frame.nzcv,\n            alignment_check(&s.cpu),', 'BLOCK_SPSR | frame.nzcv,\n            false,')], 'miss-ac'),
    'fm-takes-system-target': ([(RES, 'if target.flags & cache::SYSTEM != 0 {\n        return decline_fast(fast_decline::SYSTEM);\n    }', '')], 'miss-system'),
    # ---- the page record (the plan's M1-M14; M9's and M10's programs need the CPL switch)
    'pr-m1-hit-ignores-frame': ([(RES, '.filter(|&(at, _)| at == gpa)?', '.filter(|&(at, _)| at == gpa || true)?')], 'page-share,page-cross,cr3-switch'),
    'pr-m2-epoch-ignored': ([(ABI, '(r.epoch == epoch || (r.frame', '(true || (r.frame')], 'page-share,remap-cr3,cr3-switch,page-cross'),
    'pr-m3-table-disabled': ([(ABI, '        if hits(r, k, epoch, global_epoch, pge) {\n            let (page, flags) = answer', '        if false && hits(r, k, epoch, global_epoch, pge) {\n            let (page, flags) = answer')], 'page-counts'),
    'pr-m4-invlpg-leaves-4k': ([(ABI, 'for large in [false, true] {\n        for user in [false, true] {', 'for large in [true] {\n        for user in [false, true] {')], 'page-share,remap-invlpg'),
    'pr-m5-invlpg-leaves-2m': ([(ABI, 'for large in [false, true] {\n        for user in [false, true] {', 'for large in [false] {\n        for user in [false, true] {')], 'invlpg-large,pf-large'),
    'pr-m6-crossing-on-one-page': ([(REC, 'let Some((page, f)) = probe(s, (b.rip & !4095).wrapping_add(4096 * k as u64)) else {\n                return Mapped::Unknown;\n            };', 'let Some((page, f)) = probe(s, (b.rip & !4095).wrapping_add(4096 * k as u64)) else {\n                continue;\n            };')], 'page-cross,cross'),
    'pr-m7-global-across-global-flush': ([(ABI, 'r.epoch >= global_epoch', 'r.epoch >= 1')], 'page-global'),
    'pr-m8-pge-ignored': ([(ABI, '(r.frame & GLOBAL != 0 && pge && r.epoch', '(r.frame & GLOBAL != 0 && r.epoch')], 'page-global,place-global'),
    'pr-m10-invlpg-clears-one-class': ([(ABI, 'for user in [false, true] {\n            let k = key(va, large, user);\n            let r = &mut table[index(k)];\n            if r.epoch != 0', 'for user in [false] {\n            let k = key(va, large, user);\n            let r = &mut table[index(k)];\n            if r.epoch != 0')], 'page-user-invlpg'),
    'pr-m11-hit-skips-mark': ([(RES, '    let p = crate::perf::enter(crate::perf::FETCH);\n    match page_translation(s, va, natemu_abi::page_study::PLACE) {', '    let p = crate::perf::enter(crate::perf::FETCH);\n    let hits_before = cache::control().page_hits;\n    match page_translation(s, va, natemu_abi::page_study::PLACE) {'),
                               (RES, '            if block {\n                mark(s, gpa);', '            if block && cache::control().page_hits == hits_before {\n                mark(s, gpa);')], 'page-mark'),
    'pr-m12-check-compares-frames-only': ([(REC, 'if walked == Ok((page, flags)) {', 'if walked.map(|w| w.0) == Ok(page) {')], 'page-size-check'),
    'pr-m13-large-without-ram-rule': ([(REC, 'if large && !ps::recordable(&guest_ram(), ps::base(gpa, true), true) {', 'if false {')], 'page-large-edge'),
    'pr-m14-check-overwrites-record': ([(REC, '    cache::control().page_disagree += 1;', '    cache::control().page_disagree += 1;\n    if let Ok((g, f)) = walked {\n        record(s, va, g, f);\n    }')], 'page-check-keep'),
}


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def run_one(name, a):
    edits, programs = MUTATIONS[name] if name != 'baseline' else ([], a.baseline)
    saved = {}
    try:
        for path, old, new in edits:
            text = (ROOT / path).read_text()
            saved.setdefault(path, text)
            if old not in text:
                return 'pattern not found (the source changed: update this table): ' + path
            (ROOT / path).write_text(text.replace(old, new, 1))
        target = ROOT / 'target' / 'mut'
        r = sh(['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'],
               env=dict(os.environ, CARGO_TARGET_DIR=str(target)))
        if r.returncode != 0:
            return 'build failed: ' + r.stderr[-400:]
        out = a.out / name
        r = sh([sys.executable, 'tools/run_k1.py', '--fexc', str(a.fexc), '--out', str(out), '--cpus', '1', '--programs', programs,
                '--host', str(a.host), '--runtime', str(target / 'aarch64-unknown-none/release/natemu-runtime')])
        (a.out / f'{name}.log').write_text(r.stdout[-6000:] + r.stderr[-2000:])
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
    ap.add_argument('--baseline', default='miss-counts,miss-step,miss-dr7,miss-ac,miss-system,miss-pic,page-share,page-cross,page-global,page-user-invlpg,page-mark,page-size-check,page-counts,page-large-edge,page-check-keep,cr3-switch,remap-invlpg,remap-cr3,cross,invlpg-large,pf-large,place-global')
    a = ap.parse_args()
    if a.list:
        for n, (edits, programs) in MUTATIONS.items():
            print(f'{n}: {programs}')
        return 0
    if not (a.fexc and a.out):
        ap.error('--fexc and --out are required')
    a.out.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in a.only or list(MUTATIONS):
        results[name] = run_one(name, a)
        print(f'{name}: {results[name]}', flush=True)
    (a.out / 'mutations.json').write_text(json.dumps(results, indent=1))
    dirty = sh(['git', 'status', '--porcelain', '--', *SOURCES]).stdout
    if dirty.strip():
        print('sources not restored:\n' + dirty)
        return 1
    return 0 if all(v in ('red', 'green') for v in results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
