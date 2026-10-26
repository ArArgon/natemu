#!/usr/bin/env python3
"""Retain the live-result LFENCE candidate; this is compile-only evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

# STMXCSR is a subsequent semantic instruction, not part of this FEX request.
SOURCE = bytes.fromhex('0f58c14801d10faee89f0f90c048890f66894708f30f7f4710')
INSTRUCTIONS = (
    (0, 3, 'addps xmm0,xmm1'), (3, 3, 'add rcx,rdx'),
    (6, 3, 'lfence'), (9, 1, 'lahf'), (10, 3, 'seto al'),
    (13, 3, 'mov [rdi],rcx'), (16, 4, 'mov [rdi+8],ax'),
    (20, 5, 'movdqu [rdi+16],xmm0'),
)
RIPS = (0x102000, 0x102100)


def fence_offset(body):
    if not body or len(body) % 4:
        raise ValueError('incomplete native instructions')
    matches = [at for at in range(0, len(body), 4)
               if body[at:at + 4] == bytes.fromhex('bf3d03d5')]
    if len(matches) != 1:
        raise ValueError('expected exactly one native DMB LD')
    return matches[0]


def probe(root, producer):
    root = Path(root).resolve()
    root.mkdir()  # Never overwrite a completed or failed producing directory.
    snapshot = root / 'source'
    snapshot.mkdir()
    result = dict(schema='natemu-i4v-lfence-probe-v1', status='running',
                  bitness=64, rips=RIPS, source_hex=SOURCE.hex(),
                  source_sha256=hashlib.sha256(SOURCE).hexdigest(),
                  instructions=INSTRUCTIONS, semantic_suffix_hex='0fae5f20',
                  live_root_audit='pending emitted-code inspection', outputs=[])
    try:
        for name in ('compile.py', 'ownership.py', 'export.py', 'producer.json', 'probe_lfence.py'):
            shutil.copyfile(Path(__file__).with_name(name), snapshot / name)
        shutil.copyfile(producer, root / 'CodeSizeValidation')
        (root / 'CodeSizeValidation').chmod(0o755)
        sys.path.insert(0, str(snapshot))
        from compile import compile_source
        discovery = root / 'discovery'
        compile_source(discovery, root / 'CodeSizeValidation', SOURCE,
                       RIPS[0], 64, len(INSTRUCTIONS), 0)
        counts = re.findall(r"Testing instruction 'natemu-i4v-request': (\d+) host instructions",
                            (discovery / 'producer.log').read_text())
        if not counts or len(set(counts)) != 1 or not 0 < int(counts[0]) <= 128:
            raise ValueError('ambiguous or excessive native instruction count')
        count = result['host_count'] = int(counts[0])
        for index, rip in enumerate(RIPS):
            directory = root / f'checked-{index}'
            compiled = compile_source(directory, root / 'CodeSizeValidation', SOURCE,
                                      rip, 64, len(INSTRUCTIONS), count)
            native = (directory / 'outputs/001.native').read_bytes()
            body = native[12:12 + 4 * count]
            if len(body) != 4 * count or native[12 + 4 * count:16 + 4 * count].hex() != '0f420000':
                raise ValueError('unexpected native body extent')
            offset = fence_offset(body)
            (directory / 'body.bin').write_bytes(body)
            assembly = '.text\n' + '\n'.join(
                '.long ' + hex(int.from_bytes(body[i:i + 4], 'little'))
                for i in range(0, len(body), 4)) + '\n'
            (directory / 'body.s').write_text(assembly)
            commands = []
            for argv in (
                ['xcrun', 'clang', '-target', 'arm64-apple-macos', '-c',
                 str(directory / 'body.s'), '-o', str(directory / 'body.o')],
                ['xcrun', 'llvm-objdump', '-d', str(directory / 'body.o')],
            ):
                done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
                commands.append(dict(argv=argv, returncode=done.returncode,
                                     stdout=done.stdout, stderr=done.stderr))
                (directory / 'disassembly.json').write_text(json.dumps(commands, indent=2) + '\n')
                done.check_returncode()
            result['outputs'].append(dict(directory=directory.name, guest_rip=rip,
                body_hex=body.hex(), dmb_ld_offset=offset, dmb_ld_guest_offset=6,
                object=compiled['object'], guest_pc=compiled['guest_pc']))
            (root / 'probe.json').write_text(json.dumps(result, indent=2) + '\n')
        if result['outputs'][0]['body_hex'] != result['outputs'][1]['body_hex']:
            raise ValueError('native body differs between admitted guest RIPs')
        result['status'] = 'passed_compile_only_pending_live_root_audit'
    except BaseException as error:
        result.update(status='failed', error=str(error))
        raise
    finally:
        (root / 'probe.json').write_text(json.dumps(result, indent=2) + '\n')
        (root / 'checksums.json').write_text(json.dumps({
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'checksums.json'
        }, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--producer', required=True, type=Path)
    args = parser.parse_args()
    probe(args.directory, args.producer)
