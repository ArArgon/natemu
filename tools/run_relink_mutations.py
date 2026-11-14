#!/usr/bin/env python3
"""Deliberate mutations of relink-on-revalidation (docs/restructure/relink-plan.md section 5): each is applied to the resident's (or the
host check's) sources, built into its own target directory and run against the K1 programs that must turn red, then reverted (restored
in a `finally`, checked clean at the end).

    tools/vm_lock.py --shared tools/with-env python3 tools/run_relink_mutations.py --fexc FEXC --out DIR [--only NAME[,NAME]] [--list]
        [--host HOST]      (a release host built from the unmutated sources, default target/release/natemu-host; the host-check
                            mutation builds its own)

The result of each is `red` (the programs failed, as required), `GREEN` (they passed: the mutation survived, a defect of the tests),
`build failed` or `pattern not found` (the sources changed: update the table). The baseline (the same programs unmutated, in the same
build modes) must be green first (`--only baseline`). A mutation marked `lc` runs on a `link-check` resident. Exit status 1 when any
mutation is not red."""
import argparse, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = 'crates/runtime/src/cache.rs'
HOST = 'crates/host/src/execution.rs'
SOURCES = (CACHE, HOST)
NEW = 'cr3-relink,relink-smc,relink-dead-source,relink-stale,relink-held,relink-global,relink-many'

# name: ([(file, old, new)], programs that must go red, link-check resident?)
MUTATIONS = {
    # ---- the plan's table
    'no-unlinked-guard': ([(CACHE, 'p.offset != 0 && p.offset == r.offset && unlinked(source, p))', 'p.offset != 0 && p.offset == r.offset)')], 'relink-stale', False),
    'dead-source-relinked': ([(CACHE, '        if source.flags & DEAD != 0\n            || !source.links', '        if !source.links'),
                              (CACHE, 'let kept = if k == CHECKED {\n            alive\n        } else {', 'let kept = if k == CHECKED {\n            alive\n        } else if k == SUSP {\n            true\n        } else {')],
                             'relink-dead-source', False),
    'kill-keeps-suspended': ([(CACHE, '        if all || i < control().links_tlb {\n            // (A suspended record', '        if (all || i < control().links_tlb) && i >= control().links_susp {\n            // (A suspended record')],
                             'relink-smc', True),
    'invlpg-cut-keeps-suspended': ([(CACHE, '        if all || i < control().links_tlb {\n            // (A suspended record', '        if all || (i < control().links_tlb && i >= control().links_susp) {\n            // (A suspended record')],
                                    'remap-link,cross-link', True),
    'unlink-all-unlinks-suspended': ([(CACHE, '        if i as u64 >= c.links_susp {\n            unlink(r)?;', '        if true {\n            unlink(r)?;')], 'cr3-relink', False),
    'prune-unlinks-suspended': ([(CACHE, '        if k != SUSP {\n            unlink(r)?;\n            unlinked += 1;', '        if true {\n            unlink(r)?;\n            unlinked += 1;')], 'relink-dead-source', False),
    'inbound-unlinks-suspended': ([(CACHE, '            if i >= control().links_susp {\n                unlink(r)?;\n                undone += 1;', '            if true {\n                unlink(r)?;\n                undone += 1;')], 'relink-smc', False),
    'relinked-always-cr3': ([(CACHE, 'resume(i, r, global, &mut at);', 'resume(i, r, false, &mut at);')], 'relink-global', False),
    'relink-before-flags': ([(CACHE, '                // Relink plan: the links the last TLB event suspended into it are patched again (after the walk: it may\n', '                // MUTATION\n'),
                             (CACHE, '            if let Some(walked) = walked.filter(|_| block(n).flags & DEAD == 0) {\n                c.revalidated += 1;',
                              '            if let Some(walked) = walked.filter(|_| block(n).flags & DEAD == 0) {\n                if block(n).inbound != 0 && c.links_susp != 0 {\n                    relink(n);\n                }\n                c.revalidated += 1;'),
                             (CACHE, '                #[cfg(not(feature = "pic-count"))]\n                if block(n).inbound != 0 && c.links_susp != 0 {\n                    relink(n);\n                }\n', '')],
                            'relink-global', False),
    'suspend-leaves-links-patched': ([(CACHE, 'clean_pou(ctr, area::CODE_WRITE_VA + unlink_word(unsafe { record(i as usize).read() })?);',
                                       'clean_pou(ctr, area::CODE_WRITE_VA + u64::from(block(unsafe { record(i as usize).read() }.source as usize).code));')],
                                     'cr3-link,cr3-relink', False),
    'relink-disabled': ([(CACHE, 'if block(n).inbound != 0 && c.links_susp != 0 {\n                    relink(n);', 'if false && block(n).inbound != 0 && c.links_susp != 0 {\n                    relink(n);')], 'cr3-relink', False),
    'relink-skips-cursor-fix': ([(CACHE, '        if *follow == last as u32 + 1 {\n            *follow = i as u32 + 1;\n        }', ''),
                                 (CACHE, '            if *follow == end as u32 + 1 {\n                *follow = last as u32 + 1;\n            }', '')], 'relink-many', False),
    'relink-moves-with-insert': ([(CACHE, 'resume(i, r, global, &mut at);', 'remove(i, SUSP, &mut at);\n        insert(LinkRecord { prev_in: 0, next_in: 0, ..r }, if global { TLB } else { CR3 });')], 'relink-many', False),
    'host-rule-unlinked-only': ([(HOST, '} else if !admitted && !held {', '} else if !admitted {')], 'relink-held', False),
    # ---- the batch (step 1's synchronization, now `suspend`'s): a word left unrestored goes unnoticed until a link is taken
    'suspend-skips-last-word': ([(CACHE, '    for i in from..to {\n        clean_pou(ctr, area::CODE_WRITE_VA', '    for i in from..to.saturating_sub(1) {\n        clean_pou(ctr, area::CODE_WRITE_VA')], 'cr3-relink,relink-many', False),
    # ---- the host check's other rules
    'host-allows-current-suspended': ([(HOST, '} else if current {\n                why = Some("a current target");', '} else if false {\n                why = Some("a current target");')], 'relink-held', False),
}
# Mutations that cannot be caught by K1, with the argument (reported as `not testable`, never run).
NOT_TESTABLE = {
    'suspend-skips-barrier': 'the batch\'s `dsb`/`isb` and the cache maintenance order the new words before the next fetch; on this core (CTR_EL0 IDC and DIC set, or the clean nearly free) a missing barrier shows in no run',
    'prune-tlb-calls-unlink-on-suspended': 'prune_tlb is compiled only for `pic-count` builds, where nothing is suspended; the same mutation on `prune` and `unlink_all` is `prune-unlinks-suspended` and `unlink-all-unlinks-suspended`',
}


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def build_resident(target, features):
    cmd = ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-runtime', '--target', 'aarch64-unknown-none']
    if features:
        cmd += ['--features', features]
    return sh(cmd, env=dict(os.environ, CARGO_TARGET_DIR=str(target)))


