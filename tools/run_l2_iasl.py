#!/usr/bin/env python3
"""L2 iasl check: the ACPI tables the loader writes (`crates/host/src/boot/acpi.rs`), judged by ACPICA's `iasl`
(Homebrew `acpica`, 20260408), for N = 1..4 processors.

    python3 tools/run_l2_iasl.py --out DIR [--host HOST] [--iasl IASL]

For each N the driver's load-only dump (`boot-linux --load-only --vcpus N --dump`) is read; the tables are found from
`boot_params.acpi_rsdp_addr` (RSDP -> XSDT -> FADT, MADT; FADT -> FACS, DSDT) and written to DIR/nN/*.dat. Then:
- `iasl -d` on the XSDT, FADT, FACS, DSDT and MADT: exit 0, no error/warning/remark in its output, and no `/****`
  diagnostic in the disassembly, with exactly one allowed exception (below);
- the DSDT recompiled from iasl's own disassembly with `-oa` (no optimisation) equals ours byte for byte, except the
  checksum (byte 9) and the compiler ID/revision (bytes 28..36);
- iasl's RSDP template (`iasl -T RSDP`) filled with our OEM ID, revision, RSDT/XSDT addresses and length, compiled,
  equals our RSDP byte for byte (iasl computes both checksums).

The one exception: the MADT's multiprocessor wakeup structure is the 16-byte version-0 form of ACPI 6.5 (Table 5.43),
which iasl 20260408 knows only in its 24-byte ACPI 6.6 form (version 1, with ResetVector), so the disassembly ends with
"ACPI table terminates in the middle of a data structure!" right after that structure. Version 1 is not used: Linux
6.12 takes a version >= 1 structure's ResetVector (here 0) for its offlining/kexec path (`madt_wakeup.c`
`acpi_parse_mp_wake`). The check requires that diagnostic to follow a type 0x10, length 0x10, version 0 subtable at the
table's end, and nothing else.

Writes DIR/l2-iasl.json; exit 1 on any failure."""
import argparse, json, pathlib, re, shutil, struct, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
KERNEL = ROOT.parent / 'natemu-raw/kernel-census-20261028-01/dl/linux'
ZERO_PAGE = 0x10000
MV0 = 'ACPI table terminates in the middle of a data structure!'


def build():
    subprocess.run(['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release', '-p', 'natemu-host'], cwd=ROOT, check=True)
    return ROOT / 'target/release/natemu-host'


def tables(dump):
    """{name: bytes} of the table graph in a loader dump (records u64 gpa, u64 length, bytes; zero elsewhere)."""
    recs, o = [], 0
    while o < len(dump):
        a, n = struct.unpack_from('<QQ', dump, o)
        recs.append((a, dump[o + 16:o + 16 + n]))
        o += 16 + n

    def rd(a, n):
        b = bytearray(n)
        for g, d in recs:
            lo, hi = max(a, g), min(a + n, g + len(d))
            if lo < hi:
                b[lo - a:hi - a] = d[lo - g:hi - g]
        return bytes(b)

    def table(a):
        return rd(a, struct.unpack_from('<I', rd(a, 8), 4)[0])
    rsdp_at = struct.unpack_from('<Q', rd(ZERO_PAGE + 0x70, 8))[0]
    out = {'rsdp': rd(rsdp_at, 36)}
    out['xsdt'] = table(struct.unpack_from('<Q', out['rsdp'], 24)[0])
    for k in range(36, len(out['xsdt']), 8):
        t = table(struct.unpack_from('<Q', out['xsdt'], k)[0])
        out[t[:4].decode().lower()] = t
    out['facs'] = rd(struct.unpack_from('<I', out['facp'], 36)[0], 64)
    out['dsdt'] = table(struct.unpack_from('<Q', out['facp'], 140)[0])
    return out


