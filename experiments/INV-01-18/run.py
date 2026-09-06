"""External INV18 runner; reviews remain a human workflow, not runtime inputs."""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import random
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import socket
import uuid

from coverage import manifest, digest, canonical, NEGATIVES, SENSITIVITY
from check import check, Invalid

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("INV18_ORIGINAL_REPO", HERE.parents[1])).resolve()

# Other complete stages retain their coverage but need their own reviewed
# aggregate budgets before execution. Do not silently select fewer cells.
STAGE_BUDGETS = {"preflight": dict(wall_seconds=900, output_bytes=2 << 30,
                                 child_file_bytes=128 << 20, free_reserve_bytes=1 << 30),
                 "preflight-remaining": dict(wall_seconds=900, output_bytes=2 << 30,
                                 child_file_bytes=128 << 20, free_reserve_bytes=1 << 30),
                 "negative": dict(wall_seconds=1800, output_bytes=2 << 30,
                                 child_file_bytes=128 << 20, free_reserve_bytes=1 << 30)}


class BudgetExceeded(RuntimeError):
    pass


def output_bytes(directory):
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def check_budget(directory, limits):
    if output_bytes(directory) > limits["output_bytes"]:
        raise BudgetExceeded("campaign output limit")
    if shutil.disk_usage(directory).free < limits["free_reserve_bytes"]:
        raise BudgetExceeded("free disk reserve")


@contextlib.contextmanager
def wall_budget(seconds):
    def expired(signum, frame):
        raise BudgetExceeded("campaign wall deadline")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def wait_child(child, directory, campaign, limits):
    """Watch only the owned process group; retain failed/partial output."""
    deadline = time.monotonic() + 30
    try:
        while True:
            check_budget(campaign, limits)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.killpg(child.pid, signal.SIGKILL)
                return child.wait(), True
            try:
                return child.wait(timeout=min(.1, remaining)), False
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        raise


def sha(path):
    result=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda:stream.read(1<<20),b""):
            result.update(chunk)
    return result.hexdigest()


def command(args):
    return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,cwd=REPO,timeout=10).strip()


def raw_root():
    text=os.environ.get("NATEMU_RAW_ROOT")
    if not text:
        raise SystemExit("NATEMU_RAW_ROOT is required; no default or repository output")
    path=Path(text).expanduser().resolve()
    if path==REPO or REPO in path.parents or path in REPO.parents:
        raise SystemExit("repository-internal raw root rejected")
    path.mkdir(parents=True,exist_ok=True)
    return path


def write_new(path,value):
    with Path(path).open("x") as stream:
        stream.write(canonical(value)+"\n")
        stream.flush()
        os.fsync(stream.fileno())


def packet(directory=HERE):
    files={str(path.relative_to(directory)):sha(path) for path in sorted(directory.rglob("*"))
           if path.is_file() and "__pycache__" not in path.parts and (path.suffix in (".py", ".c", ".h", ".plist") or path.name=="Makefile")}
    return dict(files=files,sha256=digest(files))


def file_manifest(directory):
    return {str(path.relative_to(directory)):sha(path) for path in sorted(directory.rglob("*"))
            if path.is_file() and path!=directory/"manifest.json"}


