#!/usr/bin/env python3
"""L2 checks: the ACPI tables the loader writes (boot-features plan L2), judged by `tools/models/acpi_model.py`.

    python3 tests/boot/run_l2.py --out DIR [--host HOST] [--kernel BZIMAGE] [--dumps DIR]

For N = 1..4 runs `natemu-host unused boot-linux unused BZIMAGE --ram 0x8000000 --load-only --vcpus N --dump`, checks
the dump (table validity, AML decode, MADT/CPUID/mailbox/e820/device agreement) and writes DIR/l2.json plus the DSDT
listing DIR/dsdt-nN.dsl; then again with `--no-mailbox` (`nN-no-mailbox`: the MADT without the wakeup structure, M2
Part 2), and again with the board's optional platform devices (`nN-devices`: `DEVICES`, the platform-devices plan; the
DSDT then lists the pvpanic device and the 8042's keyboard and mouse, and the FADT has the 8042 bit; the SMBIOS tables, which must leave the ACPI tables as they are, are in the dump too). `--dumps DIR` judges existing nN.dump (and nN-no-mailbox.dump, nN-devices.dump)
files instead of running the driver.
Prints one pass/fail line per N; exit status 1 on any failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
from raw_root import RAW_ROOT  # noqa: E402
sys.path.insert(0, str(ROOT / 'tools'))
from models import acpi_model  # noqa: E402

RAW = RAW_ROOT / 'kernel-census-20261028-01/dl'
# The driver's flags for the board's optional platform devices (all off by default), for the `nN-devices` dumps.
DEVICES = ['--pvpanic', '--smbios', '--ps2']
# Each dump: its name's suffix, the driver's extra flags, and what the model expects.
VARIANTS = [('', [], dict()),
            ('-no-mailbox', ['--no-mailbox'], dict(mailbox=False)),
            ('-devices', DEVICES, dict(pvpanic=True, ps2=True))]


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
    for (suffix, flags, expect), n in [(v, n) for v in VARIANTS for n in range(1, 5)]:
        name = f'n{n}{suffix}'
        if o.dumps:
            dump = o.dumps / f'{name}.dump'
            r = None
        else:
            dump = o.out / f'{name}.dump'
            r = subprocess.run([host, 'unused', 'boot-linux', 'unused', o.kernel, '--ram', '0x8000000', '--load-only',
                                '--vcpus', str(n), '--dump', dump, *flags],
                               capture_output=True, text=True, timeout=300)
        if r is not None and r.returncode:
            res = dict(violations=[f'DRIVER: exit {r.returncode}: {r.stderr.strip()[-400:]}'], not_fixed=[], info={})
        else:
            res = acpi_model.check(dump.read_bytes(), n, **expect)
            (o.out / f'dsdt-{name}.dsl').write_text(res.pop('listing') + '\n')
        if not o.dumps:
            dump.unlink(missing_ok=True)
        report[name] = res
        bad = res['violations']
        fail |= bool(bad)
        flags = sum(x['judgement'].startswith('FLAG') for x in res['not_fixed'])
        print(f'{name}: {"FAIL" if bad else "pass"} ({len(bad)} violations, {flags} flagged unfixed values)')
        for v in bad:
            print('  ', v)
    (o.out / 'l2.json').write_text(json.dumps(report, indent=1, default=str))
    (o.out / 'results.json').write_text(json.dumps({'results': [   # per-case rows for the framework
        {'name': k, 'outcome': 'fail' if r['violations'] else 'match', 'diffs': list(map(str, r['violations']))}
        for k, r in report.items()]}, indent=1))
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()
