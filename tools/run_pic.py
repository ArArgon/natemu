#!/usr/bin/env python3
"""PIC study stage 2 checks (docs/restructure/pic-plan.md section 6): the K1 programs (tools/run_k1.py, natemu only, CPL 0;
the program driver places every privilege's templates position-independent) under each of the resident's test entries
(`NATEMU_PIC_TEST`, `cache::Control::pic_test`), on every second entry of a position-independent block from the resident:

- `and`, `str`, `body`: entered at its full entry's `and` (x0 = its RIP), its `str` (x0 = its page base) or its body (the
  page base in the guest-state page) with the vCPU's own doorbell rung: the stop there must take the block's own RIP (every
  program's values as without the test), stops counted at that word (`pic` counter ENTRY_STOPS + 0, 1, 2), no slot mismatch.
- `svc`: only its `svc`-word links armed: stops at a linked `svc` word, the RIP the link point's target (SVC_STOPS > 0).
- `forge`: another page's base in the slot (the guest corrupting its own state): the programs' values may differ, but the
  resident never stops on it (no bad_link, bad_stop, reuse_mismatch, panic or unknown abort) and counts the mismatch.

Every run also requires links of both forms (same-page first-word links; `svc`-word links: cross-page and absolute into
position-independent) and the host's link audit after each program (`check_links`, in the driver) with both.

    tools/with-env python3 tools/run_pic.py --fexc FEXC --out DIR [--cpus 1 2] [--modes and,str,body,svc,forge]

Run under the shared VM lock (tools/run_suite.py takes it); exit 1 on any failure."""
import argparse, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# `natemu_abi::pic::count`.
SAME, CROSS, ABS_TO_SHARED, SLOT_MISMATCH, UNPROVED, ENTRY_STOPS, SVC_STOPS = 10, 11, 13, 18, 19, 21, 24
HALTS = ('bad_link', 'bad_stop', 'bad_reply', 'reuse_mismatch', 'status=4', 'unknown_abort')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fexc', required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--cpus', nargs='+', default=['1', '2'])
    ap.add_argument('--modes', default='and,str,body,svc,forge')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    failed = []
    for mode in a.modes.split(','):
        out = a.out / mode
        env = dict(os.environ, NATEMU_PIC_TEST=mode)
        rc = subprocess.run([sys.executable, str(ROOT / 'tools/run_k1.py'), '--fexc', a.fexc, '--out', str(out),
                             '--cpus', *a.cpus], env=env, stdout=open(a.out / f'{mode}.log', 'w'), stderr=subprocess.STDOUT).returncode
        report = json.loads((out / 'k1.json').read_text())['report']
        stats = [s['k1']['pic'] for r in report for s in r.get('stats', []) if 'k1' in s]
        total = [sum(p[i] for p in stats) for i in range(32)]
        problems = [p for r in report for p in r['problems']]
        bad = []
        if mode == 'forge':
            bad += [p for p in problems if any(h in p for h in HALTS)]
            if total[SLOT_MISMATCH] == 0:
                bad.append('no slot mismatch counted')
        else:
            if rc != 0 or problems:
                bad.append(f'run_k1 exit {rc}, {len(problems)} problems (first: {problems[:2]})')
            if total[SLOT_MISMATCH]:
                bad.append(f'{total[SLOT_MISMATCH]} slot mismatches')
            want = SVC_STOPS if mode == 'svc' else ENTRY_STOPS + ['and', 'str', 'body'].index(mode)
            if total[want] == 0:
                bad.append(f'no stop at the tested word (counter {want})')
        for k, name in [(SAME, 'same-page'), (CROSS, 'cross-page'), (ABS_TO_SHARED, 'absolute into position-independent')]:
            if total[k] == 0:
                bad.append(f'no {name} link')
        if total[UNPROVED]:
            bad.append(f'{total[UNPROVED]} rewrites unproved')
        print(f"{mode}: {'FAIL ' + '; '.join(bad) if bad else 'ok'} (entry stops {total[ENTRY_STOPS:ENTRY_STOPS + 3]}, svc stops "
              f"{total[SVC_STOPS]}, slot mismatches {total[SLOT_MISMATCH]}, links same/cross/abs->pic "
              f"{total[SAME]}/{total[CROSS]}/{total[ABS_TO_SHARED]})", flush=True)
        failed += [mode] * bool(bad)
    return int(bool(failed))


if __name__ == '__main__':
    sys.exit(main())
