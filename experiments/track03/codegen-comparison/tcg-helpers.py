#!/usr/bin/env python3
"""Bounded retained extraction of actual QEMU numerical helpers; no guest run."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2] if len(HERE.parents) > 2 else HERE
SYMBOLS = ['helper_addsd','helper_subsd','helper_mulsd','helper_divsd','helper_sqrtsd','helper_divq_EAX']


def inside():
    work = Path('/work'); out = work/'outputs'; commands = []
    def run(argv):
        index = len(commands); commands.append(argv)
        (out/'commands.json').write_text(json.dumps(commands, indent=2)+'\n')
        with (out/f'{index:02}.stdout').open('wb') as stdout, (out/f'{index:02}.stderr').open('wb') as stderr:
            subprocess.run(argv, cwd=work/'build', stdout=stdout, stderr=stderr, check=True, timeout=180)
    entries = json.loads((work/'build/compile_commands.json').read_text())
    def flags(ending):
        entry = next(x for x in entries if x['file'].endswith(ending))
        words = shlex.split(entry['command']); result = []; skip = False
        for w in words[1:]:
            if skip: skip = False; continue
            if w in ('-o','-MF','-MQ','-MT'): skip = True; continue
            if w in ('-c','-MD','-MMD') or w == entry['file']: continue
            result.append(w)
        return result + ['-ffunction-sections','-fdata-sections','-fno-stack-protector','-fno-pie','-fno-pic','-fno-math-errno']
    objects=[]
    for name in ('fpu_helper.c','int_helper.c','softfloat.c'):
        entry = next(x for x in entries if x['file'].endswith('/'+name))
        obj=out/(name+'.o');run(['clang-19']+flags('/'+name)+['-c',entry['file'],'-o',str(obj)]);objects.append(str(obj))
    layout=out/'layout.c'
    layout.write_text('''#include "qemu/osdep.h"
#include "cpu.h"
bool tcg_allowed = true;
int main(void) {
    CPUX86State env = {0};
    cpu_init_fp_statuses(&env);
    cpu_set_mxcsr(&env, 0x1f80);
    FILE *f = fopen("/work/outputs/env.bin", "wbx");
    if (!f || fwrite(&env, 1, sizeof(env), f) != sizeof(env) || fclose(f)) return 1;
    printf("{\\"env_size\\":%zu,\\"sse_status\\":%zu,\\"mxcsr\\":%zu,\\"xmm0\\":%zu,\\"xmm1\\":%zu}\\n",
        sizeof(env), offsetof(CPUX86State,sse_status), offsetof(CPUX86State,mxcsr),
        offsetof(CPUX86State,xmm_regs[0]), offsetof(CPUX86State,xmm_regs[1]));
    return 0;
}
''')
    # Host program only initializes a template through actual upstream setters.
    run(['clang-19']+flags('/fpu_helper.c')+['-Wl,--gc-sections','-no-pie',str(layout)]+objects+['-lm','-o',str(out/'layout')])
    run([str(out/'layout')]);shutil.copyfile(out/f'{len(commands)-1:02}.stdout',out/'layout.json')
    bridge=out/'bridge.S'
    bridge.write_text('''.text
.global raise_exception_ra, raise_exception_err_ra
raise_exception_ra:
raise_exception_err_ra:
hvc #0x6e
b .
.global __assert_fail, g_assertion_message_expr
__assert_fail:
g_assertion_message_expr:
hvc #0x6f
b .
''')
    run(['clang-19','-c',str(bridge),'-o',str(out/'bridge.o')])
    script=out/'helpers.ld'
    script.write_text('SECTIONS { . = 0x40008000; .text : { *(.text .text.*) } .rodata : { *(.rodata .rodata.*) } .data : { *(.data .data.*) } .bss : { *(.bss .bss.* COMMON) } /DISCARD/ : { *(.eh_frame*) *(.comment) *(.note*) } }\n')
    # --no-undefined deliberately refuses any surviving Linux/libc dependency.
    linker=shutil.which('ld.lld-19') or shutil.which('ld.lld') or shutil.which('ld')
    run([linker,'--gc-sections','--no-undefined','-T',str(script),'-Map='+str(out/'helpers.map'),
         *[v for symbol in SYMBOLS for v in ('-u',symbol)],*objects,str(out/'bridge.o'),'-o',str(out/'helpers.elf')])
    run(['llvm-objcopy-19','-O','binary',str(out/'helpers.elf'),str(out/'helpers.bin')])
    run(['llvm-nm-19','-n',str(out/'helpers.elf')]);shutil.copyfile(out/f'{len(commands)-1:02}.stdout',out/'symbols.txt')
    run(['llvm-readelf-19','-lS',str(out/'helpers.elf')])
    size=(out/'helpers.bin').stat().st_size
    (out/'result.json').write_text(json.dumps({'status':'linked_not_executed','base':0x40008000,'bytes':size,'fits_32k':size<=32768,'fits_48k':size<=49152,'symbols':SYMBOLS},indent=2)+'\n')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--inside',action='store_true');parser.add_argument('--name');parser.add_argument('--input');parser.add_argument('--image');args=parser.parse_args()
    if args.inside: inside();return
    if not all((args.name,args.input,args.image)):parser.error('name/input/image required')
    spec=importlib.util.spec_from_file_location('retention',REPO/'experiments/track01/source/run.py');retention=importlib.util.module_from_spec(spec);spec.loader.exec_module(retention)
    run=retention.allocate(os.environ.get('NATEMU_RAW_ROOT'),REPO,args.name);source=run/'source';source.mkdir()
    for name in ('tcg-helpers.py','tcg-helpers.sh'):shutil.copyfile(HERE/name,source/name)
    shutil.copyfile(REPO/'experiments/track01/source/run.py',source/'retention.py')
    original=Path(args.input).resolve();shutil.copyfile(original/'manifest.json',source/'upstream-manifest.json');shutil.copyfile(original/'SHA256SUMS',source/'upstream-SHA256SUMS')
    record={'schema':'natemu.track03.tcg-helpers.v1','purpose':'exploratory_build','input':str(original),'image':args.image,'commands':[],'status':'preparing'}
    container='natemu-helper-'+uuid.uuid4().hex
    try:
        retention.save(run,record)
        retention.command(run,record,['docker','create','--name',container,'--cpus=2','--memory=8g','--memory-swap=8g','--network=none','--user=0','--entrypoint=/bin/sh','--mount',f'type=bind,src={run},dst=/work','--mount',f'type=bind,src={original},dst=/input,readonly',args.image,'/work/source/tcg-helpers.sh'],timeout=30,cpu_limit=False)
        retention.command(run,record,['docker','start','--attach',container],timeout=480,cpu_limit=False)
        record['status']='helper_build_complete'
    except BaseException as error:
        record['status']='failed';record['error']=str(error);raise
    finally:
        try:retention.command(run,record,['docker','rm','--force','--volumes',container],timeout=30,cpu_limit=False)
        except Exception as error:record['cleanup_error']=str(error)
        retention.save(run,record);retention.checksums(run);print(run)


if __name__=='__main__':main()
