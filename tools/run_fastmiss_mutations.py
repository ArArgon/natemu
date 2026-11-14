#!/usr/bin/env python3
"""Deliberate mutations of the page record and the fast miss path (page-currency plan section 8, fast-miss plan section 5) and of the
outbound link slots (`do-*`, the drain fix): each is applied
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
CACHE = 'crates/runtime/src/cache.rs'
SOURCES = (RES, REC, ABI, CACHE)

# name: ([(file, old, new)], programs that must go red)
MUTATIONS = {
    # ---- the fast miss path
    'fm-record-ignores-frame': ([(REC, '    if differ {\n        Mapped::No', '    if false {\n        Mapped::No')], 'cr3-switch,remap-cr3,remap-invlpg,page-share,page-cross'),
    'fm-unknown-is-yes': ([(RES, 'Mapped::Unknown => {\n            unknown = true;\n            Mapped::Unknown\n        }', 'Mapped::Unknown => {\n            unknown = true;\n            Mapped::Yes(0)\n        }')], 'page-share,page-cross,cross'),
    'fm-entry-not-full-entry': ([(RES, 'cache::code_of(target).full_entry(),\n            BLOCK_SPSR | frame.nzcv', 'cache::code_of(target).entry(),\n            BLOCK_SPSR | frame.nzcv')], 'miss-pic'),
    'fm-ignores-tf-debug': ([(RES, 'if s.debug != 0 || s.cpu.rflags & RFLAGS_TF != 0 || s.cpu.dr[5] & DR7_ENABLES != 0 {', 'if s.cpu.dr[5] & DR7_ENABLES != 0 {')], 'miss-step'),
    'fm-ignores-dr7': ([(RES, 'if s.debug != 0 || s.cpu.rflags & RFLAGS_TF != 0 || s.cpu.dr[5] & DR7_ENABLES != 0 {', 'if s.debug != 0 || s.cpu.rflags & RFLAGS_TF != 0 {')], 'miss-dr7'),
    'fm-skips-fill-l1': ([(RES, 'c.fast_taken += 1;\n    cache::fill_l1(rip, target);', 'c.fast_taken += 1;')], 'miss-counts'),
    'fm-resumes-without-alignment-check': ([(RES, 'BLOCK_SPSR | frame.nzcv,\n            alignment_check(&s.cpu),', 'BLOCK_SPSR | frame.nzcv,\n            0,')], 'miss-ac'),
    'fm-takes-system-target': ([(RES, 'if target.flags & cache::SYSTEM != 0 {\n        return decline_fast(fast_decline::SYSTEM);\n    }', '')], 'miss-system'),
    # ---- checks whose programs are other runners' (`slice3:MODE` = tools/run_slice3_vm.py MODE; `m2:ONLY` = tools/run_m2.py --only ONLY)
    'fm-ignores-boundary-state': ([(RES, 'if s.cpu.mov_ss | s.sti_shadow | s.mid_string | s.plain_step | s.stop_wanted != 0 {', 'if s.plain_step | s.stop_wanted != 0 {')], 'miss-boundary'),
    'fm-ignores-io-pending': ([(RES, 'if io_pending(s) {\n        return decline_fast(fast_decline::IO);', 'if false {\n        return decline_fast(fast_decline::IO);')], 'hold-io'),
    'fm-ignores-code-pending': ([(RES, 'if cache::pending() {\n        return decline_fast(fast_decline::PENDING);', 'if false {\n        return decline_fast(fast_decline::PENDING);')], 'hold-pending'),
    'fm-ignores-lapic-due': ([(RES, 'if lapic::due() {\n        return decline_fast(fast_decline::LAPIC);', 'if false {\n        return decline_fast(fast_decline::LAPIC);')], 'hold-lapic'),
    'fm-ignores-events': ([(RES, 'if !events::none(s) {\n        return decline_fast(fast_decline::EVENT);', 'if false {\n        return decline_fast(fast_decline::EVENT);')], 'hold-event,miss-event'),
    'fm-reads-if-from-record': ([(RES, '    if s.cpu.cpl == 0 {\n        // SAFETY: the frame page is mapped EL1 read/write at FRAME_VA.', '    if false {\n        // SAFETY: the frame page is mapped EL1 read/write at FRAME_VA.')], 'hold-event'),
    'fm-ignores-uncached': ([(RES, 'if c.uncached_count | c.uncached_fence != 0 {', 'if false {')], 'miss-uc'),
    # ---- the page record (the plan's M1-M14; M9 is the key without the privilege class: see the table of what is not covered)
    'pr-m1-hit-ignores-frame': ([(RES, '.filter(|&(at, _)| at == gpa)?', '.filter(|&(at, _)| at == gpa || true)?')], 'page-share,page-cross,cr3-switch'),
    'pr-m2-epoch-ignored': ([(ABI, '(r.epoch == epoch || (r.frame', '(true || (r.frame')], 'page-share,remap-cr3,cr3-switch,page-cross'),
    'pr-m3-table-disabled': ([(ABI, '        if hits(r, k, epoch, global_epoch, pge) {\n            let (page, flags) = answer', '        if false && hits(r, k, epoch, global_epoch, pge) {\n            let (page, flags) = answer')], 'page-counts'),
    'pr-m4-invlpg-leaves-4k': ([(ABI, 'for large in [false, true] {\n        for user in [false, true] {', 'for large in [true] {\n        for user in [false, true] {')], 'page-share,remap-invlpg'),
    'pr-m5-invlpg-leaves-2m': ([(ABI, 'for large in [false, true] {\n        for user in [false, true] {', 'for large in [false] {\n        for user in [false, true] {')], 'invlpg-large,pf-large'),
    'pr-m6-crossing-on-one-page': ([(REC, 'let Some((page, f)) = probe(s, (b.rip & !4095).wrapping_add(4096 * k as u64)) else {\n                return Mapped::Unknown;\n            };', 'let Some((page, f)) = probe(s, (b.rip & !4095).wrapping_add(4096 * k as u64)) else {\n                continue;\n            };')], 'page-cross,cross'),
    'pr-m7-global-across-global-flush': ([(ABI, 'r.epoch >= global_epoch', 'r.epoch >= 1')], 'page-global'),
    'pr-m8-pge-ignored': ([(ABI, '(r.frame & GLOBAL != 0 && pge && r.epoch', '(r.frame & GLOBAL != 0 && r.epoch')], 'page-global,place-global'),
    'pr-m9-key-without-privilege-class': ([(ABI, '(large as u64) << 1 | user as u64', '(large as u64) << 1 | 0')], 'page-user'),
    'pr-m10-invlpg-clears-one-class': ([(ABI, 'for user in [false, true] {\n            let k = key(va, large, user);\n            let r = &mut table[index(k)];\n            if r.epoch != 0', 'for user in [false] {\n            let k = key(va, large, user);\n            let r = &mut table[index(k)];\n            if r.epoch != 0')], 'page-user-invlpg'),
    'pr-m11-hit-skips-mark': ([(RES, '    let p = crate::perf::enter(crate::perf::FETCH);\n    match page_translation(s, va, natemu_abi::page_study::PLACE) {', '    let p = crate::perf::enter(crate::perf::FETCH);\n    let hits_before = cache::control().page_hits;\n    match page_translation(s, va, natemu_abi::page_study::PLACE) {'),
                               (RES, '            if block {\n                mark(s, gpa);', '            if block && cache::control().page_hits == hits_before {\n                mark(s, gpa);')], 'page-mark'),   # survives: `kept` discards a block placed from an unmarked page and the step runs fresh bytes (outcome-equal, perf only)
    'pr-m12-check-compares-frames-only': ([(REC, 'if walked == Ok((page, flags)) {', 'if walked.map(|w| w.0) == Ok(page) {')], 'page-size-check'),
    'pr-m13-large-without-ram-rule': ([(REC, 'if large && !ps::recordable(&guest_ram(), ps::base(gpa, true), true) {', 'if false {')], 'page-large-edge'),
    'pr-m14-check-overwrites-record': ([(REC, '    cache::control().page_disagree += 1;', '    cache::control().page_disagree += 1;\n    if let Ok((g, f)) = walked {\n        record(s, va, g, f);\n    }')], 'page-check-keep'),
    # ---- the outbound slots (drain fix: a block's death undoes its links out by its slots, not by a pass over every record)
    'do-death-skips-outbound': ([(CACHE, '        if dead && block(n).out != [0; MAX_LINK_POINTS] {\n            undone += unlink_outbound(n)?;\n        }', '')], 'kill-out'),
    'do-death-passes-over-records': ([(CACHE, 'undone += unlink_outbound(n)?;', 'undone += prune(CHECKED, false)?;')], 'kill-out'),
    'do-move-leaves-slot': ([(CACHE, '    unsafe { *slot = to as u32 + 1 };\n', '')], 'kill-out,local-write,remap-link,cr3-link'),
    'do-remove-keeps-slot': ([(CACHE, '    list_del(i as usize);\n    out_del(i as usize);', '    list_del(i as usize);')], 'kill-out,local-write,remap-link,cr3-link'),
    'do-prune-tlb-keeps-slot': ([(CACHE, '                list_del(i as usize);\n                out_del(i as usize);', '                list_del(i as usize);')], 'cr3-link,remap-link,cr3-switch'),
    'do-unlink-all-keeps-slots': ([(CACHE, '        unsafe { *out_slot(&r) = 0 };\n', '')], 'link-full'),
    'do-copy-keeps-slots': ([(CACHE, '        inbound: 0,\n        out: [0; MAX_LINK_POINTS],\n        ..*b\n', '        inbound: 0,\n        ..*b\n')], 'reuse-alias,reuse-write,reuse-remap,reuse-cross'),
}


# Mutations that must stay green, with the argument: the removed terms are always 0 at a dispatcher miss (`enter` clears `mov_ss`,
# `sti_shadow` and `mid_string`, as `system`, `delivery` and the other resident exits do), so the check is a guard no state can reach.
EQUIVALENT = {'fm-ignores-boundary-state': '`mov_ss`/`sti_shadow`/`mid_string` are 0 at every dispatcher miss (enter, system, delivery clear them)'}


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def run_one(name, a):
    edits, programs = MUTATIONS[name] if name != 'baseline' else ([], a.baseline)
    saved = {}
    try:
        for path, old, new in edits:
            text = (ROOT / path).read_text()
            saved.setdefault(path, text)
            if text.count(old) != 1:
                return 'pattern not found or not unique (the source changed: update this table): ' + path
            (ROOT / path).write_text(text.replace(old, new, 1))
        target = ROOT / 'target' / 'mut'
        # (The `do-*` mutations run under the `link-check` resident: a dead block's record left behind has no effect a program sees,
        # and the end of every K1 program, a CR3 load, drops it; the check at the cut sees it.)
        r = sh(['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none',
                *(['--features', 'link-check'] if name.startswith('do-') else [])], env=dict(os.environ, CARGO_TARGET_DIR=str(target)))
        if r.returncode != 0:
            return 'build failed: ' + r.stderr[-400:]
        out = a.out / name
        if programs.startswith(('slice3:', 'm2:')):
            runner, _, arg = programs.partition(':')
            runtime = str(target / 'aarch64-unknown-none/release/natemu-runtime')
            cmd = ([sys.executable, 'tools/run_slice3_vm.py', '--fexc', str(a.fexc), '--out', str(out), arg] if runner == 'slice3' else
                   [sys.executable, 'tools/run_m2.py', '--fexc', str(a.fexc), '--out', str(out), '--only', arg, '--host', str(a.host), '--runtime', runtime])
            r = sh(cmd, env=dict(os.environ, CARGO_TARGET_DIR=str(target)))
            (a.out / f'{name}.log').write_text(r.stdout[-6000:] + r.stderr[-2000:])
            return 'red' if r.returncode != 0 else 'GREEN'
        hooks = []
        if any(x.startswith('hold-') for x in programs.split(',')):
            ht = ROOT / 'target' / 'mut-hooks'
            r = sh(['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none', '--features', 'io-test-hooks'],
                   env=dict(os.environ, CARGO_TARGET_DIR=str(ht)))
            if r.returncode != 0:
                return 'build failed: ' + r.stderr[-400:]
            hooks = ['--hooks-host', str(a.hooks_host), '--hooks-runtime', str(ht / 'aarch64-unknown-none/release/natemu-runtime')]
        r = sh([sys.executable, 'tools/run_k1.py', '--fexc', str(a.fexc), '--out', str(out), '--cpus', '1', '--programs', programs,
                '--host', str(a.host), '--runtime', str(target / 'aarch64-unknown-none/release/natemu-runtime'), *hooks])
        (a.out / f'{name}.log').write_text(r.stdout[-6000:] + r.stderr[-2000:])
        if name == 'baseline':
            return 'green' if r.returncode == 0 else 'RED (the baseline fails)'
        if name in EQUIVALENT:
            return 'equivalent (green as documented): ' + EQUIVALENT[name] if r.returncode == 0 else 'RED (an equivalent mutation went red: the argument is wrong)'
        return 'red' if r.returncode != 0 else 'GREEN'
    finally:
        for path, text in saved.items():
            (ROOT / path).write_text(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path)
    ap.add_argument('--out', type=pathlib.Path)
    ap.add_argument('--host', type=pathlib.Path, default=ROOT / 'target/release/natemu-host')
    ap.add_argument('--hooks-host', type=pathlib.Path, default=ROOT / 'target/hooks/release/natemu-host', help='an io-test-hooks host built from the unmutated sources (the hold cases)')
    ap.add_argument('--only', type=lambda s: s.split(','))
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--baseline', default='miss-counts,miss-uc,miss-boundary,miss-event,hold-io,hold-pending,hold-event,hold-lapic,miss-step,miss-dr7,miss-ac,miss-system,miss-pic,page-share,page-cross,page-global,page-user-invlpg,page-mark,page-size-check,page-counts,page-large-edge,page-check-keep,cr3-switch,remap-invlpg,remap-cr3,cross,invlpg-large,pf-large,place-global')
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
    return 0 if all(v in ('red', 'green') or v.startswith('equivalent') for v in results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
