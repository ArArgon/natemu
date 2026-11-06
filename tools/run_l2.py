#!/usr/bin/env python3
"""L2 checks: the ACPI tables the loader writes (boot-features plan L2), judged by `tools/models/acpi_model.py`.

    python3 tools/run_l2.py --out DIR [--host HOST] [--kernel BZIMAGE] [--dumps DIR]

For N = 1..4 runs `natemu-host unused boot-linux unused BZIMAGE --ram 0x8000000 --load-only --vcpus N --dump`, checks
the dump (table validity, AML decode, MADT/CPUID/mailbox/e820/device agreement) and writes DIR/l2.json plus the DSDT
listing DIR/dsdt-nN.dsl. `--dumps DIR` judges existing nN.dump files instead of running the driver.
Prints one pass/fail line per N; exit status 1 on any failure."""
import argparse, json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from models import acpi_model  # noqa: E402

RAW = ROOT.parent / 'natemu-raw/kernel-census-20261028-01/dl'


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--out', required=True, type=pathlib.Path)
    a.add_argument('--host', type=pathlib.Path)
    a.add_argument('--kernel', type=pathlib.Path, default=RAW / 'linux')
    a.add_argument('--dumps', type=pathlib.Path)
    o = a.parse_args()
    o.out.mkdir(parents=True, exist_ok=True)
    if not o.dumps and not o.host:
        subprocess.run(['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-host'],
                       cwd=ROOT, check=True)
    host = o.host or ROOT / 'target/release/natemu-host'
    report, fail = {}, False
    for n in range(1, 5):
        if o.dumps:
            dump = o.dumps / f'n{n}.dump'
            r = None
        else:
            dump = o.out / f'n{n}.dump'
            r = subprocess.run([host, 'unused', 'boot-linux', 'unused', o.kernel, '--ram', '0x8000000', '--load-only',
                                '--vcpus', str(n), '--dump', dump], capture_output=True, text=True, timeout=300)
        if r is not None and r.returncode:
            res = dict(violations=[f'DRIVER: exit {r.returncode}: {r.stderr.strip()[-400:]}'], not_fixed=[], info={})
        else:
            res = acpi_model.check(dump.read_bytes(), n)
            (o.out / f'dsdt-n{n}.dsl').write_text(res.pop('listing') + '\n')
        if not o.dumps:
            dump.unlink(missing_ok=True)
        report[f'n{n}'] = res
        bad = res['violations']
        fail |= bool(bad)
        flags = sum(x['judgement'].startswith('FLAG') for x in res['not_fixed'])
        print(f'N={n}: {"FAIL" if bad else "pass"} ({len(bad)} violations, {flags} flagged unfixed values)')
        for v in bad:
            print('  ', v)
    (o.out / 'l2.json').write_text(json.dumps(report, indent=1, default=str))
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
