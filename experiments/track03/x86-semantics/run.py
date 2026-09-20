#!/usr/bin/env python3
"""Retain source preparation, a Linux build, or one separately reviewed run."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
INPUTS = ("probe.c", "README.md", "run.py")


def load(path):
    spec = importlib.util.spec_from_file_location("retention", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--explore", action="store_true",
                        help="explicitly permit non-Intel/hypervisor exploratory observations")
    args = parser.parse_args()
    helper = REPO / "experiments/track01/source/run.py"
    retention = load(helper)
    run = retention.allocate(os.environ.get("NATEMU_RAW_ROOT"), REPO, args.name)
    source = run / "source"
    source.mkdir()
    record = dict(schema="natemu.track03.x86-semantics.v2", status="preparing",
                  purpose="exploratory", explore_requested=args.explore,
                  applicability_mode="explicit_any_x86_64" if args.explore else "native_intel_guard",
                  commands=[], execute_requested=args.execute,
                  started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  budget=dict(build_seconds=60, probe_seconds=45,
                              probe_invocations=1, cases=6, retries=0))
    try:
        for name in INPUTS:
            shutil.copyfile(HERE / name, source / name)
        shutil.copyfile(helper, source / "retention.py")
        for name in INPUTS:
            if (HERE / name).read_bytes() != (source / name).read_bytes():
                raise RuntimeError("source changed during capture: " + name)
        if helper.read_bytes() != (source / "retention.py").read_bytes():
            raise RuntimeError("retention helper changed during capture")
        record["inputs"] = {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in (*INPUTS, "retention.py")}
        retention = load(source / "retention.py")
        if args.source_only:
            record["status"] = "source_only_not_built_or_executed"
            return
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise RuntimeError("requires x86_64 Linux; use --source-only elsewhere")
        for path in ("/proc/cpuinfo", "/proc/version", "/etc/os-release"):
            record.setdefault("host_files", {})[path] = Path(path).read_text()
        retention.command(run, record, ["uname", "-a"])
        compiler = shutil.which("cc")
        if not compiler:
            raise RuntimeError("cc is unavailable")
        retention.command(run, record, [compiler, "--version"])
        retention.command(run, record, [compiler, "-std=gnu11", "-O2", "-g",
            "-Wall", "-Wextra", "-Werror", source / "probe.c", "-o", run / "probe"],
            timeout=60)
        retention.command(run, record, ["objdump", "-d", run / "probe"])
        record["status"] = "built_not_executed"
        if args.execute:
            argv = [run / "probe"] + (["--explore"] if args.explore else [])
            retention.command(run, record, argv, timeout=45, cpu_limit=False)
            record["status"] = "collected_requires_findings_review"
    except BaseException as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        retention.save(run, record)
        retention.checksums(run)
        print(run)


if __name__ == "__main__":
    main()
