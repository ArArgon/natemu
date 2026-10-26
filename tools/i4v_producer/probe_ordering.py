#!/usr/bin/env python3
"""Retain whole FEX ordering/atomic blocks without inferred memory-site splitting."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

SOURCES = {
    'sb': ('c703010000008b02', 2),
    'mp_writer': ('c70301000000c70201000000', 2),
    'reader': ('8b038b0a', 2),
    'wrc_middle': ('8b03c70201000000', 2),
    'writer': ('c70301000000', 1),
    'sb_mfence': ('c703010000000faef08b02', 3),
    'sb_xchg': ('c7030100000087068b02', 3),
    'xchg_result': ('87038902', 2),
    'lfence': ('0faee8', 1),
    'sfence': ('0faef8', 1),
    'mfence': ('0faef0', 1),
    'xchg': ('8703', 1),
}

def probe(root, producer):
    root = Path(root).resolve(); root.mkdir()
    snapshot = root / 'source'; snapshot.mkdir()
    for name in ('compile.py', 'ownership.py', 'export.py', 'producer.json', 'probe_ordering.py'):
        shutil.copyfile(Path(__file__).with_name(name), snapshot / name)
    shutil.copyfile(producer, root / 'CodeSizeValidation'); (root / 'CodeSizeValidation').chmod(0o755)
    sys.path.insert(0, str(snapshot))
    from compile import compile_source
    result = dict(schema='natemu-i4v-ordering-probe-v1',status='running',bitness=64,rips=[0x102000,0x102100],variants=[])
    try:
        for name, (source_hex, instructions) in SOURCES.items():
            raw = bytes.fromhex(source_hex); discovery = root / f'{name}-discovery'
            compile_source(discovery, root / 'CodeSizeValidation', raw, result['rips'][0], 64, instructions, 0)
            counts = re.findall(r"Testing instruction 'natemu-i4v-request': (\d+) host instructions", (discovery / 'producer.log').read_text())
            if not counts or len(set(counts)) != 1: raise ValueError('ambiguous native count')
            count = int(counts[0]); variant = dict(name=name,source_hex=source_hex,source_sha256=hashlib.sha256(raw).hexdigest(),instructions=instructions,host_count=count,outputs=[])
            for index, rip in enumerate(result['rips']):
                directory = root / f'{name}-{index}'
                compiled = compile_source(directory, root / 'CodeSizeValidation', raw, rip, 64, instructions, count)
                native = (directory / 'outputs/001.native').read_bytes(); body = native[12:12 + 4 * count]
                if native[12 + 4 * count:16 + 4 * count].hex() != '0f420000': raise ValueError('unexpected body end')
                (directory / 'body.bin').write_bytes(body)
                assembly = '.text\n' + '\n'.join('.long ' + hex(int.from_bytes(body[i:i+4], 'little')) for i in range(0,len(body),4)) + '\n'
                (directory / 'body.s').write_text(assembly)
                commands = []
                for argv in (['xcrun','clang','-target','arm64-apple-macos','-c',str(directory/'body.s'),'-o',str(directory/'body.o')], ['xcrun','llvm-objdump','-d',str(directory/'body.o')]):
                    done = subprocess.run(argv, capture_output=True, text=True, check=True)
                    commands.append(dict(argv=argv,returncode=done.returncode,stdout=done.stdout,stderr=done.stderr))
                (directory / 'disassembly.json').write_text(json.dumps(commands,indent=2)+'\n')
                variant['outputs'].append(dict(directory=directory.name,guest_rip=rip,body_hex=body.hex(),object=compiled['object'],guest_pc=compiled['guest_pc']))
            result['variants'].append(variant)
            (root/'probe.json').write_text(json.dumps(result,indent=2)+'\n')
        result['status']='passed_compile_only'
    except BaseException as error:
        result.update(status='failed',error=str(error)); raise
    finally:
        (root/'probe.json').write_text(json.dumps(result,indent=2)+'\n')
        (root/'checksums.json').write_text(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file() and p.name!='checksums.json'},indent=2)+'\n')

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path);parser.add_argument('--producer',required=True,type=Path)
    args=parser.parse_args();probe(args.directory,args.producer)
