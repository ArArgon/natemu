"""Compile/invoke the production receive-error reporter with fabricated inputs.

No Mach message, port destruction, faulting access or clock experiment executes.
Use a retained compile-only build to supply the actual MIG inputs and source.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--build',type=Path,required=True)
    args=parser.parse_args();build=args.build.resolve(strict=True)
    source=build/'sources/src/state_server.c'
    with tempfile.TemporaryDirectory(prefix='m06-receive-error-') as temporary:
        directory=Path(temporary);wrapper=directory/'fixture.c';binary=directory/'fixture'
        wrapper.write_text('#define main original_main\n#include '+json.dumps(str(source))+'''
#undef main
int main(int argc,char **argv) {
 if(argc!=4) return 2;
 cfg.n=2; cfg.negative="receive-right-destruction";
 workers[0].id=0;workers[0].front=100;
 workers[1].id=1;workers[1].front=101;
 atomic_store_explicit(&workers[0].registered,1,memory_order_release);
 atomic_store_explicit(&workers[1].registered,1,memory_order_release);
 atomic_store_explicit(&workers[0].receive_destroyed,atoi(argv[3]),memory_order_release);
 receive_failure((mach_port_t)strtoul(argv[1],NULL,0),(mach_msg_return_t)strtoul(argv[2],NULL,0));
}
''')
        sdk=subprocess.check_output(['xcrun','--show-sdk-path'],text=True,timeout=10).strip()
        subprocess.run(['xcrun','clang','-arch','arm64','-isysroot',sdk,'-O1','-std=c11',
                        '-Wall','-Wextra','-Werror','-I',str(build),str(wrapper),
                        str(build/'mach_excServer.c'),str(build/'mach_excUser.c'),
                        '-o',str(binary)],check=True,capture_output=True,timeout=60)
        for port,code,owned in [(100,0x10004002,True),(100,0x10004006,True),
                                (100,0x10004009,True),(100,0x10004003,False),
                                (101,0x10004006,False)]:
            result=subprocess.run([str(binary),str(port),str(code),'1'],capture_output=True,text=True,timeout=3)
            assert result.returncode==70,result
            witness,terminal=map(json.loads,result.stdout.splitlines())
            assert witness==dict(kind='receive_failure',schema='M06-M1-receive-failure-v1',
                worker=port-100,port=port,role='front',code=code,destruction_observed=port==100),witness
            assert terminal==dict(kind='terminal',reason='owned receive right invalidated' if owned else 'receive failure',code=code)
        # Without the actual publication the owned branch must not emit a witness.
        child=subprocess.Popen([str(binary),'100',str(0x10004006),'0'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            child.communicate(timeout=.2)
            raise AssertionError('unpublished destruction did not wait')
        except subprocess.TimeoutExpired:
            child.kill();out,err=child.communicate(timeout=3)
            assert child.returncode<0 and not out and not err
        finally:
            if child.poll() is None:child.kill();child.wait()
    print('production receive-error reporter: five exact cases and missing-publication containment passed')


if __name__=='__main__':main()
