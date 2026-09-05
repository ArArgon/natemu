#!/usr/bin/env python3
"""F08-2 external exact-child watchdog and pre-execution review gate.

No invocation is authorized by this program's presence. An external envelope
binding actual accepted reviews is mandatory, including instrument evidence
for full runs. Validation controls never authorize timing findings.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from authorization import (EXPERIMENT, REPO, bundle, canonical, digest, external,
                           file_sha, json_read, live_context, load_authorization, require)
from fallback import validate_fallback, validate_full_counts

MODES = {"normal": 600, "negative": 600, "fallback-only": 60,
         "fallback-drop-write": 60, "fallback-lock-error": 60, "unexpected-fault": 5}


def supervise(command, stdout, stderr, timeout, env=None):
    """Only this exact child is killed/reaped. Files avoid pipe saturation."""
    start = time.monotonic_ns()
    with open(stdout, "xb") as out, open(stderr, "xb") as err:
        child = subprocess.Popen(command, stdout=out, stderr=err, env=env)
        timed_out = False
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.kill()
            code = child.wait()
        except BaseException:
            child.kill()
            child.wait()
            raise
    return {"returncode": code, "timed_out": timed_out,
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
    sdk = Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()).resolve(strict=True)
    return {"commands": result, "sdk_resolved": str(sdk), "header_sha256": {
        name: file_sha(sdk / name) for name in ("usr/include/mach/mach_time.h", "usr/include/mach/thread_policy.h",
                                               "usr/include/pthread/qos.h")}}


def native_authorize(args):
    auth_path, out, mode, numer, denom, revision, compiled_bundle = args
    root = os.environ["NATEMU_RAW_ROOT"]
    destination = external(out, root)
    require(not destination.exists(), "result must not exist before launch")
    a = load_authorization(auth_path, mode, int(numer), int(denom))
    require(a["code_revision"] == revision, "binary built from a different revision")
    # REV-210 R3: this is the actual compiled-source digest baked into the
    # binary at build time (Makefile's F08_COMPILED_BUNDLE), independently of
    # whatever environment variable it is about to be launched with. A
    # binary compiled from dirty/unreviewed source cannot pass this even if
    # the tree is later restored to the accepted, clean revision.
    require(a["code_bundle_sha256"] == compiled_bundle, "compiled source-bundle digest differs from authorization")
    require(os.environ.get("NATEMU_F08_AUTH_SHA256") == file_sha(auth_path) and
            os.environ.get("NATEMU_F08_BUNDLE_SHA256") == a["code_bundle_sha256"], "native provenance environment mismatch")
    return 0


def run_child(args):
    auth_path = Path(args.authorization).resolve(strict=True)
    # Inspect reviewed timebase first; native guard verifies this against the
    # actual mach_timebase_info response before it can measure or create raw.
    prelim = json_read(auth_path)
    if prelim.get("kind") == "evidence":
        tb = prelim["clock"]["packet"]["context"]["timebase"]
        numer, denom = tb["numer"], tb["denom"]
        a = load_authorization(auth_path, args.mode, numer, denom)
        context = live_context(numer, denom)
    else:
        a = load_authorization(auth_path, args.mode, live=False)
        context = None
    root = Path(os.environ["NATEMU_RAW_ROOT"]).resolve(strict=True)
    require(subprocess.check_output(["sysctl", "-n", "hw.optional.arm.FEAT_LSE"], text=True).strip() == "1",
            "required LSE host feature unavailable")
    destination = external(args.directory, root)
    destination.mkdir(mode=0o700)  # exclusive; never overwrite/reuse a run
    args.created_directory = destination
    result = destination / "result.json"
    build = destination / "build"
    build_command = ["make", "-C", str(EXPERIMENT), "BUILD=" + str(build), "CC=clang",
                     "CFLAGS=-O2 -Wall -Wextra -Werror -std=c11", "all", "disasm"]
    with open(destination / "build.log", "xb") as log:
        subprocess.run(build_command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
    binary = build / "litmus"
    before = environment()
    signing = subprocess.run(["codesign", "-d", "--entitlements", "-", str(binary)], capture_output=True, text=True, timeout=10)
    before["signing"] = {"returncode": signing.returncode, "stdout": signing.stdout, "stderr": signing.stderr}
    write_new(destination / "environment-before.json", before)
    env = dict(os.environ, NATEMU_F08_AUTHORIZATION=str(auth_path),
               NATEMU_F08_AUTH_SHA256=file_sha(auth_path), NATEMU_F08_BUNDLE_SHA256=a["code_bundle_sha256"],
               NATEMU_RAW_ROOT=str(root))
    command = [str(binary), "--mode", args.mode, "--out", str(result)]
    write_new(destination / "invocation.json", {
        "schema": "INV-01-08-invocation-v1", "command": command, "code_revision": a["code_revision"],
        "binary_sha256": file_sha(binary), "code_bundle": bundle(), "build_command": build_command,
        "authorization": {"path": str(auth_path), "sha256": file_sha(auth_path)},
        "context": context, "load_average_before": os.getloadavg(),
        "started_utc_ns": time.time_ns(), "prerequisite_validated_before_spawn": True})
    receipt = supervise(command, destination / "stdout.txt", destination / "stderr.txt", MODES[args.mode], env)
    receipt.update(schema="INV-01-08-supervisor-v1", mode=args.mode, context=context,
                   authorization={"path": str(auth_path), "sha256": file_sha(auth_path)},
                   authorization_validated_before_spawn=True, load_average_after=os.getloadavg(),
                   result_sha256=file_sha(result) if result.exists() else None,
                   stdout_sha256=file_sha(destination / "stdout.txt"), stderr_sha256=file_sha(destination / "stderr.txt"))
    write_new(destination / "result.supervisor.json", receipt)
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
            "schema": "INV-01-08-execution-manifest-v1", "mode": args.mode,
            "classification_returncode": code, "code_bundle": bundle(),
            "artifacts": {str(p.relative_to(destination)): file_sha(p)
                          for p in sorted(destination.rglob("*")) if p.is_file()}})
    print(output.getvalue(), end="")
    return code


def campaign(args):
    # All five runs are new and explicit. A failure stops promotion and is
    # retained; no automatic rerun, exclusion or successful-arm rescue.
    a = load_authorization(args.authorization, "normal", live=False)
    require(a["kind"] == "evidence", "campaign requires prior clock evidence")
    parent = external(args.directory, os.environ["NATEMU_RAW_ROOT"])
    parent.mkdir(mode=0o700)
    modes = ("normal", "negative", "fallback-drop-write", "fallback-lock-error", "unexpected-fault")
    members = {}
    for mode in modes:
        child_args = argparse.Namespace(authorization=args.authorization, mode=mode, directory=str(parent/mode))
        code = run(child_args)
        if code:
            write_new(parent / "campaign-failure.json", {"mode": mode, "returncode": code, "timing_eligible": False})
            return 1
        manifest = parent/mode/"manifest.json"
        members[mode] = {"path": str(manifest), "sha256": file_sha(manifest)}
    write_new(parent / "campaign.json", {"schema": "INV-01-08-campaign-v1", "members": members})
    import check
    import contextlib
    import io
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = check.cli([str(parent/"normal/result.json")])
    with open(parent / "campaign-check.txt", "x") as stream:
        stream.write(output.getvalue())
    print(output.getvalue(), end="")
    return code


def cli(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "--native-authorize":
            require(len(argv) == 8, "native authorization arguments")
            return native_authorize(argv[1:])
        if argv == ["--bundle"]:
            print(json.dumps({"files": bundle(), "sha256": digest(bundle())}, sort_keys=True, indent=2))
            return 0
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--authorization", required=True)
        parser.add_argument("--directory", required=True)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--mode", choices=MODES)
        group.add_argument("--campaign", action="store_true")
        args = parser.parse_args(argv)
        return campaign(args) if args.campaign else run(args)
    except Exception as exc:
        print(f"F08 refused/invalid ({type(exc).__name__}); no findings", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
