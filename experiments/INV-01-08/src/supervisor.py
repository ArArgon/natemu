#!/usr/bin/env python3
"""External snapshot build and exact-child watchdog; reviews live in the investigation."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import shutil
import uuid
import signal

sys.dont_write_bytecode = True
from evidence import (EXPERIMENT, REPO, bundle, canonical, digest, external,
                           file_sha, json_read, live_context, require)
from fallback import validate_fallback, validate_full_counts

MODES = {"normal": 600, "negative": 600, "fallback-only": 60,
         "fallback-drop-write": 60, "fallback-lock-error": 60, "unexpected-fault": 5}


CAMPAIGN_MODES = ("normal", "negative", "fallback-drop-write", "fallback-lock-error", "unexpected-fault")
CAMPAIGN_SECONDS = 45 * 60
CAMPAIGN_BYTES = 128 * 1024 * 1024


def output_bytes(directory):
    return sum(p.stat().st_size for p in Path(directory).rglob("*") if p.is_file())


def supervise(command, stdout, stderr, timeout, env=None, *, output_root=None, max_bytes=None):
    """A campaign owns a process group; ordinary native children inherit it."""
    start = time.monotonic_ns()
    grouped = output_root is not None
    timed_out = limited = False
    size = 0
    with open(stdout, "xb") as out, open(stderr, "xb") as err:
        child = subprocess.Popen(command, stdout=out, stderr=err, env=env, start_new_session=grouped)
        def stop():
            if grouped:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                child.kill()
        try:
            if grouped:
                while True:
                    size = output_bytes(output_root)
                    limited = size >= max_bytes
                    timed_out = (time.monotonic_ns() - start) / 1e9 >= timeout
                    code = child.poll()
                    if limited or timed_out or code is not None:
                        break
                    time.sleep(0.02)
                # Also clean any descendant that survived its direct parent.
                stop()
                code = child.wait()
            else:
                try:
                    code = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop()
                    code = child.wait()
        except BaseException:
            stop()
            child.wait()
            raise
    return {"returncode": code, "timed_out": timed_out, "output_limited": limited,
            "output_bytes": size, "output_limit_bytes": max_bytes,
            "watchdog_seconds": timeout, "supervisor_elapsed_ns": time.monotonic_ns() - start}


def exact_int(value, expected):
    """Strict int equality: True/False must never satisfy a 0/1 expectation."""
    return type(value) is int and value == expected


def control_oracle(mode, receipt, data, output):
    """Pure control classification; a successful negative is not a valid sample."""
    if receipt.get("timed_out") is not False:
        return False
    if mode == "unexpected-fault":
        return (type(receipt.get("returncode")) is int and receipt["returncode"] == 90 and
                data is None and receipt.get("result_sha256") is None and not output.strip())
    if not isinstance(data, dict) or data.get("mode") != mode or data.get("completed_normal") is not False:
        return False
    # REV-210 R2: `standalone` and the nested fallback `completed_normal` are
    # both restamped unconditionally by the repaired copy below, so a
    # malformed original value here would otherwise never be inspected by
    # anything (the copy conceals it before validate_fallback ever runs).
    if data.get("standalone") is not True:
        return False
    f = data.get("fallback_global_lock", {})
    if not isinstance(f, dict) or f.get("completed_normal") is not False:
        return False
    failures = validate_fallback(data)
    if mode == "fallback-drop-write":
        if type(receipt.get("returncode")) is not int or receipt["returncode"] != 1 or f.get("injected") is not True:
            return False
        # Do not accept arbitrary failed runs as the intended omitted-store
        # control: all other fields must pass the same positive oracle, and
        # every count here is checked as an actual int (never a bool that
        # happens to equal 0/1 under Python's `==`).
        if not (exact_int(f.get("completed_ops"), 1000000) and exact_int(f.get("ops"), 1000000) and
                exact_int(f.get("actual_writes"), 999999) and exact_int(f.get("final_value"), 999999) and
                exact_int(f.get("lost_updates"), 1)):
            return False
        import copy
        repaired = copy.deepcopy(data)
        r = repaired["fallback_global_lock"]
        workers = r.get("workers", [])
        if len(workers) != 4 or any(not exact_int(w.get("actual_writes"), 249999 if w.get("id") == 0 else 250000)
                                    for w in workers):
            return False
        repaired.update(mode="normal", completed_normal=True, standalone=False)
        r.update(injected=False, completed_normal=True, actual_writes=1000000, final_value=1000000, lost_updates=0)
        next(w for w in workers if w["id"] == 0)["actual_writes"] = 250000
        return bool(failures) and not validate_fallback(repaired)
    if mode == "fallback-lock-error":
        if type(receipt.get("returncode")) is not int or receipt["returncode"] != 1 or f.get("injected") is not True or not failures:
            return False
        workers = f.get("workers", [])
        if len(workers) != 4 or sorted(w.get("id", -1) for w in workers) != list(range(4)):
            return False
        w = next(w for w in workers if w["id"] == 0)
        import errno
        fe = w.get("first_error")
        if (not isinstance(fe, dict) or set(fe) != {"operation", "iteration", "rc", "injected"}
                or fe.get("operation") != "lock" or not exact_int(fe.get("iteration"), 0)
                or not exact_int(fe.get("rc"), errno.EIO) or fe.get("injected") is not True):
            return False
        if any(not exact_int(w.get(k), 0)
               for k in ("completed_iterations", "actual_writes", "successful_locks", "successful_unlocks")):
            return False
        # Coordinator abort may stop other workers at different iterations;
        # inspect actual counts and lifetime rather than assume scheduling.
        total = 0
        for worker in workers:
            n = worker.get("completed_iterations")
            if type(n) is not int or not 0 <= n <= 250000:
                return False
            if any(type(worker.get(k)) is not int or worker[k] != n
                   for k in ("actual_writes", "successful_locks", "successful_unlocks")):
                return False
            if worker.get("ready") is not True or not exact_int(worker.get("qos_rc"), 0):
                return False
            # RC2 (REV-218): `.get(...) is not None` is satisfied by a
            # missing key, and the repair below unconditionally restamps
            # first_error=None, so an original raw that simply omits the
            # field on a non-injected worker was concealed before
            # validate_fallback's F3 (which requires the key present) ever
            # saw it. Require the key present, matching the drop-write
            # branch's already-correct behavior.
            if worker["id"] != 0 and ("first_error" not in worker or worker["first_error"] is not None):
                return False
            total += n
        consistent = (exact_int(f.get("ops"), total) and exact_int(f.get("completed_ops"), total) and
                exact_int(f.get("actual_writes"), total) and exact_int(f.get("final_value"), total) and
                exact_int(f.get("lost_updates"), 1000000 - total)
                and f.get("created") == f.get("joined") == 4
                and f.get("create_rc") == f.get("join_rc") == [0] * 4
                and f.get("init_rc") == f.get("destroy_rc") == 0
                and all(f.get("canary_" + side + suffix) == value
                        for side, value in (("before", 0xa5a5a5a5), ("after", 0x5a5a5a5a))
                        for suffix in ("_initial", "_final")))
        if not consistent:
            return False
        import copy
        repaired = copy.deepcopy(data)
        repaired.update(mode="normal", completed_normal=True, standalone=False)
        r = repaired["fallback_global_lock"]
        r.update(injected=False, completed_normal=True, ops=1000000, completed_ops=1000000,
                 actual_writes=1000000, final_value=1000000, lost_updates=0)
        for w in r["workers"]:
            if type(w.get("completed")) is not bool or w["completed"] != (w["completed_iterations"] == 250000):
                return False
            w.update(completed=True, completed_iterations=250000, actual_writes=250000,
                     successful_locks=250000, successful_unlocks=250000, first_error=None)
        return not validate_fallback(repaired)
    return False


def write_new(path, value):
    with open(path, "xb") as stream:
        stream.write(canonical(value) + b"\n")


def sequential_oracle(data, receipt):
    import check
    import contextlib
    import copy
    import io
    if not isinstance(data, dict) or data.get("mode") != "negative" or data.get("completed_normal") is not False:
        return False
    if type(receipt.get("returncode")) is not int or receipt["returncode"] != 0 or receipt.get("timed_out") is not False:
        return False
    repaired = copy.deepcopy(data)
    repaired.update(mode="normal", completed_normal=True)
    if validate_fallback(repaired) or validate_full_counts(repaired):
        return False
    if len(data.get("litmus", [])) != 12 or any(row.get("sequential") is not True for row in data["litmus"]):
        return False
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        code = check.main("sequential-control", data=data, integrity_only=True, historical=True)
    lines = capture.getvalue().splitlines()
    failed = [line for line in lines if line.startswith("  FAIL  I")]
    # REV-210 R1: exactly I2/I3 failing is not sufficient on its own -- those
    # are the only two rules a real sequential run always trips, so a
    # corrupted/missing/mismatched histogram elsewhere (reported as a
    # "FAIL  H-input" line by check.main) must also independently reject.
    other_fails = [line for line in lines if line.startswith("  FAIL") and line not in failed]
    return (code == 1 and len(failed) == 2 and not other_fails
            and any("FAIL  I2 " in line for line in failed) and any("FAIL  I3 " in line for line in failed))


def environment():
    """Read-only platform/signing/topology/power capture, preserving failures."""
    commands = [
        ["sw_vers"], ["sysctl", "hw.model", "hw.memsize", "hw.ncpu", "hw.pagesize", "kern.osversion",
                      "hw.perflevel0.logicalcpu", "hw.perflevel1.logicalcpu",
                      "hw.optional.arm.FEAT_LSE", "hw.optional.arm.FEAT_LSE2"],
        ["clang", "--version"], ["xcrun", "--show-sdk-path"], ["xcrun", "--show-sdk-version"],
        ["uptime"], ["pmset", "-g", "therm"], ["pmset", "-g", "batt"],
    ]
    result = []
    for command in commands:
        p = subprocess.run(command, capture_output=True, text=True, timeout=10)
        result.append({"command": command, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
    return {"commands": result}


def native_check(args):
    out, compiled = args
    destination=external(out, os.environ["NATEMU_RAW_ROOT"])
    require(not destination.exists(), "result must not exist before launch")
    require(os.environ.get("NATEMU_F08_SOURCE_SHA256")==compiled, "compiled source differs from producing build")
    require(__import__("re").fullmatch(r"[0-9a-f]{32}",os.environ.get("NATEMU_F08_RUN_ID","")), "run identity missing")
    return 0


def prepare(directory, run_id):
    directory.mkdir(mode=0o700)
    try:
        snapshot=directory/"snapshot"
        sources=bundle()
        for name in sources:
            target=snapshot/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(EXPERIMENT/name,target)
        # Clock checker is a local build input, retained alongside the consumer.
        clock=REPO/"experiments/INV-01-07/c1/clock_evidence.py"
        shutil.copy2(clock,snapshot/"src/clock_evidence.py")
        sources["src/clock_evidence.py"]=file_sha(clock)
        require(all(file_sha(snapshot/name)==expected for name,expected in sources.items()), "snapshot changed during preparation")
        sdk=subprocess.check_output(["xcrun","--sdk","macosx","--show-sdk-path"],text=True,timeout=10).strip()
        command=["make","-C",str(snapshot),"BUILD="+str(directory/"build"),"REPO="+str(REPO),
                 "BUNDLE="+digest(sources),"CC=clang","CFLAGS=-O2 -Wall -Wextra -Werror -std=c11 -isysroot "+sdk,"all","disasm"]
        outcome="failed"
        try:
            with open(directory/"build.log","xb") as log:
                subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=60)
            outcome="built"
        finally:
            write_new(directory/"build-manifest.json",dict(schema="INV-01-08-build-v2",run_id=run_id,
                      source_sha256=digest(sources),sources=sources,command=command,outcome=outcome,
                      environment=environment(),artifacts={str(p.relative_to(directory)):file_sha(p)
                                for p in sorted(directory.rglob("*")) if p.is_file()}))
        return directory/"build-manifest.json"
    except BaseException as exc:
        if not (directory/"build-manifest.json").exists():
            write_new(directory/"build-manifest.json",dict(schema="INV-01-08-build-v2",run_id=run_id,
                      outcome="failed",error=dict(type=type(exc).__name__,message=str(exc)),
                      artifacts={str(p.relative_to(directory)):file_sha(p) for p in directory.rglob("*") if p.is_file()}))
        raise


def run_child(args):
    root=Path(os.environ["NATEMU_RAW_ROOT"]).resolve(strict=True)
    destination=external(args.directory,root)
    destination.mkdir(mode=0o700)
    args.created_directory=destination
    result=destination/"result.json"
    build_manifest=Path(args.build_manifest).resolve(strict=True)
    build_data=json_read(build_manifest)
    require(build_data["outcome"]=="built", "build incomplete")
    from evidence import verify_files
    verify_files(build_manifest.parent,build_data["artifacts"])
    binary=build_manifest.parent/"build/litmus"
    context=live_context(None,None)
    require(subprocess.check_output(["sysctl","-n","hw.optional.arm.FEAT_LSE"],text=True,timeout=10).strip()=="1", "required LSE feature unavailable")
    before = environment()
    signing = subprocess.run(["codesign", "-d", "--entitlements", "-", str(binary)], capture_output=True, text=True, timeout=10)
    before["signing"] = {"returncode": signing.returncode, "stdout": signing.stdout, "stderr": signing.stderr}
    write_new(destination / "environment-before.json", before)
    env=dict(os.environ,NATEMU_F08_SOURCE_SHA256=build_data["source_sha256"],
             NATEMU_F08_RUN_ID=build_data["run_id"],NATEMU_F08_REPO=str(REPO),NATEMU_RAW_ROOT=str(root))
    command=[str(binary),"--mode",args.mode,"--out",str(result)]
    write_new(destination/"invocation.json",dict(schema="INV-01-08-invocation-v2",run_id=build_data["run_id"],
              command=command,binary_sha256=file_sha(binary),build={"path":str(build_manifest),"sha256":file_sha(build_manifest)},
              context=context,started_utc_ns=time.time_ns()))
    receipt=supervise(command,destination/"stdout.txt",destination/"stderr.txt",MODES[args.mode],env)
    if result.exists():
        try: context["timebase"]=json_read(result)["timebase"]
        except (ValueError,KeyError): pass
    # Unexpected-fault emits no result; record the campaign's known timebase.
    if context["timebase"]=={"numer":None,"denom":None} and getattr(args,"timebase",None):
        context["timebase"]=args.timebase
    sys.path.insert(0,str(build_manifest.parent/"snapshot/src"))
    from clock_evidence import validate
    timing=validate(args.clock,context)
    receipt.update(schema="INV-01-08-supervisor-v2",mode=args.mode,run_id=build_data["run_id"],context=context,
                   timing=timing,result_sha256=file_sha(result) if result.exists() else None)
    write_new(destination/"result.supervisor.json",receipt)
    write_new(destination / "environment-after.json", environment())
    data = None
    if result.exists():
        try:
            data = json_read(result)
        except (ValueError, UnicodeError):
            pass  # preserve partial raw; it is never a completed sample
    if args.mode in ("fallback-drop-write", "fallback-lock-error", "unexpected-fault"):
        output = (destination / "stdout.txt").read_text(errors="replace")
        okay = control_oracle(args.mode, receipt, data, output)
        print(f"CONTROL {'PASS' if okay else 'FAIL'} ({args.mode}; no timing finding)")
        return 0 if okay else 1
    if receipt["timed_out"] or receipt["returncode"] != 0 or data is None:
        print("RUN INVALID: child failure/timeout/incomplete raw; no findings")
        return 1
    if args.mode == "fallback-only":
        # Standalone validation can exercise positive oracle but cannot be a
        # full normal result or bypass the instrument prerequisite.
        import copy
        tested = copy.deepcopy(data)
        tested.update(mode="normal", completed_normal=True, standalone=False)
        okay = not validate_fallback(tested)
        print(f"VALIDATION {'PASS' if okay else 'FAIL'} (standalone; no timing finding)")
        return 0 if okay else 1
    if args.mode == "negative":
        okay = sequential_oracle(data, receipt)
        print(f"CONTROL {'PASS' if okay else 'FAIL'} (sequential negative; no timing finding)")
        return 0 if okay else 1
    import check
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        integrity = check.main(str(result), data=data, integrity_only=True, historical=True)
    okay = not validate_fallback(data) and not validate_full_counts(data) and integrity == 0
    print(f"NORMAL RAW {'VALIDATED' if okay else 'INVALID'} (timing held until complete campaign controls)")
    return 0 if okay else 1


def run(args):
    import contextlib
    import io
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            code = run_child(args)
    except Exception as exc:
        output = io.StringIO(f"RUN INVALID ({type(exc).__name__}); no findings\n")
        code = 2
    destination = getattr(args, "created_directory", None)
    if destination:
        with open(destination / "check.txt", "x") as stream:
            stream.write(output.getvalue())
        write_new(destination / "manifest.json", {
            "schema": "INV-01-08-execution-manifest-v2", "mode": args.mode,
            "run_id": json_read(args.build_manifest)["run_id"],
            "classification_returncode": code,
            "artifacts": {str(p.relative_to(destination)): file_sha(p)
                          for p in sorted(destination.rglob("*")) if p.is_file()}})
    print(output.getvalue(), end="")
    return code


def campaign(args):
    parent=external(args.directory,os.environ["NATEMU_RAW_ROOT"])
    if not args.campaign_worker:
        campaign_start=time.monotonic_ns()
        require(not args.prepared_build, "a campaign must prepare its own build")
        parent.mkdir(mode=0o700)
        run_id=uuid.uuid4().hex
        write_new(parent/"schedule.json", dict(schema="INV-01-08-schedule-v3", run_id=run_id,
                  modes=list(CAMPAIGN_MODES), native_seconds=1325,
                  wall_seconds=CAMPAIGN_SECONDS, output_limit_bytes=CAMPAIGN_BYTES))
        command=[sys.executable,"-B",str(Path(__file__).resolve()),"--campaign",
                 "--directory",str(parent),"--campaign-worker"]
        if args.clock:
            command += ["--clock",str(args.clock)]
        receipt={"returncode":None,"timed_out":False,"output_limited":False}
        try:
            receipt=supervise(command,parent/"campaign.stdout.txt",parent/"campaign.stderr.txt",
                              CAMPAIGN_SECONDS,output_root=parent,max_bytes=CAMPAIGN_BYTES)
        except BaseException as exc:
            receipt["error"]={"type":type(exc).__name__,"message":str(exc)}
        okay=(receipt.get("returncode")==0 and not receipt.get("timed_out") and
              not receipt.get("output_limited") and not receipt.get("error"))
        states={}
        for mode in CAMPAIGN_MODES:
            member=parent/mode/"manifest.json"
            try:
                states[mode]="completed" if json_read(member)["classification_returncode"]==0 else "invalid"
            except (OSError,ValueError,KeyError):
                states[mode]="interrupted" if (parent/mode).exists() else "not-run"
        okay=okay and all(v=="completed" for v in states.values()) and (parent/"campaign.json").is_file()
        receipt.update(schema="INV-01-08-campaign-supervisor-v3",run_id=run_id,
                       status="completed" if okay else "failed",modes=states,
                       artifacts={str(p.relative_to(parent)):file_sha(p)
                                  for p in sorted(parent.rglob("*")) if p.is_file()})
        receipt["supervisor_elapsed_ns"]=time.monotonic_ns()-campaign_start
        receipt["output_bytes"]=output_bytes(parent)
        if receipt["supervisor_elapsed_ns"] >= CAMPAIGN_SECONDS*10**9:
            receipt["timed_out"]=True
        # Include final accounting in successful-campaign admission. Failure
        # diagnostics are retained even when the operational threshold was hit.
        if receipt["output_bytes"]+len(canonical(receipt))+1 >= CAMPAIGN_BYTES:
            receipt["output_limited"]=True
        if receipt["timed_out"] or receipt["output_limited"]:
            okay=False
            receipt["status"]="failed"
        write_new(parent/"campaign.supervisor.json",receipt)
        print("Campaign checks completed; findings review required." if okay else
              "Campaign failed; partial artifacts and not-run cells retained; no findings.")
        return 0 if okay else 1
    schedule=json_read(parent/"schedule.json")
    run_id=schedule["run_id"]
    if args.prepared_build:
        build=args.prepared_build.resolve(strict=True)
        require(json_read(build)["run_id"]==run_id,"prepared build identity differs")
    else:
        build=prepare(parent/"producing-build",run_id)
        os.environ["NATEMU_F08_REPO"]=str(REPO)
        command=[sys.executable,"-B",str(build.parent/"snapshot/src/supervisor.py"),
                 "--campaign","--campaign-worker","--directory",str(parent),
                 "--prepared-build",str(build)]
        if args.clock:
            command += ["--clock",str(args.clock)]
        os.execv(sys.executable,command)
    members={}; tb=None
    for mode in CAMPAIGN_MODES:
        child_args=argparse.Namespace(mode=mode,directory=str(parent/mode),build_manifest=build,clock=args.clock,timebase=tb)
        code=run(child_args)
        if code:
            write_new(parent/"campaign-failure.json",dict(schema="INV-01-08-campaign-failure-v3",run_id=run_id,mode=mode,returncode=code,timing_eligible=False))
            return 1
        receipt=json_read(parent/mode/"result.supervisor.json")
        tb=receipt["context"]["timebase"]
        manifest=parent/mode/"manifest.json"
        members[mode]={"path":str(manifest),"sha256":file_sha(manifest)}
    write_new(parent/"campaign.json",dict(schema="INV-01-08-campaign-v3",run_id=run_id,members=members))
    # Full packet/control validation runs inside the outer budget. This private
    # pre-finalization check cannot qualify a result: consumers require the
    # outer receipt, which only the waiting parent writes after this exits.
    from evidence import validate_campaign
    validate_campaign(parent/"normal/result.json",json_read(build),supervision_required=False)
    return 0


def cli(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "--native-check":
            require(len(argv) == 3, "native output/build check arguments")
            return native_check(argv[1:])
        if argv == ["--bundle"]:
            print(json.dumps({"files": bundle(), "sha256": digest(bundle())}, sort_keys=True, indent=2))
            return 0
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--clock", type=Path)
        parser.add_argument("--prepared-build", type=Path, help=argparse.SUPPRESS)
        parser.add_argument("--campaign-worker", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--directory", required=True)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--mode", choices=MODES)
        group.add_argument("--campaign", action="store_true")
        args = parser.parse_args(argv)
        if args.campaign:
            return campaign(args)
        destination=external(args.directory,os.environ["NATEMU_RAW_ROOT"])
        # Keep the selected result directory fresh; build is its fresh sibling.
        if not args.prepared_build:
            build=prepare(destination.with_name(destination.name+"-build"),uuid.uuid4().hex)
            os.environ["NATEMU_F08_REPO"]=str(REPO)
            os.execv(sys.executable,[sys.executable,"-B",str(build.parent/"snapshot/src/supervisor.py"),
                      *sys.argv[1:],"--prepared-build",str(build)])
        args.build_manifest=args.prepared_build
        return run(args)
    except Exception as exc:
        print(f"F08 refused/invalid ({type(exc).__name__}); no findings", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
