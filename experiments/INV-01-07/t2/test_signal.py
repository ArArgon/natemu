"""Compile the real handler/consumer; directly invoke them, without signals/timers."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

SOURCE=Path(__file__).resolve().parent

def exercise(directory):
    directory.mkdir()
    source=directory/'signal_fixture.c'
    # Include production storage, initialization, handler and consumer unchanged.
    # Stub only the unused Swift deadline linkage. Native main is never invoked.
    source.write_text('#define main t2_native_main\n#include '+json.dumps(str(SOURCE/'producer.c'))+'''\n#undef main
uint64_t t2_dispatch_deadline(uint64_t requested, uint64_t *represented) {
    (void)requested;(void)represented;abort();
}
int main(void) {
    prepare_signal_flag();
    require(take_signal()==0,"initial flag");
    handler(SIGUSR1);
    require(take_signal()==1,"published flag");
    require(take_signal()==0,"consumed flag");
    handler(SIGUSR1);handler(SIGUSR1);
    require(take_signal()==1 && take_signal()==0,"coalesced flag");
    return 0;
}
''')
    binary=directory/'signal_fixture'
    command=['xcrun','clang','-std=c11','-O2','-fblocks','-Wall','-Wextra','-Werror','-pthread','-DT2_REPO="/unused-synthetic-repo"',str(source),'-o',str(binary)]
    print(json.dumps(command),flush=True)
    subprocess.run(command,check=True,timeout=30)
    subprocess.run([str(binary)],check=True,timeout=5)
    print('PASS: production lock-free flag initialization, handler store, consume/reset; no signal delivery or timer reads')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory');args=parser.parse_args()
    if args.directory:exercise(Path(args.directory))
    else:
        with tempfile.TemporaryDirectory(prefix='t2-signal-synthetic-') as name:exercise(Path(name)/'fixture')
