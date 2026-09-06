"""M06-M1 supervisor: retained build inputs and bounded child execution.

Pre-execution review remains a workflow prerequisite, outside this runner.
The first stage admits six original n=1 plain non-JIT direct/serial positives.
Other positive, hook and control obligations remain explicit and unexecuted.
"""
import argparse
import contextlib
import resource
import shutil
import plistlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import uuid
import subprocess
import sys
import time

from coverage import manifest, digest, canonical

HERE = Path(__file__).resolve().parent
CONTEXT = HERE / "source-context.json"
REPO = Path(json.loads(CONTEXT.read_text())["repository"]) if CONTEXT.exists() else HERE.parents[2]


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            result.update(chunk)
    return result.hexdigest()


def command(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=60).strip()


def raw_root():
    text = os.environ.get("NATEMU_RAW_ROOT")
    if not text:
        raise SystemExit("refusing to run: NATEMU_RAW_ROOT is not set")
    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise SystemExit("refusing to run: NATEMU_RAW_ROOT is not an existing directory")
    if path == REPO or REPO in path.parents:
        raise SystemExit("refusing to run: NATEMU_RAW_ROOT is inside the repository")
    return path


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def snapshot_sources(output):
    destination = output / "sources"
    destination.mkdir()
    for path in sorted(HERE.rglob("*")):
        if path.suffix in (".py", ".c"):
            if path.is_symlink():
                raise ValueError("source symlink")
            target = destination / path.relative_to(HERE)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(path.read_bytes())
    write_new(destination / "source-context.json", {"repository": str(REPO)})
    return destination


def file_manifest(directory):
    result = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_symlink():
            raise ValueError("artifact symlink")
        if path.is_file():
            if path.stat().st_size > 512 * 1024 * 1024:
                raise ValueError("artifact exceeds resource limit")
            result[str(path.relative_to(directory))] = sha(path)
    return result


def load_build(directory, root):
    directory = Path(directory)
    if (not directory.is_absolute() or directory.resolve(strict=True) != directory
            or directory == root or not directory.is_relative_to(root)):
        raise ValueError("build must be canonical external directory")
    value = json.loads((directory / "build-manifest.json").read_text())
    if (value.get("schema") != "INV-01-06-m1-build-v3" or value.get("runtime_executed") is not False
            or not isinstance(value.get("build_id"), str)):
        raise ValueError("historical or incomplete build format")
    if not {"state_server", "sources/src/state_server.c", "sources/run.py", "sources/check.py", "sources/coverage.py", "sources/build.py",
            "sources/source-context.json", "wire-layout.json", "state_server-hardened",
            "adhoc-plain-signature.txt", "adhoc-hardened-allow-jit-signature.txt"}.issubset(value["files"]):
        raise ValueError("incomplete retained build")
    for name, expected in value["files"].items():
        path = directory / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or path.resolve(strict=True) != path
                or not path.is_file() or sha(path) != expected):
            raise ValueError("retained build checksum mismatch")
    if set(value.get("signing",{}))!={"adhoc-plain","adhoc-hardened-allow-jit"}:
        raise ValueError("missing actual signing cells")
    for signing,record in value["signing"].items():
        if record["sha256"] != sha(directory/record["binary"]):
            raise ValueError("signed binary mismatch")
    return value, directory / "state_server"