def iasl(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def problems_in(log):
    return [line for line in log.splitlines() if re.search(r'\b(Error|Warning|Remark)\b', line) and 'errors' not in line.lower()
            or re.search(r'Compilation complete\. [1-9]|[1-9]\d* Errors?|[1-9]\d* Warnings?', line)]


def check_n(host, tool, work, n):
    work.mkdir(parents=True, exist_ok=True)
    dump = work / 'loaded.dump'
    subprocess.run([host, 'unused', 'boot-linux', 'unused', KERNEL, '--ram', '0x8000000', '--load-only', '--vcpus', str(n),
                    '--dump', dump], capture_output=True, check=True)
    t = tables(dump.read_bytes())
    dump.unlink()
    v = []
    for name in ('xsdt', 'facp', 'facs', 'dsdt', 'apic'):
        (work / f'{name}.dat').write_bytes(t[name])
        rc, log = iasl([tool, '-d', f'{name}.dat'], work)
        dsl = (work / f'{name}.dsl').read_text(errors='replace') if (work / f'{name}.dsl').exists() else ''
        if rc:
            v.append(f'{name}: iasl -d exit {rc}')
        v += [f'{name}: {p}' for p in problems_in(log)]
        diags = [line for line in dsl.splitlines() if line.lstrip().startswith('/****')]
        if name == 'apic':
            # The allowed exception: the last subtable is the version-0 wakeup structure (type 10, length 10 hex).
            a = t['apic']
            last = a[-16:]
            ok_tail = last[0] == 0x10 and last[1] == 16 and last[2:4] == b'\0\0' and MV0 in dsl \
                and re.search(r'Subtable Type : 10 \[Mutiprocessor Wakeup\]\s*\n.*Length : 10\s*\n.*Mailbox Version : 0000', dsl)
            if not ok_tail:
                v.append('apic: the version-0 wakeup structure is not the last subtable as expected')
            diags = [d for d in diags if MV0 not in d]
        v += [f'{name}: {d.strip()}' for d in diags]
    # DSDT round trip: compile iasl's disassembly without optimisation, compare.
    rc, log = iasl([tool, '-oa', '-p', 'dsdt-rt', 'dsdt.dsl'], work)
    rt = work / 'dsdt-rt.aml'
    if rc or not rt.exists():
        v.append(f'dsdt: recompile failed (exit {rc}): {log[-400:]}')
    else:
        v += [f'dsdt recompile: {p}' for p in problems_in(log)]
        mask = lambda x: x[:9] + b'\0' + x[10:28] + b'\0' * 8 + x[36:]  # noqa: E731
        if mask(rt.read_bytes()) != mask(t['dsdt']):
            v.append(f'dsdt: recompiled bytes differ\n ours {t["dsdt"][36:].hex()}\n iasl {rt.read_bytes()[36:].hex()}')
    # RSDP from iasl's template with our values.
    r = t['rsdp']
    (work / 'rsdp').mkdir(exist_ok=True)
    iasl([tool, '-T', 'RSDP'], work / 'rsdp')
    asl = (work / 'rsdp/rsdp.asl').read_text()
    for field, value in (('Oem ID', '"%s"' % r[9:15].decode()), ('Revision', '%02X' % r[15]),
                         ('RSDT Address', '%08X' % struct.unpack_from('<I', r, 16)[0]),
                         ('Length', '%08X' % struct.unpack_from('<I', r, 20)[0]),
                         ('XSDT Address', '%016X' % struct.unpack_from('<Q', r, 24)[0])):
        asl, k = re.subn(r'(\] +%s : ).*' % re.escape(field), lambda m: m.group(1) + value, asl)
        if k != 1:
            v.append(f'rsdp template: field {field} not found once')
    (work / 'rsdp/rsdp.asl').write_text(asl)
    rc, log = iasl([tool, 'rsdp.asl'], work / 'rsdp')
    aml = work / 'rsdp/rsdp.aml'
    if rc or not aml.exists():
        v.append(f'rsdp: template compile failed (exit {rc}): {log[-400:]}')
    elif aml.read_bytes() != r:
        v.append(f'rsdp: differs from iasl template\n ours {r.hex()}\n iasl {aml.read_bytes().hex()}')
    return v


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--out', required=True, type=pathlib.Path)
    a.add_argument('--host', type=pathlib.Path)
    a.add_argument('--iasl', default=shutil.which('iasl') or '/opt/homebrew/bin/iasl')
    o = a.parse_args()
    host = o.host or build()
    o.out.mkdir(parents=True, exist_ok=True)
    report = {}
    for n in range(1, 5):
        v = check_n(host, o.iasl, o.out / f'n{n}', n)
        report[n] = v
        print(f'N={n}: {"pass" if not v else "FAIL"}')
        for x in v:
            print('  ', x)
    (o.out / 'l2-iasl.json').write_text(json.dumps(report, indent=1))
    sys.exit(1 if any(report.values()) else 0)


if __name__ == '__main__':
    main()
