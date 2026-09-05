"""Build/sign/disassemble only; never executes the resulting instrument."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

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
    sdk=Path("/Library/Developer/CommandLineTools/SDKs/MacOSX26.4.sdk")
    if not sdk.is_dir():
        raise SystemExit("exact SDK26.4 unavailable; no silent SDK replacement")
    sources=packet()
    commands=[]
    cc=["xcrun","clang","-arch","arm64","-isysroot",str(sdk),"-O2","-g","-std=c11",
        "-Wall","-Wextra","-Werror","-pthread",str(HERE/"src/protocol.c"),"-o",str(output/"protocol-A")]
    with (output/"build.log").open("xb") as log:
        subprocess.run(cc,stdout=log,stderr=subprocess.STDOUT,check=True)
        commands.append(cc)
        for signing in ("A","B","C"):
            binary=output/("protocol-"+signing)
            if signing!="A":
                shutil.copy2(output/"protocol-A",binary)
            argv=["codesign","--force","--sign","-"]
            if signing!="A":
                argv += ["--options","runtime","--entitlements",str(HERE/("entitlements-"+signing+".plist"))]
            argv.append(str(binary))
            subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT,check=True)
            commands.append(argv)
            for name,command in (
                ("signature",["codesign","-d","--verbose=4","--entitlements",":-",str(binary)]),
                ("disassembly",["xcrun","llvm-objdump","-d",str(binary)])):
                with (output/(signing+"-"+name+".txt")).open("xb") as stream:
                    subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
    files={path.name:sha(path) for path in output.iterdir() if path.is_file()}
    write_new(output/"build-manifest.json",dict(code_packet=sources,commands=commands,files=files,
                                               runtime_executed=False,sdk=str(sdk),
                                               compiler=subprocess.check_output(["xcrun","clang","--version"],text=True),
                                               source_revision=subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip(),
                                               source_worktree_status=subprocess.check_output(["git","status","--porcelain","--",str(HERE)],cwd=REPO,text=True)))
    print(output)


if __name__=="__main__":
    main()