def environment():
    result = dict(platform=platform.platform(), machine=platform.machine(),
                  captured_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    for key, args in dict(
            os_build=["sw_vers", "-buildVersion"], os_version=["sw_vers", "-productVersion"],
            model=["sysctl", "-n", "hw.model"], cpus=["sysctl", "-n", "hw.logicalcpu"],
            page_size=["sysctl", "-n", "hw.pagesize"],
            sdk=["xcrun", "--sdk", "macosx", "--show-sdk-version"],
            mig_version=["xcrun", "mig", "-version"],
            compiler=["xcrun", "clang", "--version"]).items():
        try:
            result[key] = command(args)
        except subprocess.CalledProcessError as exc:
            result[key] = dict(unavailable=True, output=exc.output)
    result["distribution_scope"] = "local signing capability, not Developer ID/notarization"
    return result


def jobs(plan):
    """One job per fresh-exec child. Positive cells: 3 children (seeds
    60601-60603) per eligible cell. Auxiliary arms: 3 children per arm."""
    rows = []
    for cell in plan["cells"]:
        if cell["status"] != "planned":
            rows.append(dict(cell_id=cell["cell_id"], cell=cell, kind="not-run",
                              reason=cell["invalid_reason"]))
            continue
        for seed in plan["positive_sample_plan"]["seeds"]:
            rows.append(dict(cell_id=cell["cell_id"], cell=cell, kind="positive", seed=seed,
                              warmup=plan["positive_sample_plan"]["warmup_events_per_worker"],
                              checked=plan["positive_sample_plan"]["checked_events_per_worker"]))
    for arm in plan["auxiliary_arms"]:
        for seed in plan["auxiliary_sample_plan"]["seeds"]:
            rows.append(dict(arm_id=arm["arm_id"], arm=arm, kind=arm["kind"], seed=seed))
    for hold in plan["holds"]:
        if hold["status"]=="NA": continue
        for repetition in range(hold["repetitions"]):
            cell=next(c for c in plan["cells"] if c["cell_id"]==hold["cell_id"])
            rows.append(dict(kind="hold",hold=hold,cell=cell,repetition=repetition,
                             seed=plan["positive_sample_plan"]["seeds"][repetition],
                             warmup=plan["positive_sample_plan"]["warmup_events_per_worker"],
                             checked=plan["positive_sample_plan"]["checked_events_per_worker"]))
    return rows


def native_args(binary, job):
    if job["kind"] == "not-run":
        return None
    result = [str(binary)]
    if job["kind"] in ("positive","hold"):
        c = job["cell"]
        result += ["--n", str(c["n"]), "--jit", str(c["jit"]), "--access", c["access"],
                   "--topology", c["topology"], "--path", c["path"],
                   "--warmup", str(job["warmup"]), "--checked", str(job["checked"]),
                   "--seed", str(job["seed"])]
        if job["kind"]=="hold":result += ["--hold",job["hold"]["hold"],"--hold_worker",str(job["hold"]["worker"])]
    elif job["kind"] == "negative":
        result += ["--n", "2", "--jit", "0", "--access", "load", "--topology", "perworker",
                   "--path", ("forward" if job["arm"]["arm"] in ("restoration","prior-server-failure") else "direct"), "--warmup", "0", "--checked", "1",
                   "--seed", str(job["seed"]), "--negative", job["arm"]["arm"]]
    else:  # control / terminal
        result += ["--n", "1", "--jit", "0", "--access", "load", "--topology", "serial",
                   "--path", ("forward" if job["arm"]["arm"] in ("restoration","prior-server-failure") else "direct"), "--warmup", "0", "--checked", "1",
                   "--seed", str(job["seed"]), "--arm", job["arm"]["arm"]]
    return result


CHILD_SECONDS=30
NO_PROGRESS_SECONDS=5
FILE_BYTES=64<<20
STAGE_BYTES=1<<30
FREE_RESERVE_BYTES=1<<30

class BudgetExceeded(RuntimeError): pass

@contextlib.contextmanager
def wall_budget(seconds):
    def expired(signum,frame): raise BudgetExceeded("stage wall deadline")
    prior=signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,seconds)
    try: yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,prior)

def baseline_job(job):
    return (job["kind"]=="positive" and all(job["cell"][k]==v for k,v in
            dict(n=1,jit=0,signing="adhoc-plain",topology="serial",path="direct").items()))

