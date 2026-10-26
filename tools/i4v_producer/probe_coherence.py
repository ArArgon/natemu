#!/usr/bin/env python3
"""Retain actual 64-bit-address DWORD load/store exports for coherence tests."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys


def probe(root, producer):
    root = Path(root).resolve()
    root.mkdir()
    snapshot = root / 'source'
    snapshot.mkdir()
    for name in ('compile.py', 'export.py', 'producer.json', 'probe_coherence.py'):
        shutil.copyfile(Path(__file__).with_name(name), snapshot / name)
    shutil.copyfile(producer, root / 'CodeSizeValidation')
    (root / 'CodeSizeValidation').chmod(0o755)
    sys.path.insert(0, str(snapshot))
    from compile import compile_source
    from export import CASES, export_program, verify
    record = dict(schema='natemu-i4v-coherence-probe-v1', status='running', exports=[])
    try:
        for name in ('store', 'load'):
            case = f'fex_{name}_eax_rbx64'
            spec = CASES[case]
            for index, rip in enumerate((0x102000, 0x102310)):
                directory = root / f'{name}-{index}'
                compile_source(directory, root / 'CodeSizeValidation', bytes.fromhex(spec['source']),
                               rip, 64, 1, spec['host_count'])
                export_program(directory, case)
                exported = verify(directory, dict(case=case, guest_rip=rip, bitness=64,
                                                   source_hex=spec['source']))
                record['exports'].append(dict(directory=directory.name, export=exported))
                (root / 'probe.json').write_text(json.dumps(record, indent=2)+'\n')
        record['status'] = 'passed_compile_only'
    except BaseException as error:
        record.update(status='failed', error=str(error))
        raise
    finally:
        (root / 'probe.json').write_text(json.dumps(record, indent=2)+'\n')
        (root / 'checksums.json').write_text(json.dumps({str(p.relative_to(root)):
            hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*'))
            if p.is_file() and p.name != 'checksums.json'}, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--producer', required=True, type=Path)
    args = parser.parse_args()
    probe(args.directory, args.producer)