def run_one(name, a):
    edits, programs, lc = MUTATIONS[name] if name != 'baseline' else ([], a.baseline, False)
    saved = {}
    try:
        for path, old, new in edits:
            text = (ROOT / path).read_text()
            saved.setdefault(path, text)
            current = (ROOT / path).read_text()
            if current.count(old) != 1:
                return 'pattern not found or not unique (the source changed: update this table): ' + path + ': ' + old[:60]
            (ROOT / path).write_text(current.replace(old, new, 1))
        target = ROOT / 'target' / ('mut-lc' if lc else 'mut')
        r = build_resident(target, 'link-check' if lc else None)
        if r.returncode != 0:
            return 'build failed: ' + r.stderr[-600:]
        host = a.host
        if any(p == HOST for p, _, _ in edits):
            r = sh(['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-host'], env=dict(os.environ, CARGO_TARGET_DIR=str(ROOT / 'target' / 'mut-host')))
            if r.returncode != 0:
                return 'host build failed: ' + r.stderr[-600:]
            host = ROOT / 'target' / 'mut-host' / 'release' / 'natemu-host'
            sh(['codesign', '--force', '--sign', '-', '--entitlements', str(ROOT / 'crates/host/entitlements.plist'), str(host)])
        out = a.out / name
        r = sh([sys.executable, 'tools/vm_lock.py', '--shared', 'tools/with-env', sys.executable, 'tools/run_k1.py', '--fexc', str(a.fexc), '--out', str(out), '--cpus', '1',
                '--programs', programs, '--host', str(host), '--runtime', str(target / 'aarch64-unknown-none/release/natemu-runtime')])
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
    ap.add_argument('--baseline', default=NEW + ',cr3-link,remap-link,cross-link,local-gpa,global,place-global,local-write,link-full')
    a = ap.parse_args()
    if a.list:
        for n, (edits, programs, lc) in MUTATIONS.items():
            print(f'{n}{" (link-check)" if lc else ""}: {programs}')
        for n, why in NOT_TESTABLE.items():
            print(f'{n}: not testable: {why}')
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
        print('sources not restored (or uncommitted changes in them):\n' + dirty)
    return 0 if all(v in ('red', 'green') for v in results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