def verified_campaign(directory,sources):
    directory=Path(directory).resolve()
    saved=json.loads((directory/"manifest.json").read_text())
    if saved.get("schema")!="INV-01-18-campaign-v3":
        raise SystemExit("historical campaign requires its original reader")
    if saved["files"]!=file_manifest(directory):
        raise SystemExit("campaign artifact changed: "+str(directory))
    if json.loads((directory/"code-packet.json").read_text())!=sources:
        raise SystemExit("campaign belongs to a different code packet")
    schedule=json.loads((directory/"schedule.json").read_text())["jobs"]
    results=json.loads((directory/"results.json").read_text())
    mode=results["evidence_mode"]
    budget=json.loads((directory/"budget.json").read_text())
    if (budget.get("schema")!="INV-01-18-budget-v1" or budget.get("limits")!=STAGE_BUDGETS.get(mode)
            or budget.get("run_id")!=saved.get("run_id") or budget.get("mode")!=mode):
        raise SystemExit("missing or mismatched stage resource budget")
    expected=sorted(digest(job) for job in jobs(manifest(),mode))
    if sorted(digest(job) for job in schedule)!=expected:
        raise SystemExit("campaign schedule is not the full frozen stage")
    if mode=="preflight-remaining" and schedule!=list(jobs(manifest(),mode)):
        raise SystemExit("remaining preflight must retain original suffix order")
    if len(results["results"])!=len(schedule) or {row["index"] for row in results["results"]}!=set(range(len(schedule))):
        raise SystemExit("campaign has missing or duplicate results")
    record=json.loads((directory/"run.json").read_text())
    if record.get("schema")!="INV-01-18-run-v2" or record.get("run_id")!=saved.get("run_id"):
        raise SystemExit("mixed campaign identity")
    build_dir=Path(record["build_directory"])
    if sha(build_dir/"build-manifest.json")!=record["build_manifest_sha256"]:
        raise SystemExit("campaign producing build changed")
    build=json.loads((build_dir/"build-manifest.json").read_text())
    if build.get("schema")!="INV-01-18-build-v2" or build.get("outcome")!="built" or build.get("code_packet")!=sources:
        raise SystemExit("campaign requires its complete v2 producing build")
    for name,expected in build["files"].items():
        target=(build_dir/name).resolve()
        if build_dir not in target.parents or sha(target)!=expected:
            raise SystemExit("campaign build artifact mismatch")
    for result in results["results"]:
        if result["status"]=="not-run": continue
        child=directory/f"{result['index']:06d}"
        job=schedule[result["index"]]
        inv=json.loads((child/"invocation.json").read_text())
        retained=json.loads((child/"manifest.json").read_text())
        with (child/"trace.jsonl").open() as trace:
            header=json.loads(next(trace))
        if header.get("schema")!="INV-01-18-trace-v2" or header.get("run_id")!=record["run_id"] or header.get("build_id")!=build["run_id"]:
            raise SystemExit("mixed raw producing identity")
        if (inv.get("schema")!="INV-01-18-invocation-v2" or retained.get("schema")!="INV-01-18-child-v2" or
            inv.get("run_id")!=record["run_id"] or retained.get("run_id")!=record["run_id"] or
            inv.get("binary_sha256")!=build["files"]["protocol-"+job["signing"]] or
            inv.get("argv")!=native_args(build_dir/("protocol-"+job["signing"]),job) or
            retained.get("job_sha256")!=digest(job) or
            retained.get("result")!={key:value for key,value in result.items() if key!="index"}):
            raise SystemExit("mixed child run/build/job/result")
    return directory,schedule,results["results"],mode


def qualification(campaigns,sources):
    """Derive per-cell eligibility, never turn unavailable controls into passes."""
    required={"preflight","hooks","negative","protection"}
    plan=manifest()
    stages={}; references=[]; identity=None; build_digest=None
    for path in campaigns:
        directory,schedule,results,mode=verified_campaign(path,sources)
        if mode in stages:
            raise SystemExit("one immutable campaign per stage; no outcome-driven replacement")
        stages[mode]=(schedule,results)
        host=json.loads((directory/"environment.json").read_text())
        current={key:host[key] for key in ("host_id","model","os_build","sdk")}
        run_record=json.loads((directory/"run.json").read_text())
        if identity is not None and (identity!=current or build_digest!=run_record["build_manifest_sha256"]):
            raise SystemExit("qualification campaigns differ in host/build")
        identity=current; build_digest=run_record["build_manifest_sha256"]
        references.append(dict(directory=str(directory),manifest_sha256=sha(directory/"manifest.json"),mode=mode))
    if not required<=stages.keys():
        raise SystemExit("qualification needs full preflight/hooks/negative/protection campaigns")
    blocked={cell["cell_id"]:[] for cell in plan["cells"]}
    for mode,(schedule,results) in stages.items():
        for result in results:
            job=schedule[result["index"]]
            good=(result.get("negative_detected") is True if mode=="negative" else
                  result["status"]==("control-qualified" if mode=="protection" else "correctness-qualified"))
            if good: continue
            affected=blocked if mode=="negative" else (
                [c["cell_id"] for c in plan["cells"] if c["arm"]==job["arm"]]
                if mode=="preflight" else [job["cell"]])
            for cell in affected:
                blocked[cell].append(dict(stage=mode,index=result["index"],status=result["status"]))
    return dict(schema="INV-01-18-qualification-v2",kind="control-qualification",code_packet=sources,host=identity,
                build_manifest_sha256=build_digest,campaigns=references,
                eligible_cells=[cell for cell,reasons in blocked.items() if not reasons],
                blocked_cells={cell:reasons for cell,reasons in blocked.items() if reasons},
                correctness_stage_included="correctness" in stages,evidence=False,completed_question=False)


