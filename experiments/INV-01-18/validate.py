"""Capture lightweight author validation externally; never run the instrument."""
import argparse
from pathlib import Path
import subprocess
import sys

from run import HERE,packet,raw_root,sha,write_new


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    root=raw_root(); output=args.output.resolve()
    if root not in output.parents:
        raise SystemExit("validation output must be fresh beneath external NATEMU_RAW_ROOT")
    output.mkdir(parents=True,exist_ok=False)
    sources=packet()
    commands=[("syntax",["clang","-fsyntax-only","-isysroot","/Library/Developer/CommandLineTools/SDKs/MacOSX26.4.sdk",
                         "-O2","-Wall","-Wextra","-Werror","-std=c11",str(HERE/"src/protocol.c")]),
              ("model",[sys.executable,"-B",str(HERE/"model.py")]),
              ("fixtures",[sys.executable,"-B",str(HERE/"tests.py")])]
    outcomes=[]
    for name,argv in commands:
        with (output/(name+".txt")).open("xb") as stream:
            result=subprocess.run(argv,stdout=stream,stderr=subprocess.STDOUT)
        outcomes.append(dict(name=name,argv=argv,returncode=result.returncode))
    write_new(output/"manifest.json",dict(code_packet=sources,commands=outcomes,
              files={path.name:sha(path) for path in output.iterdir() if path.is_file()},
              passed=all(row["returncode"]==0 for row in outcomes),native_instrument_executed=False,
              host_evidence=False))
    print(output)
    if any(row["returncode"] for row in outcomes): raise SystemExit(1)


if __name__=="__main__": main()
