"""Build/sign/disassemble only; never executes the resulting instrument."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from run import HERE, REPO, raw_root, packet, sha, write_new


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    root=raw_root()
    output=args.output.resolve()
    if output==REPO or REPO in output.parents or not (output==root or root in output.parents):
        raise SystemExit("build output must be in external NATEMU_RAW_ROOT")
    output.mkdir(parents=True,exist_ok=False)
    try:
        sdk=Path(subprocess.check_output(["xcrun","--sdk","macosx","--show-sdk-path"],text=True).strip())
        if not sdk.is_dir():
            raise SystemExit("selected SDK unavailable")
        snapshot=output/"snapshot"
        sources=packet()
        for name in sources["files"]:
            target=snapshot/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(HERE/name,target)
        shutil.copy2(REPO/"experiments/INV-01-07/c1/clock_evidence.py",snapshot/"clock_evidence.py")
        sources=packet(snapshot)
        if any(sha(HERE/name)!=expected for name,expected in sources["files"].items() if name!="clock_evidence.py"):
            raise SystemExit("source changed while preparing snapshot")
        commands=[]
        build_id=uuid.uuid4().hex
        cc=["xcrun","clang",'-DINV18_BUILD_ID="'+build_id+'"',"-arch","arm64","-isysroot",str(sdk),"-O2","-g","-std=c11",
            "-Wall","-Wextra","-Werror","-pthread",str(snapshot/"src/protocol.c"),"-o",str(output/"protocol-A")]
        outcome="failed"
        try:
            with (output/"build.log").open("xb") as log:
                commands.append(cc)
                subprocess.run(cc,stdout=log,stderr=subprocess.STDOUT,check=True)
                for signing in ("A","B","C"):
                    binary=output/("protocol-"+signing)
                    if signing!="A":
                        shutil.copy2(output/"protocol-A",binary)
                    argv=["codesign","--force","--sign","-"]
                    if signing!="A":
                        argv += ["--options","runtime","--entitlements",str(snapshot/("entitlements-"+signing+".plist"))]
                    argv.append(str(binary))
                    subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT,check=True)
                    commands.append(argv)
                    for name,command in (
                        ("signature",["codesign","-d","--verbose=4","--entitlements",":-",str(binary)]),
                        ("disassembly",["xcrun","llvm-objdump","-d",str(binary)])):
                        with (output/(signing+"-"+name+".txt")).open("xb") as stream:
                            subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
            outcome="built"
        finally:
            files={str(path.relative_to(output)):sha(path) for path in output.rglob("*") if path.is_file()}
            write_new(output/"build-manifest.json",dict(schema="INV-01-18-build-v2",run_id=build_id,
                        outcome=outcome,code_packet=sources,commands=commands,files=files,runtime_executed=False,
                        sdk=str(sdk),sdk_version=subprocess.check_output(["xcrun","--show-sdk-version"],text=True).strip(),
                        compiler=subprocess.check_output(["xcrun","clang","--version"],text=True)))
        print(output)
    except BaseException as exc:
        if not (output/"build-manifest.json").exists():
            write_new(output/"build-manifest.json",dict(schema="INV-01-18-build-v2",outcome="failed",
                      error=dict(type=type(exc).__name__,message=str(exc)),runtime_executed=False,
                      files={str(p.relative_to(output)):sha(p) for p in output.rglob("*") if p.is_file()}))
        raise



if __name__=="__main__":
    main()