def run_child(binary, job, directory, identity):
    directory.mkdir(parents=True)
    write_new(directory / "job.json", job)
    write_new(directory / "identity.json", identity)
    argv = native_args(binary, job)
    if argv is None:
        write_new(directory / "check.json", dict(status="not-run", reason=job["reason"]))
        return dict(status="not-run", reason=job["reason"])
    write_new(directory / "invocation.json", dict(argv=argv, binary_sha256=sha(binary)))
    env = dict(os.environ, NATEMU_RAW_ROOT=str(directory))
    start = time.monotonic(); last_progress=start; prior_size=0
    with (directory / "trace.jsonl").open("xb") as out, (directory / "stderr.txt").open("xb") as err:
        def file_limit(): resource.setrlimit(resource.RLIMIT_FSIZE,(FILE_BYTES,FILE_BYTES))
        child = subprocess.Popen(argv, stdout=out, stderr=err, env=env, start_new_session=True,preexec_fn=file_limit)
        timed_out=False; stop_reason=None
        try:
            while child.poll() is None:
                now=time.monotonic()
                size=os.fstat(out.fileno()).st_size+os.fstat(err.fileno()).st_size
                if size!=prior_size: last_progress=now;prior_size=size
                if now-start>=CHILD_SECONDS or now-last_progress>=NO_PROGRESS_SECONDS:
                    timed_out=True;stop_reason="total timeout" if now-start>=CHILD_SECONDS else "no-progress timeout";break
                if shutil.disk_usage(directory).free<FREE_RESERVE_BYTES:
                    stop_reason="free-space reserve";break
                if sum(p.stat().st_size for p in directory.parent.rglob("*") if p.is_file())>STAGE_BYTES:
                    stop_reason="stage output cap";break
                try: child.wait(timeout=.05)
                except subprocess.TimeoutExpired: pass
            if child.poll() is None: os.killpg(child.pid,signal.SIGKILL)
            code=child.wait()
        except BaseException as exc:
            if child.poll() is None: os.killpg(child.pid,signal.SIGKILL)
            code=child.wait();stop_reason=type(exc).__name__+":"+str(exc)
        out.flush(); err.flush();os.fsync(out.fileno());os.fsync(err.fileno())
    status = dict(returncode=code, timed_out=timed_out, stop_reason=stop_reason,
                  watchdog_elapsed_seconds=time.monotonic() - start,
                  watchdog_is_latency_bound=False, no_progress_budget_seconds=NO_PROGRESS_SECONDS,
                  total_budget_seconds=CHILD_SECONDS, per_file_byte_cap=FILE_BYTES)
    write_new(directory / "exit.json", status)
    from check import two_pass_check, Invalid
    try:
        trace_path = directory / "trace.jsonl"
        if trace_path.stat().st_size > 512 * 1024 * 1024:
            raise Invalid("TRACE_RESOURCE_LIMIT")
        text = trace_path.read_text()
        if job["kind"] in ("negative","terminal","control"):
            from controls import check_control
            fails=check_control(text,job["arm"]["arm"],status)
        elif job["kind"]=="hold":
            from holds import check_hold
            fails=check_hold(text,job)
            if code!=0 or timed_out or stop_reason:fails.append("held child incomplete")
        else:
            fails = two_pass_check(text)
            if code != 0 or timed_out or stop_reason:
                fails.append("child process incomplete")
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        metadata = [row for row in rows if row.get("kind") == "meta"]
        expected = dict(zip((arg[2:] for arg in argv[1::2]), argv[2::2]))
        if len(metadata) != 1 or any(str(metadata[0].get(key)) != val for key, val in expected.items() if key not in ("hold","hold_worker")):
            fails.append("trace does not match scheduled child inputs")
        if job["kind"] in ("positive","hold"):
            summaries = [row for row in rows if row.get("kind") == "summary"]
            cell = job["cell"]
            if (len(summaries) != 1 or summaries[0].get("status") != "completed"
                    or summaries[0].get("events_checked") != cell["n"] * job["checked"]
                    or summaries[0].get("commits_total") != cell["n"] * (job["warmup"] + job["checked"] + int(cell["path"]=="forward"))):
                fails.append("incomplete positive event counts")
        result = dict(status="INVALID" if fails else "checked", fails=fails,
                      timing="unqualified: correctness-only method")
    except (Invalid, ValueError, TypeError, KeyError) as exc:
        result = dict(status="INVALID", invariant=str(exc))
    write_new(directory / "check.json", result)
    files = {p.name: sha(p) for p in directory.iterdir() if p.is_file()}
    write_new(directory / "manifest.json", dict(schema="INV-01-06-m1-child-v3", **identity, job_sha256=digest(job), files=files, result=result))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "run"))
    parser.add_argument("--build", type=Path, help="retained v2 build directory")
    parser.add_argument("--output", type=Path, help="new external campaign directory")
    parser.add_argument("--stage",choices=("baseline","controls","holds"),default="baseline")
    args = parser.parse_args()
    plan = manifest()
    if args.action == "plan":
        print(canonical(plan))
        return
    if not args.build or not args.output:
        parser.error("run needs --build and --output")
    root = raw_root()
    build, binary = load_build(args.build, root)
    retained_runner = args.build / "sources" / "run.py"
    if Path(__file__).resolve() != retained_runner:
        os.execv(sys.executable, [sys.executable, "-B", str(retained_runner), "run",
                                 "--build", str(args.build), "--output", str(args.output),"--stage",args.stage])
    campaign = args.output
    if (not campaign.is_absolute() or campaign.parent.resolve(strict=True) != campaign.parent
            or campaign == root or not campaign.is_relative_to(root) or campaign.is_relative_to(REPO)):
        raise ValueError("output must be a new canonical path below external NATEMU_RAW_ROOT")
    campaign.mkdir(exist_ok=False)
    identity = dict(run_id=uuid.uuid4().hex, build_id=build["build_id"])
    schedule = jobs(plan)
    def selected_job(job):
        if args.stage=="baseline":return baseline_job(job)
        if args.stage=="controls":return job["kind"] in ("negative","terminal","control")
        return (job["kind"]=="hold" and job["hold"]["worker"]==0 and all(job["cell"][k]==v for k,v in
                dict(n=2,jit=0,signing="adhoc-plain",access="load").items()))
    stage_count=sum(selected_job(job) for job in schedule)
    stage_seconds={"baseline":900,"controls":1800,"holds":3600}[args.stage]
    results = []
    error = None
    try:
        write_new(campaign / "coverage.json", plan)
        write_new(campaign / "build.json", dict(directory=str(args.build), **build))
        write_new(campaign / "environment.json", environment())
        write_new(campaign / "schedule.json", dict(schema="INV-01-06-m1-campaign-v2", **identity,
                  jobs=schedule, planned=len(schedule), child_ceiling_seconds=30))
        for name,signature in build["signing"].items():
            executable=args.build/signature["binary"]
            subprocess.run(["codesign","--verify","--strict",str(executable)],check=True,capture_output=True,timeout=10)
        write_new(campaign/"budget.json",dict(stage=args.stage,wall_seconds=stage_seconds,children=stage_count,
                  child_seconds=CHILD_SECONDS,no_progress_seconds=NO_PROGRESS_SECONDS,
                  file_bytes=FILE_BYTES,stage_bytes=STAGE_BYTES,free_reserve_bytes=FREE_RESERVE_BYTES))
        stopped=False
        with wall_budget(stage_seconds):
            for index, job in enumerate(schedule):
                if not selected_job(job) or stopped:
                    results.append(dict(index=index,status="not-run",reason="remaining method obligation; later stage preparation/review" if not stopped else "stage stopped after failure"))
                    continue
                repeated, repeated_binary = load_build(args.build, root)
                if repeated != build or repeated_binary != binary:
                    raise ValueError("producing build changed during campaign")
                signing=job["cell"]["signing"] if job["kind"] in ("positive","hold") else "adhoc-plain"
                selected=args.build/build["signing"][signing]["binary"]
                result = run_child(selected, job, campaign / f"{index:06d}", dict(index=index, **identity))
                results.append(dict(index=index, **result))
                if result["status"]!="checked": stopped=True
    except BaseException as exc:
        error = dict(type=type(exc).__name__, message=str(exc))
        write_new(campaign / "failure.json", error)
    accounted={r["index"] for r in results}
    results.extend(dict(index=i,status="not-run",reason="interrupted stage; inspect partial child") for i in range(len(schedule)) if i not in accounted)
    baseline_ok=sum(r["status"]=="checked" for r in results)==stage_count and error is None
    write_new(campaign / "results.json", dict(schema="INV-01-06-m1-campaign-v3", **identity,
              status=args.stage+"-complete-full-method-incomplete" if baseline_ok else args.stage+"-incomplete", results=results, planned=len(schedule), failure=error,
              timing="unqualified: correctness-only method",
              limitations=["full topology/signing/JIT/forwarding cells, holds and negative/control execution remain outstanding"]))
    write_new(campaign / "manifest.json", dict(schema="INV-01-06-m1-manifest-v2", **identity,
                                              files=file_manifest(campaign)))
    print(campaign)
    return 0 if baseline_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
