"""Executed only inside the pinned container, with no network or dependencies."""
import hashlib, json, pathlib, shutil, subprocess, sys
from model import elf, compare
root=pathlib.Path('/work')
def run(args, directory, name):
    result=subprocess.run(args,cwd=directory,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
    (directory/(name+'.stdout')).write_bytes(result.stdout)
    (directory/(name+'.stderr')).write_bytes(result.stderr)
    (directory/(name+'.command.json')).write_text(json.dumps({'argv':args,'returncode':result.returncode}))
    if result.returncode: raise RuntimeError(name+' failed')
    return result.stdout
run(['/work/pin/qemu-x86_64-static','--version'],root,'qemu-version')
nasm_version=run(['nasm','-v'],root,'nasm-version')
if nasm_version.strip()!=b'NASM version 2.16.03': raise RuntimeError('pinned NASM version')
run(['python3','--version'],root,'python-version')
(root/'executed-binaries.json').write_text(json.dumps({name:{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for name,path in [('qemu',pathlib.Path('/work/pin/qemu-x86_64-static')),('nasm',pathlib.Path(shutil.which('nasm'))),('python',pathlib.Path(sys.executable))]},indent=2))
results=[]
for d in sorted((root/'cases').iterdir()):
    p=json.loads((d/'projection.json').read_text())
    run(['nasm','-f','bin','harness.asm','-o','harness.bin','-l','harness.lst'],d,'assemble')
    (d/'harness.elf').write_bytes(elf((d/'harness.bin').read_bytes(),p))
    (d/'harness.elf').chmod(0o755)
    output=run(['/work/pin/qemu-x86_64-static','-cpu','qemu64','-d','in_asm','-D','qemu.log','harness.elf'],d,'qemu')
    results.append(compare(p,output))
(root/'comparison.json').write_text(json.dumps(results,indent=2)+'\n')