def qualify(campaigns,output,sources):
    result=qualification(campaigns,sources)
    root=raw_root(); output=Path(output).resolve()
    if root not in output.parents:
        raise SystemExit("qualification must be a new external raw artifact")
    write_new(output,result)
    print(output)


def environment():
    import resource
    result=dict(host_id=socket.gethostname(),platform=platform.platform(),machine=platform.machine(),
                captured_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    for key,args in dict(os_build=["sw_vers","-buildVersion"],os_version=["sw_vers","-productVersion"],
                         model=["sysctl","-n","hw.model"],cpus=["sysctl","-n","hw.logicalcpu"],
                         physical=["sysctl","-n","hw.physicalcpu"],memory=["sysctl","-n","hw.memsize"],
                         page_size=["sysctl","-n","hw.pagesize"],load=["uptime"],
                         power=["pmset","-g","batt"],thermal=["pmset","-g","therm"],
                         sdk=["xcrun","--sdk","macosx","--show-sdk-version"],
                         compiler=["xcrun","clang","--version"]).items():
        try:
            result[key]=command(args)
        except subprocess.CalledProcessError as exc:
            result[key]=dict(unavailable=True,output=exc.output)
    result["placement"]="uncontrolled"
    result["distribution_scope"]="local signing capability, not Developer ID/notarization"
    result["process_limits"]={name:list(resource.getrlimit(getattr(resource,name))) for name in ("RLIMIT_NOFILE","RLIMIT_NPROC","RLIMIT_STACK","RLIMIT_AS")}
    result["sandbox_state"]="App Sandbox entitlement checked per binary; inherited launcher restrictions not independently measured"
    return result


def signatures(directory):
    captured={}
    for signing in ("A","B","C"):
        binary=directory/("protocol-"+signing)
        subprocess.run(["codesign","--verify","--strict",str(binary)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        details=subprocess.run(["codesign","-d","--verbose=4","--entitlements",":-",str(binary)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        raw=details.stdout+details.stderr
        start=raw.find(b"<?xml"); end=raw.find(b"</plist>")
        entitlements=plistlib.loads(raw[start:end+8]) if start>=0 and end>=start else {}
        expected={} if signing=="A" else plistlib.loads((HERE/("entitlements-"+signing+".plist")).read_bytes())
        runtime=b"(runtime)" in raw or b",runtime)" in raw
        if entitlements!=expected or runtime!=(signing!="A"):
            raise SystemExit("actual signature/entitlement cell mismatch: "+signing)
        captured[signing]=dict(binary_sha256=sha(binary),entitlements=entitlements,hardened_runtime=runtime,
                               raw=raw.decode(errors="replace"),app_sandbox_entitled=bool(entitlements.get("com.apple.security.app-sandbox")))
    return captured


def jobs(plan,mode):
    rows={row["cell_id"]:row for row in plan["cells"]}
    if mode=="hooks":
        for spec in plan["hooks"]:
            if spec["status"]=="NA":
                continue
            for repetition in range(3):
                cell=rows[spec["cell_id"]]
                yield dict(cell,cell=cell["cell_id"],mode="preflight",kind="hook",seed=18001+repetition,
                           hook=spec["hook"],held=spec["actor"],repetition=repetition)
    elif mode in ("correctness","cost"):
        seeds=plan[mode]["seeds"]
        for cell in plan["cells"]:
            for seed in seeds:
                for baseline in (("","data-indirection","handshake") if mode=="cost" else ("",)):
                    yield dict(cell,cell=cell["cell_id"],mode=mode,kind=mode,seed=seed,baseline=baseline)
    elif mode=="preflight":
        for arm in ("I","S","E","V","M-data-map","M-data-protect","M-jit-protect"):
            cell=next(row for row in plan["cells"] if row["arm"]==arm and row["n"]==1 and row["rate"]==1)
            yield dict(cell,cell=cell["cell_id"],mode="preflight",kind="preflight",seed=18001)
    elif mode=="preflight-remaining":
        # Original seed18001 schedule suffix after the retained unavailable
        # MAP_JIT protection attempt. This is not a complete preflight stage.
        original={job["arm"]:job for job in jobs(plan,"preflight")}
        for arm in ("M-data-protect","M-data-map","V","I"):
            yield original[arm]
    elif mode=="sensitivity":
        for cell in plan["cells"]:
            for negative in SENSITIVITY:
                yield dict(cell,cell=cell["cell_id"],mode="sensitivity",kind="sensitivity",seed=18001,negative=negative)
    elif mode=="negative":
        cell=next(row for row in plan["cells"] if row["arm"]=="I" and row["n"]==2 and row["rate"]==1 and row["size"]==256 and row["producers"]==2)
        for negative in NEGATIVES:
            for repetition in range(3):
                selected=cell
                extra={}
                if negative=="early-retire":
                    selected=next(row for row in plan["cells"] if row["arm"]=="I" and row["n"]==2 and row["rate"]==64 and row["size"]==256 and row["producers"]==2)
                    extra=dict(hook="reader-in-call",held=0)
                yield dict(selected,cell=selected["cell_id"],mode="preflight",kind="negative",seed=18001+repetition,negative=negative,**extra)
    elif mode=="protection":
        for cell in plan["cells"]:
            if not cell["arm"].startswith("M-"):
                continue
            for control in ("forbidden","neighbor","restored"):
                for repetition in range(3):
                    yield dict(cell,cell=cell["cell_id"],mode="preflight",kind="protection",seed=18001+repetition,
                               negative="protection-"+control)
    else:
        raise ValueError(mode)


def native_args(binary,job):
    result=[str(binary)]
    keys=("arm","n","producers","size","rate","pages","seed","held","mode","qos","layout",
          "pressure","hook","negative","cell","signing","baseline")
    for key in keys:
        if key in job:
            result.extend(("--"+key,str(job[key])))
    return result


def byte_estimate(schedule):
    """Planning estimate only: actual extra dispatcher calls are not bounded.

    Uses 256 bytes per fixed-width trace record, 24 transition records per
    publication and the sample-plan minimum call count. All four possible
    calibrated batch sizes are shown; no favorable size is preselected.
    """
    totals={str(size):0 for size in (1,16,256,4096)}
    for job in schedule:
        for size in (1,16,256,4096):
            diagnostic=job["kind"] in ("hook","preflight","negative","sensitivity","protection")
            rounds=36 if diagnostic else 1024+(1000*size if job["mode"]=="cost" else 4096)
            publications=rounds*(job["n"] if job["arm"]=="V" else 1)
            call_floor=(128 if diagnostic else 16384)*job["n"]
            if job["mode"]=="cost": call_floor=max(call_floor,rounds*job["n"])
            setup=512*(24+3*job["n"]) if job["pressure"]!="normal" else 0
            totals[str(size)]+=256*(24*publications+3*call_floor+setup+1024)
    return dict(estimated_bytes_by_possible_cost_batch=totals,record_bytes_assumption=256,
                transition_records_per_publication_assumption=24,
                extra_dispatcher_calls_unbounded_by_this_estimate=True,
                estimate_is_neither_disk_budget_nor_performance_result=True)


def protection_result(path,job,status):
    from check import records,pair
    rows=list(records(path))
    def require(ok):
        if not ok:
            raise Invalid("PROTECTION_WITNESS")
    require(rows and all(rows[0].get(key)==job[key] for key in ("cell","negative","signing","n","seed","size","mode")))
    header=rows[0]
    arms=[row for row in rows if row["kind"]=="protection-arm"]
    done=[row for row in rows if row["kind"]=="protection-complete"]
    require(len(arms)==1 and arms[0]["control"]==job["negative"] and arms[0]["length"]>0)
    arm=arms[0]; jit=job["arm"]=="M-jit-protect"
    length=(16<<20) if jit else job["pages"]*header["page_size"]
    require(arm["jit"]==jit and arm["length"]==length and arm["owned_length"]==(length if jit else 2*length))
    require(arm["address"]%header["page_size"]==0)
    api=[row for row in rows if row["kind"]=="protection-api"]
    phases=["protect","restore"] if job["negative"]=="protection-restored" else ["protect"]
    require([row["phase"] for row in api]==phases)
    require(all(row["address"]==arm["address"] and row["length"]==length and row["result"]==0 and row["errno"]==0 for row in api))
    require(rows.index(api[-1])<rows.index(arm))
    accesses=[row for row in rows if row["kind"]=="protection-access"]
    if job["negative"]=="protection-neighbor":
        require([row["phase"] for row in accesses]==["before","after"])
        before,after=accesses
        require(rows.index(before)<rows.index(api[0])<rows.index(arm)<rows.index(after))
        require(before["value"]==after["value"]==0x1864 and before["function"]==after["function"] and before["data"]==after["data"])
        if jit:
            require(arm["accessible_control"]=="unrelated-AOT-and-data-not-adjacent")
            require(all(not arm["address"]<=before[key]<arm["address"]+length for key in ("function","data")))
        else:
            require(arm["accessible_control"]=="adjacent-owned-data-page" and before["data"]==arm["address"]+length)
    else: require(not accesses)
    returned=[row for row in rows if row["kind"]=="protection-jit-return"]
    if jit and job["negative"]=="protection-restored":
        callbacks=[row for row in rows if row["kind"]=="protection-restore-callback"]
        require(len(callbacks)==1 and callbacks[0]["result"]==0 and rows.index(api[-1])<rows.index(callbacks[0])<rows.index(arm))
        require(len(returned)==1 and (returned[0]["x"],returned[0]["y"])==pair(1,job["seed"]))
    else: require(not returned)
    if job["negative"]=="protection-forbidden":
        require(not done and status["signal"] in (signal.SIGBUS,signal.SIGSEGV) and not status["timed_out"])
    else:
        require(status["returncode"]==0 and len(done)==1 and done[0]["value"]==0x1864)
    return dict(status="control-qualified",runtime_control=True,evidence=False)


def mprotect_failure_witness(stderr,job,page_size):
    lines=[line.removeprefix("INV18 MPROTECT_RESULT ") for line in stderr.splitlines()
           if line.startswith("INV18 MPROTECT_RESULT ")]
    if len(lines)!=1:
        raise Invalid("MPROTECT_FAILURE_WITNESS")
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise Invalid("MPROTECT_FAILURE_WITNESS")
            result[key]=value
        return result
    try: record=json.loads(lines[0],object_pairs_hook=unique)
    except ValueError: raise Invalid("MPROTECT_FAILURE_WITNESS")
    fields={"schema","address","length","protect_protection","restore_protection","protect_result",
            "protect_errno","restore_attempted","restore_result","restore_errno"}
    if (type(record) is not dict or set(record)!=fields
            or record["schema"]!="INV-01-18-mprotect-failure-v1"):
        raise Invalid("MPROTECT_FAILURE_WITNESS")
    integer_fields=fields-{"schema","restore_attempted","restore_result","restore_errno"}
    if any(type(record[key]) is not int for key in integer_fields):
        raise Invalid("MPROTECT_FAILURE_WITNESS")
    if (not 0<record["address"]<1<<64 or not 0<record["length"]<=16<<20
            or record["protect_protection"]!=0 or record["restore_protection"] not in (1,7)):
        raise Invalid("MPROTECT_FAILURE_WITNESS")
    if (job.get("arm") not in ("M-jit-protect","M-data-protect")
            or type(page_size) is not int or page_size<=0):
        raise Invalid("MPROTECT_FAILURE_CONTEXT")
    jit=job["arm"]=="M-jit-protect"
    if (record["restore_protection"]!=(7 if jit else 1)
            or record["length"]!=((16<<20) if jit else job["pages"]*page_size)
            or record["address"]%page_size):
        raise Invalid("MPROTECT_FAILURE_CONTEXT")
    if record["protect_result"]==-1:
        okay=(record["protect_errno"]>0 and record["restore_attempted"] is False
              and record["restore_result"] is None and record["restore_errno"] is None)
    else:
        okay=(record["protect_result"]==0 and record["protect_errno"]==0
              and record["restore_attempted"] is True and type(record["restore_result"]) is int
              and record["restore_result"]==-1 and type(record["restore_errno"]) is int
              and record["restore_errno"]>0)
    if not okay: raise Invalid("MPROTECT_FAILURE_WITNESS")
    return record


def run_child(binary,job,directory,timer_gate=None,run_id=None,build_id=None,limits=None):
    if limits is None:
        raise BudgetExceeded("reviewed stage resource budget required")
    directory.mkdir()
    write_new(directory/"job.json",job)
    env=dict(os.environ,NATEMU_RAW_ROOT=str(directory),INV18_REPO_ROOT=str(REPO),INV18_RUN_ID=run_id or "")
    argv=native_args(binary,job)
    write_new(directory/"invocation.json",dict(schema="INV-01-18-invocation-v2",run_id=run_id,argv=argv,binary_sha256=sha(binary)))
    start=time.monotonic()
    with (directory/"trace.jsonl").open("xb") as output,(directory/"stderr.txt").open("xb") as error:
        def file_limit():
            resource.setrlimit(resource.RLIMIT_FSIZE,(limits["child_file_bytes"],limits["child_file_bytes"]))
        child=subprocess.Popen(argv,stdout=output,stderr=error,env=env,start_new_session=True,preexec_fn=file_limit)
        timed_out=False
        budget_error=None
        try:
            code,timed_out=wait_child(child,directory,directory.parent,limits)
        except BudgetExceeded as exc:
            budget_error=str(exc)
            code=child.wait()
        except BaseException:
            try: os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait()
            raise
        output.flush(); error.flush()
        os.fsync(output.fileno()); os.fsync(error.fileno())
    if code == -signal.SIGXFSZ:
        budget_error="native output file limit"
    status=dict(returncode=code,signal=-code if code<0 else None,timed_out=timed_out,
                budget_failure=budget_error,
                watchdog_elapsed_seconds=time.monotonic()-start,watchdog_is_latency_bound=False)
    write_new(directory/"exit.json",status)
    if budget_error:
        raise BudgetExceeded(budget_error)
    try:
        result=protection_result(directory/"trace.jsonl",job,status) if job["kind"]=="protection" else check(directory/"trace.jsonl",job)
    except Invalid as exc:
        result=dict(status="INVALID",invariant=str(exc))
    producer_identity_valid=False
    try:
        with (directory/"trace.jsonl").open() as stream:
            header=json.loads(next(stream))
        if not isinstance(header,dict) or header.get("schema")!="INV-01-18-trace-v2" or header.get("run_id")!=run_id or header.get("build_id")!=build_id:
            result=dict(status="INVALID",invariant="PRODUCER_IDENTITY")
        else:
            producer_identity_valid=True
    except (ValueError,StopIteration):
        result=dict(status="INVALID",invariant="PRODUCER_IDENTITY")
    if timed_out:
        result=dict(status="timed-out",last_checker_result=result)
    elif code==78 and producer_identity_valid:
        result=dict(status="unavailable",last_checker_result=result)
        stderr=(directory/"stderr.txt").read_text()
        if "INV18 UNAVAILABLE mprotect roundtrip" in stderr:
            try:
                result["mprotect_failure"]=mprotect_failure_witness(stderr,job,header.get("page_size"))
            except Invalid as exc:
                result=dict(status="INVALID",invariant=str(exc))
    elif code not in (0,79) and result["status"]!="control-qualified":
        result=dict(status="INVALID",exit=status,last_checker_result=result)
    if job["kind"]=="negative":
        result["negative_detected"]=negative_result(job,status,result,(directory/"stderr.txt").read_text())
    from clock_evidence import validate
    context=dict(host_id=socket.gethostname(),host_model=command(["sysctl","-n","hw.model"]),
                 os_build=command(["sw_vers","-buildVersion"]),architecture=platform.machine())
    try:
        with (directory/"trace.jsonl").open() as trace:
            header=json.loads(next(trace))
        context["timebase"]={"numer":header["numer"],"denom":header["denom"]}
    except (ValueError,KeyError,StopIteration):
        context["timebase"]=None
    timing=validate(timer_gate,context)
    result["timing_qualification"]=timing
    if "cost" in result and (timing["status"]!="context-matched" or result.get("status")!="correctness-qualified"):
        result["cost"]={"status":"unqualified","reason":"clock context or correctness unavailable",
                        "local_diagnostic":result["cost"]}
    write_new(directory/"check.json",result)
    files={path.name:sha(path) for path in directory.iterdir() if path.is_file()}
    write_new(directory/"manifest.json",dict(schema="INV-01-18-child-v2",run_id=run_id,job_sha256=digest(job),files=files,result=result))
    return result


def negative_result(job,status,result,stderr):
    """An unrelated timeout/crash never supplies the named negative witness."""
    name=job["negative"]
    if name.startswith("trace-"):
        marker="INV18 TRACE_NEGATIVE "+name+" "
        expected="TRACE_JSON:" if name=="trace-truncated" else "TRACE_SEQUENCE"
        return status["returncode"]==0 and marker in stderr and result.get("invariant","").startswith(expected)
    code=NEGATIVES.index(name)+1
    marker=re.search(rf"^INV18 NEGATIVE {code} token=(\d+) detail=(\d+)$",stderr,re.M)
    if not marker or int(marker[1])==0:
        return False
    if name in ("lost-ack","stale-ack"):
        return status["timed_out"] and status["signal"]==signal.SIGKILL
    causes={"wrong-generation":"returned pair", "corrupt-return":"returned pair",
            "stale-translation":"negative stale/unowned install rejected",
            "unowned-install":"negative stale/unowned install rejected",
            "double-grant":"negative second grant detected",
            "early-retire":"negative early retirement detected before reuse",
            "aba":"negative reused address has a different token"}
    return (status["returncode"]==70 and not status["timed_out"] and
            "INV18 invariant: "+causes[name]+"\n" in stderr)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("plan","run","qualify"))
    parser.add_argument("--mode",choices=("preflight","preflight-remaining","hooks","negative","protection","sensitivity","correctness","cost"))
    parser.add_argument("--binary-dir",type=Path)
    parser.add_argument("--timer-packet",type=Path)
    parser.add_argument("--qualification",type=Path)
    parser.add_argument("--campaign",type=Path,action="append",default=[])
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    plan=manifest()
    sources=packet()
    if args.action=="plan":
        print(canonical(dict(plan=plan,code_packet=sources)))
        return
    if args.action=="qualify":
        if not args.campaign or not args.output:
            parser.error("qualify needs campaign paths and a new output artifact")
        record=json.loads((args.campaign[0]/"run.json").read_text())
        retained=Path(record["build_directory"])/"snapshot/run.py"
        if HERE!=retained.parent:
            os.environ["INV18_ORIGINAL_REPO"]=str(REPO)
            os.execv(sys.executable,[sys.executable,"-B",str(retained),*sys.argv[1:]])
        qualify(args.campaign,args.output,sources)
        return
    if not args.mode or not args.binary_dir:
        parser.error("run needs mode and binary-dir")
    directory=args.binary_dir.resolve()
    root=raw_root()
    if root not in directory.parents:
        raise SystemExit("build must be retained under NATEMU_RAW_ROOT")
    build_manifest=directory/"build-manifest.json"
    build=json.loads(build_manifest.read_text())
    if build.get("schema")!="INV-01-18-build-v2" or build.get("outcome")!="built":
        raise SystemExit("complete v2 snapshot build required; use original readers for historical builds")
    for name,expected in build["files"].items():
        target=(directory/name).resolve()
        if directory not in target.parents or sha(target)!=expected:
            raise SystemExit("build artifact mismatch")
    if packet(directory/"snapshot")!=build["code_packet"]:
        raise SystemExit("retained source snapshot mismatch")
    # Use the retained runner/checker/coverage, independent of later repository edits.
    retained=directory/"snapshot/run.py"
    if HERE!=retained.parent:
        os.environ["INV18_ORIGINAL_REPO"]=str(REPO)
        os.execv(sys.executable,[sys.executable,"-B",str(retained),*sys.argv[1:]])
    sources=build["code_packet"]
    host=environment()
    signed=signatures(args.binary_dir.resolve())
    eligibility=None
    if args.mode in ("correctness","cost"):
        if not args.qualification:
            parser.error("stress/cost needs the exact-build control qualification packet")
        eligibility=json.loads(args.qualification.read_text())
        if (eligibility.get("schema")!="INV-01-18-qualification-v2" or eligibility.get("code_packet")!=sources or eligibility.get("build_manifest_sha256")!=sha(build_manifest) or
            eligibility.get("host")!={key:host[key] for key in ("host_id","model","os_build","sdk")}):
            raise SystemExit("qualification source/build/host mismatch")
        expected=qualification([Path(ref["directory"]) for ref in eligibility["campaigns"]],sources)
        if eligibility!=expected:
            raise SystemExit("qualification differs from recomputed controls/correctness")
        if args.mode=="cost" and not eligibility.get("correctness_stage_included"):
            raise SystemExit("cost needs the additional full correctness campaign in qualification")
    limits=STAGE_BUDGETS.get(args.mode)
    if limits is None:
        raise SystemExit("stage retains full coverage but has no reviewed aggregate execution budget: "+args.mode)
    timer=args.timer_packet.resolve() if args.timer_packet else None
    root=raw_root()/"INV-01-18"
    root.mkdir(exist_ok=True)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    campaign=root/(stamp+"-"+args.mode)
    campaign.mkdir()
    run_id=uuid.uuid4().hex
    schedule=list(jobs(plan,args.mode))
    if args.mode!="preflight-remaining":
        random.Random(18001 if args.mode!="cost" else 18101).shuffle(schedule)
    write_new(campaign/"coverage.json",plan)
    write_new(campaign/"code-packet.json",sources)
    write_new(campaign/"environment.json",host)
    write_new(campaign/"signatures.json",signed)
    write_new(campaign/"run.json",dict(schema="INV-01-18-run-v2",run_id=run_id,
                                        timer_path=str(timer) if timer else None,build_directory=str(directory),
                                        build_manifest_sha256=sha(build_manifest),
                                        qualification_sha256=sha(args.qualification) if args.qualification else None))
    write_new(campaign/"schedule.json",dict(jobs=schedule,planned=len(schedule),child_ceiling_seconds=30,
                                          maximum_child_budget_seconds=30*len(schedule),
                                          bytes=byte_estimate(schedule),free_disk_bytes_at_start=shutil.disk_usage(campaign).free,
                                          incomplete_interactions=plan["deferred"]))
    write_new(campaign/"budget.json",dict(schema="INV-01-18-budget-v1",run_id=run_id,
                                         mode=args.mode,limits=limits))
    results=[]
    abort=False
    budget_start=time.monotonic()
    try:
        with wall_budget(limits["wall_seconds"]):
            check_budget(campaign,limits)
            for index,job in enumerate(schedule):
                if abort:
                    results.append(dict(index=index,status="not-run",reason="campaign stopped after invalid/unknown outcome"))
                    continue
                if eligibility and job["cell"] not in eligibility["eligible_cells"]:
                    results.append(dict(index=index,status="not-run",reason="required controls/correctness unavailable or failed; obligation retained"))
                    continue
                if packet(directory/"snapshot")!=sources:
                    abort=True
                    results.append(dict(index=index,status="not-run",reason="source packet changed"))
                    continue
                now=environment()
                if any(now[key]!=host[key] for key in ("host_id","model","os_build","sdk")):
                    abort=True
                    results.append(dict(index=index,status="not-run",reason="environment identity changed"))
                    continue
                binary=args.binary_dir.resolve()/("protocol-"+job["signing"])
                if not binary.is_file():
                    results.append(dict(index=index,status="unavailable",reason="signing binary absent"))
                    continue
                if sha(binary)!=signed[job["signing"]]["binary_sha256"]:
                    abort=True
                    results.append(dict(index=index,status="not-run",reason="signed binary changed"))
                    continue
                result=run_child(binary,job,campaign/f"{index:06d}",timer,run_id,build["run_id"],limits)
                check_budget(campaign,limits)
                results.append(dict(index=index,**result))
                expected_negative=job["kind"]=="negative" and result.get("negative_detected")
                if result["status"] not in ("correctness-qualified","control-qualified") and not expected_negative:
                    abort=True
    except BaseException as exc:
        abort=True
        write_new(campaign/"failure.json",dict(type=type(exc).__name__,message=str(exc)))
        completed={row["index"] for row in results}
        for index in range(len(schedule)):
            if index not in completed:
                results.append(dict(index=index,status="not-run",reason="campaign interrupted; inspect retained partial child"))
    write_new(campaign/"results.json",dict(results=results,planned=len(schedule),completed_question=False,
                                        evidence_mode=args.mode,stopped=abort))
    write_new(campaign/"budget-outcome.json",dict(schema="INV-01-18-budget-outcome-v1",run_id=run_id,
                  elapsed_seconds=time.monotonic()-budget_start,stopped=abort,
                  payload_bytes_before_terminal_inventory=output_bytes(campaign)))
    write_new(campaign/"manifest.json",dict(schema="INV-01-18-campaign-v3",run_id=run_id,files=file_manifest(campaign),completed_question=False))
    print(campaign)
    if abort:
        raise SystemExit(1)


if __name__=="__main__":
    main()
