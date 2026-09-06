"""Small, synthetic checker/driver fixtures. These are never host evidence.

Writes disposable fixture traces only in an OS temporary directory. Imports
do not execute native code, allocate executable memory, or start threads.
"""
import collections
import copy
import json
import os
from pathlib import Path
import signal
import tempfile

from check import Invalid, check, clock_result, instruction_words, pair
from run import HERE, REPO, command, negative_result, protection_result


def protection_fixture(arm,pages,control):
    job=dict(cell="protection-fixture",mode="preflight",kind="protection",n=2,
             seed=18001,arm=arm,signing="C",size=256,pages=pages,negative="protection-"+control)
    header=dict(job,kind="header",page_size=16384)
    jit=arm=="M-jit-protect"; base=0x10000000
    length=(16<<20) if jit else pages*16384
    pointer=0x2000 if jit else base+length
    access=dict(kind="protection-access",function=0x1000,data=pointer,value=0x1864)
    api=dict(kind="protection-api",address=base,length=length,result=0,errno=0)
    rows=[header]
    if control=="neighbor": rows.append(dict(access,phase="before"))
    rows.append(dict(api,phase="protect"))
    if control=="restored":
        rows.append(dict(api,phase="restore"))
        if jit: rows.append(dict(kind="protection-restore-callback",result=0))
    rows.append(dict(kind="protection-arm",control=job["negative"],jit=jit,address=base,length=length,
                     owned_length=length if jit else 2*length,
                     accessible_control="unrelated-AOT-and-data-not-adjacent" if jit else "adjacent-owned-data-page"))
    if control=="neighbor": rows.append(dict(access,phase="after"))
    if control=="restored" and jit:
        x,y=pair(1,job["seed"]); rows.append(dict(kind="protection-jit-return",x=x,y=y))
    if control!="forbidden": rows.append(dict(kind="protection-complete",value=0x1864))
    status=dict(returncode=-signal.SIGBUS if control=="forbidden" else 0,
                signal=signal.SIGBUS if control=="forbidden" else None,timed_out=False)
    return job,rows,status


def fixture(arm="I"):
    job=dict(cell="fixture",mode="preflight",kind="preflight",n=1,seed=18001,
             arm=arm,signing="A",size=64,rate=1)
    header=dict(kind="header",investigation="INV-01-18",method="M18-2",page_size=16384,
                numer=125,denom=3,code_offset=0,**{k:job[k] for k in ("cell","mode","n","seed","signing","size")},arm=arm[0])
    streams=collections.defaultdict(list)
    def emit(actor,kind,*values):
        streams[actor].append(dict(kind=kind,actor=actor,seq=len(streams[actor]),v=list(values)+[0]*(6-len(values))))
    # tokens/request_cap follow check.py's re-derivation of protocol.c's
    # formula for n=1, pressure=normal, control mode (warmup=4,publications=32):
    # total=4+32+16=52; tokens=52*1+1=53; request_cap=52*3+16=172.
    emit(0,"allocation-plan",53,172,10000,256<<20,24,16)
    emit(0,"setup-plan",0,0,0,0,10000,0)
    emit(0,"participants",1,1,1,0,0,0)
    for actor in (0,1,12): emit(actor,"qos",21,21,0)
    versions=[1,1]; current=[0,0]; nonces={}; epoch=0; token=0
    floors=[0,0]
    def request(page):
        nonlocal epoch
        epoch+=1
        stop=int(arm[0] in "SM")
        emit(0,"request",epoch,page,versions[page],stop)
        emit(0,"all-ack",epoch,page,versions[page],stop)
        emit(1,"ack",epoch,page,versions[page])
        floors[page]=versions[page]
        if stop:
            emit(0,"resume-issued",epoch)
            emit(1,"resume",epoch)
    def publish(page):
        nonlocal token
        prior=current[page]
        token+=1
        nonce=0
        if prior:
            previous=pair(prior,job["seed"],nonces[prior])
            while any((a>>(16*h))&65535 == (b>>(16*h))&65535
                      for a,b in zip(previous,pair(token,job["seed"],nonce)) for h in range(4)):
                nonce+=1
            emit(0,"retire",prior,page,epoch)
            emit(0,"reuse",prior,token,page)
        nonces[token]=nonce
        emit(0,"descriptor",token,page,versions[page],0,page,nonce)
        x,y=pair(token,job["seed"],nonce)
        emit(0,"built",token,page,versions[page],0,x,y)
        if token<=2:
            for index,word in instruction_words((x,y),64,False).items():
                emit(0,"code-word",token,index,word)
        if arm[0] in "SM": request(page)
        emit(0,"publish",token,page,0,prior,versions[page])
        if arm[0]=="E": request(page)
        current[page]=token
    publish(0); publish(1)
    backing=[1,2]
    for round_number in range(36):
        page=round_number%2; kind=round_number%4
        before=backing[page]; backing[page]+=int(kind==2)
        emit(0,"guest-stimulus",round_number+1,kind,page,before,backing[page],int(kind==1))
        if kind==1:
            emit(1,"stimulus-worker",round_number+1,page,round_number,round_number+1,0)
        versions[page]+=1
        emit(0,"invalidate-start",page,versions[page],versions[page^1])
        request(page)
        emit(0,"invalidate-complete",page,versions[page],epoch,versions[page^1],versions[page^1])
        publish(page)
        emit(0,"round",round_number,page,int(round_number>=4))
        emit(0,"retention",round_number,0,0,0)
    for index in range(128):
        page=index%2; t=current[page]
        x,y=pair(t,job["seed"],nonces[t])
        emit(1,"admit",t,2,epoch,page,floors[page],t if arm=="I" else 0)
        emit(1,"call",t,versions[page],x,y,1+index%2,page)
        if arm.startswith("M-"):
            emit(1,"mapping-witness",t,1+index%2,1+index%2,epoch)
    emit(0,"stress-begin",0,0,4)
    if arm=="M-jit-protect":
        emit(0,"capability",1,0x10000000,16<<20)
        emit(0,"mapping",3,0x10000000,16<<20,1,1,0)
        emit(0,"restore-thread",epoch,0,1)
        emit(1,"restore-thread",epoch,0,0)
    emit(1,"worker-done",128,epoch)
    emit(0,"complete",4,32,128,1,token,epoch)
    # Deliberate collector order: coordinator stream before reader stream.
    return job,[header]+streams[0]+streams[1]+streams[12]


def pressure_fixture(pressure="near-full"):
    """R5: synthetic M18-P1 pressure-mode trace, arm I only. tests.py otherwise
    emits only pressure="normal" jobs, so check.py's ~55 near-full/fragmented
    lines (check.py:191-215, :366-397, added by REV-214) ship with zero
    fixture coverage. Reuses REV-200's own minimal topology: a 2 MiB header
    page size gives slot_count=4, H={3} (near-full and fragmented coincide at
    this size), actives at slots {0,1}, scratch at slot 2.

    Not covered here (disclosed gap, REV-226): S/M's in-place no-replacement
    branch and V's owner-transfer branch. Only arm I's near-full-with-
    replacement and fragmented-without-replacement paths are exercised.
    """
    arm="I"
    page_size=2*1024*1024
    job=dict(cell="pressure-fixture",mode="preflight",kind="preflight",n=1,seed=18001,
             arm=arm,signing="A",size=64,rate=1,pressure=pressure)
    header=dict(kind="header",investigation="INV-01-18",method="M18-2",page_size=page_size,
                numer=125,denom=3,code_offset=0,**{k:job[k] for k in ("cell","mode","n","seed","signing","size")},arm=arm[0])
    streams=collections.defaultdict(list)
    def emit(actor,kind,*values):
        streams[actor].append(dict(kind=kind,actor=actor,seq=len(streams[actor]),v=list(values)+[0]*(6-len(values))))

    slots=(16<<20)//(2*page_size)                     # 4
    owners=1
    H=sorted(s for s in range(2*owners+1,slots) if pressure=="near-full" or (pressure=="fragmented" and s%2))
    scratch_count=2 if pressure=="near-full" else 0
    replacement=pressure=="near-full"                 # arm I is not in "SM"
    held_count=len(H)
    setup_publications=held_count+int(replacement)
    total_plan=4+32+16                                # control-mode warmup+publications+16
    tokens_plan=total_plan*owners+held_count+scratch_count+int(replacement)+1
    request_cap_plan=total_plan*3+16+2*held_count+2*int(replacement)
    bytes_plan=tokens_plan*24+request_cap_plan*16+50000

    emit(0,"allocation-plan",tokens_plan,request_cap_plan,bytes_plan,256<<20,24,16)
    emit(0,"participants",1,1,1,0,0,0)
    for actor in (0,1,12): emit(actor,"qos",21,21,0)
    emit(0,"setup-plan",held_count,scratch_count,int(replacement),2*held_count+2*int(replacement),bytes_plan,0)

    versions=[1,1]; current=[0,0]; nonces={}; epoch=0; token=0; floors=[0,0]
    slot_of={}; page_slot=[0,1]

    def request(page):
        nonlocal epoch
        epoch+=1
        emit(0,"request",epoch,page,versions[page],0)
        emit(0,"all-ack",epoch,page,versions[page],0)
        emit(1,"ack",epoch,page,versions[page])
        floors[page]=versions[page]

    def publish(page):
        nonlocal token
        slot=page_slot[page]; prior=current[page]
        token+=1; nonce=0
        if prior:
            previous=pair(prior,job["seed"],nonces[prior])
            while any((a>>(16*h))&65535 == (b>>(16*h))&65535
                      for a,b in zip(previous,pair(token,job["seed"],nonce)) for h in range(4)):
                nonce+=1
            emit(0,"retire",prior,page,epoch)
            emit(0,"reuse",prior,token,slot)
        nonces[token]=nonce; slot_of[token]=slot
        emit(0,"descriptor",token,slot,versions[page],0,page,nonce)
        x,y=pair(token,job["seed"],nonce)
        emit(0,"built",token,page,versions[page],0,x,y)
        if token<=2:
            for index,word in instruction_words((x,y),64,False).items():
                emit(0,"code-word",token,index,word)
        emit(0,"publish",token,page,0,prior,versions[page])
        current[page]=token
        return token,x,y

    def call_once(t,x,y,page):
        emit(1,"admit",t,2,epoch,page,floors[page],t)
        emit(1,"call",t,versions[page],x,y,1,slot_of[t])

    # --- M18-P1 pressure prefix: populate H one member at a time ---
    emit(0,"stage",1,0,0,0,0,0)
    h_tokens=[]
    for slot in H:
        token+=1; t=token; nonce=0
        nonces[t]=nonce; slot_of[t]=slot
        emit(0,"descriptor",t,slot,versions[0],0,0,nonce)
        x,y=pair(t,job["seed"],nonce)
        emit(0,"built",t,0,versions[0],0,x,y)
        emit(0,"publish",t,0,0,0,versions[0])
        current[0]=t
        call_once(t,x,y,0)
        emit(0,"setup-call-witness",t,0,0,1,0,0)
        versions[0]+=1
        emit(0,"invalidate-start",0,versions[0],versions[1])
        request(0)
        emit(0,"invalidate-complete",0,versions[0],epoch,versions[1],versions[1])
        emit(0,"retire",t,slot,epoch,0,0,0)
        emit(0,"pressure-hold",t,slot,0,0,0,0)
        current[0]=0
        h_tokens.append(t)
    emit(0,"stage",2,held_count,held_count,epoch,0,0)

    # --- ordinary bootstrap actives (slots 0 and 1) ---
    t2,x2,y2=publish(0); call_once(t2,x2,y2,0)
    t3,x3,y3=publish(1); call_once(t3,x3,y3,1)

    # --- pressure_diagnostic: allocator-only scratch dance + genuine swap ---
    if pressure=="near-full":
        scratch_slot=2*owners
        token+=1; s1=token; nonce=0
        nonces[s1]=nonce; slot_of[s1]=scratch_slot
        emit(0,"descriptor",s1,scratch_slot,versions[0],0,0,nonce)
        emit(0,"scratch-reserve",s1,scratch_slot,1,0,0,0)
        emit(0,"allocator-reject",s1,scratch_slot,0,1,0,0)
        emit(0,"scratch-discard",s1,scratch_slot,0,0,0,0)

        token+=1; s2=token; nonce=0
        nonces[s2]=nonce; slot_of[s2]=scratch_slot
        emit(0,"descriptor",s2,scratch_slot,versions[0],0,0,nonce)
        emit(0,"scratch-reserve",s2,scratch_slot,2,0,0,0)
        emit(0,"allocator-reject",s2,scratch_slot,0,1,0,0)
        emit(0,"scratch-reuse",s1,s2,scratch_slot,0,0,0)

        if replacement:
            released=h_tokens[0]; old_active=current[0]
            token+=1; new=token; nonce=0
            previous=pair(released,job["seed"],nonces[released])
            while any((a>>(16*h))&65535 == (b>>(16*h))&65535
                      for a,b in zip(previous,pair(new,job["seed"],nonce)) for h in range(4)):
                nonce+=1
            nonces[new]=nonce; slot_of[new]=slot_of[released]
            emit(0,"backpressure",new,slots,0,0,0,0)
            emit(0,"pressure-release",released,slot_of[released],new,0,0,0)
            emit(0,"reuse",released,new,slot_of[released],0,0,0)
            emit(0,"descriptor",new,slot_of[released],versions[0],0,0,nonce)
            xn,yn=pair(new,job["seed"],nonce)
            emit(0,"built",new,0,versions[0],0,xn,yn)
            emit(0,"publish",new,0,0,old_active,versions[0])
            current[0]=new; page_slot[0]=slot_of[new]
            call_once(new,xn,yn,0)
            emit(0,"setup-call-witness",new,0,0,1,0,0)
            emit(0,"retire",old_active,slot_of[old_active],epoch,0,0,0)
            emit(0,"pressure-hold",old_active,slot_of[old_active],1,0,0,0)
            emit(0,"pressure-swap",released,new,old_active,0,s2,0)
        emit(0,"scratch-discard",s2,scratch_slot,0,0,0,0)

    # --- final per-slot layout, exactly the slots this fixture ever touches ---
    if pressure=="near-full":
        layout={0:(old_active,1,4),1:(t3,0,2),2:(0,0,0),3:(new,0,2)}
    else:
        layout={0:(t2,0,2),1:(t3,0,2),2:(0,0,0),3:(h_tokens[0],1,4)}
    for s in range(slots):
        t,held,life=layout[s]
        emit(0,"pressure-layout",s,t,held,life,0,0)
    emit(0,"setup-summary",held_count,setup_publications,scratch_count,epoch,1,0)
    emit(0,"stage",5,0,0,0,0,0)

    # --- unchanged warmup/stress: 36 rounds, alternating pages ---
    backing=[1,2]
    for round_number in range(36):
        page=round_number%2; kind=round_number%4
        before=backing[page]; backing[page]+=int(kind==2)
        emit(0,"guest-stimulus",round_number+1,kind,page,before,backing[page],int(kind==1))
        if kind==1:
            emit(1,"stimulus-worker",round_number+1,page,round_number,round_number+1,0)
        versions[page]+=1
        emit(0,"invalidate-start",page,versions[page],versions[page^1])
        request(page)
        emit(0,"invalidate-complete",page,versions[page],epoch,versions[page^1],versions[page^1])
        publish(page)
        emit(0,"round",round_number,page,int(round_number>=4))
        emit(0,"retention",round_number,0,0,0)
    for index in range(128):
        page=index%2; t=current[page]
        x,y=pair(t,job["seed"],nonces[t])
        call_once(t,x,y,page)
    setup_calls=held_count+int(pressure=="near-full" and replacement)
    total_calls=setup_calls+2+128
    emit(0,"stress-begin",0,0,4)
    emit(1,"worker-done",total_calls,epoch)
    emit(0,"complete",4,32,128,1,token,epoch)
    return job,[header]+streams[0]+streams[1]+streams[12]


def retained_campaign_fixtures():
    import run
    from unittest.mock import patch
    def write(path,value):
        path.write_text(json.dumps(value))
    with tempfile.TemporaryDirectory(prefix="inv18-retention-") as temporary:
        root=Path(temporary).resolve(); directory=root/"campaign"; directory.mkdir()
        build_dir=root/"build"; build_dir.mkdir()
        sources={"files":{},"sha256":"synthetic"}
        for signing in "ABC": (build_dir/("protocol-"+signing)).write_text("synthetic binary "+signing)
        build=dict(schema="INV-01-18-build-v2",run_id="b"*32,outcome="built",code_packet=sources,
                   files={path.name:run.sha(path) for path in build_dir.iterdir()})
        write(build_dir/"build-manifest.json",build)
        schedule=list(run.jobs(run.manifest(),"preflight"))
        write(directory/"schedule.json",dict(jobs=schedule))
        write(directory/"code-packet.json",sources)
        write(directory/"environment.json",dict(host_id="synthetic",model="synthetic",os_build="synthetic",sdk="synthetic"))
        write(directory/"run.json",dict(schema="INV-01-18-run-v2",run_id="a"*32,build_directory=str(build_dir),
                                      build_manifest_sha256=run.sha(build_dir/"build-manifest.json")))
        results=[]
        for index,job in enumerate(schedule):
            child=directory/f"{index:06d}"; child.mkdir()
            result=dict(status="correctness-qualified")
            results.append(dict(index=index,**result))
            write(child/"invocation.json",dict(schema="INV-01-18-invocation-v2",run_id="a"*32,
                  binary_sha256=build["files"]["protocol-"+job["signing"]],
                  argv=run.native_args(build_dir/("protocol-"+job["signing"]),job)))
            write(child/"trace.jsonl",dict(schema="INV-01-18-trace-v2",run_id="a"*32,build_id="b"*32))
            write(child/"manifest.json",dict(schema="INV-01-18-child-v2",run_id="a"*32,
                  job_sha256=run.digest(job),result=result))
        write(directory/"results.json",dict(evidence_mode="preflight",results=results))
        write(directory/"budget.json",dict(schema="INV-01-18-budget-v1",run_id="a"*32,
                                          mode="preflight",limits=run.STAGE_BUDGETS["preflight"]))
        def restamp():
            write(directory/"manifest.json",dict(schema="INV-01-18-campaign-v3",run_id="a"*32,files=run.file_manifest(directory)))
        def rejected():
            try: run.verified_campaign(directory,sources)
            except (SystemExit,ValueError,KeyError): return
            raise AssertionError("mixed/invalid campaign accepted")
        restamp(); run.verified_campaign(directory,sources)
        # Unrelated commit/editorial bytes do not participate in retained identity.
        (root/"README.md").write_text("unrelated editorial revision")
        with patch.object(run,"command",side_effect=AssertionError("git must not be consulted")):
            run.verified_campaign(directory,sources)
        child=directory/"000000"
        original=json.loads((child/"trace.jsonl").read_text())
        for key,value in (("run_id","c"*32),("build_id","d"*32),("schema","historical")):
            write(child/"trace.jsonl",dict(original,**{key:value})); restamp(); rejected()
        write(child/"trace.jsonl",original); restamp()
        with patch.dict(os.environ,NATEMU_RAW_ROOT=str(root)):
            try: run.qualify([directory],root/"qualification.json",sources)
            except SystemExit as exc: assert "full preflight/hooks/negative/protection" in str(exc)
            else: raise AssertionError("missing controls qualified")
        try: run.qualification([],sources)
        except SystemExit as exc: assert "full preflight/hooks/negative/protection" in str(exc)
        else: raise AssertionError("caller-declared eligibility can bypass missing controls")
        original_schedule=list(schedule)
        write(directory/"schedule.json",dict(jobs=schedule[:-1])); restamp(); rejected()
        write(directory/"schedule.json",dict(jobs=original_schedule)); restamp()
        (build_dir/"protocol-A").write_text("different producing binary"); rejected()
        # Unknown historical schemas cannot inherit v2 success from omitted fields.
        write(directory/"manifest.json",dict(files=run.file_manifest(directory))); rejected()


def qualification_consumer_fixtures():
    """Independent review reproduction: the consumer must rederive controls.

    Author: /root/investigations. Synthetic build and host only; child launch
    is replaced by a failure. This exercises main(), not just its helper.
    """
    import contextlib
    import sys
    import run
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix="inv18-qualification-consumer-") as temporary:
        root=Path(temporary).resolve(); directory=root/"build"; snapshot=directory/"snapshot"
        snapshot.mkdir(parents=True)
        sources={"files":{},"sha256":"synthetic-only"}
        build=dict(schema="INV-01-18-build-v2",outcome="built",files={},code_packet=sources)
        (directory/"build-manifest.json").write_text(json.dumps(build))
        host=dict(host_id="fixture",model="fixture",os_build="fixture",sdk="fixture")
        base=dict(schema="INV-01-18-qualification-v2",kind="control-qualification",code_packet=sources,
                  host=host,build_manifest_sha256=run.sha(directory/"build-manifest.json"),campaigns=[],
                  eligible_cells=["fabricated"],blocked_cells={},correctness_stage_included=True,
                  evidence=False,completed_question=False)
        qualification=root/"qualification.json"
        def consume(supplied,expected=None):
            qualification.write_text(json.dumps(supplied))
            argv=["run.py","run","--mode","cost","--binary-dir",str(directory),"--qualification",str(qualification)]
            with contextlib.ExitStack() as stack:
                for target,value in [("HERE",snapshot),("packet",lambda *args:sources),("environment",lambda:host),
                                     ("signatures",lambda *args:{}),("raw_root",lambda:root)]:
                    stack.enter_context(patch.object(run,target,value))
                stack.enter_context(patch.object(sys,"argv",argv))
                stack.enter_context(patch.object(run.os,"execv",side_effect=AssertionError("reexec unexpected")))
                stack.enter_context(patch.object(run,"run_child",side_effect=AssertionError("child must never launch")))
                if expected is not None:
                    stack.enter_context(patch.object(run,"qualification",return_value=expected))
                try: run.main()
                except SystemExit as exc: return str(exc)
                raise AssertionError("fabricated qualification accepted")
        assert "full preflight/hooks/negative/protection" in consume(base)
        expected=dict(base,eligible_cells=[],correctness_stage_included=False)
        for supplied in (base,dict(expected,eligible_cells=["fabricated"]),dict(expected,correctness_stage_included=True)):
            assert "differs from recomputed controls/correctness" in consume(supplied,expected)


def remaining_preflight_fixtures():
    """The continuation preserves the original suffix and cannot replace preflight."""
    import random
    import run
    from unittest.mock import patch
    plan=run.manifest()
    original=list(run.jobs(plan,"preflight"))
    random.Random(18001).shuffle(original)
    remaining=list(run.jobs(plan,"preflight-remaining"))
    assert [job["arm"] for job in original[:3]]==["E","S","M-jit-protect"]
    assert remaining==original[3:] and len(remaining)==4
    assert 30*len(remaining)==120
    assert run.STAGE_BUDGETS["preflight-remaining"]==run.STAGE_BUDGETS["preflight"]
    with tempfile.TemporaryDirectory(prefix="inv18-remaining-qualification-") as temporary:
        directory=Path(temporary)
        (directory/"environment.json").write_text(json.dumps(dict(host_id="fixture",model="fixture",os_build="fixture",sdk="fixture")))
        (directory/"run.json").write_text(json.dumps(dict(build_manifest_sha256="fixture")))
        (directory/"manifest.json").write_text("{}")
        results=[dict(index=i,status="correctness-qualified") for i in range(4)]
        with patch.object(run,"verified_campaign",return_value=(directory,remaining,results,"preflight-remaining")):
            try: run.qualification([directory],{})
            except SystemExit as exc: assert "full preflight/hooks/negative/protection" in str(exc)
            else: raise AssertionError("remaining subset promoted to full qualification")


def budget_fixtures():
    """Synthetic owned children only; no generated code or host experiment."""
    import run
    import subprocess
    import sys
    import time
    from unittest.mock import patch
    # Preserve the original twelve controls and all three seeds; this stage
    # cannot silently select a favorable control or shrink its denominator.
    negative_jobs=list(run.jobs(run.manifest(),"negative"))
    assert len(negative_jobs)==36
    assert {(j['negative'],j['seed']) for j in negative_jobs}=={
        (name,seed) for name in run.NEGATIVES for seed in (18001,18002,18003)}
    assert all(j['arm']=='I' and j['n']==j['producers']==2 and j['signing']=='A' for j in negative_jobs)
    assert run.STAGE_BUDGETS['negative']==dict(wall_seconds=1800,output_bytes=2<<30,
        child_file_bytes=128<<20,free_reserve_bytes=1<<30)
    assert len(negative_jobs)*30==1080<run.STAGE_BUDGETS['negative']['wall_seconds']
    with tempfile.TemporaryDirectory(prefix="inv18-budget-") as temporary:
        root=Path(temporary)
        limits=dict(run.STAGE_BUDGETS["preflight"],output_bytes=4096,free_reserve_bytes=0)
        child=subprocess.Popen([sys.executable,"-c","pass"],start_new_session=True)
        assert run.wait_child(child,root,root,limits)==(0,False)
        child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"],start_new_session=True)
        try:
            with run.wall_budget(.05): run.wait_child(child,root,root,limits)
        except run.BudgetExceeded as exc: assert "wall" in str(exc)
        else: raise AssertionError("wall deadline missed")
        assert child.poll()==-signal.SIGKILL
        (root/"overflow").write_bytes(b"x"*8192)
        child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(10)"],start_new_session=True)
        try: run.wait_child(child,root,root,limits)
        except run.BudgetExceeded as exc: assert "output" in str(exc)
        else: raise AssertionError("output cap missed")
        assert child.poll()==-signal.SIGKILL
        (root/"overflow").unlink()
        with patch.object(run.shutil,"disk_usage",return_value=type("Disk",(),{"free":0})()):
            try: run.check_budget(root,dict(limits,free_reserve_bytes=1))
            except run.BudgetExceeded as exc: assert "reserve" in str(exc)
            else: raise AssertionError("low disk reserve missed")
        # The actual launcher applies an OS per-file bound, independently of polling.
        writer=[sys.executable,"-c", "import os,signal; signal.signal(signal.SIGXFSZ,signal.SIG_DFL); os.write(1,b'x'*8192); os.write(1,b'y')"]
        with patch.object(run,"native_args",return_value=writer):
            try:
                run.run_child(Path(sys.executable),{},root/"limited",limits=dict(limits,output_bytes=1<<20,child_file_bytes=128))
            except run.BudgetExceeded as exc: assert "file limit" in str(exc)
            else: raise AssertionError("native file cap missed")
        assert (root/"limited/trace.jsonl").stat().st_size==128


def mprotect_failure_fixtures():
    """Exercise synchronous reporting with fabricated API outcomes only."""
    import run
    import subprocess
    job={"arm":"M-jit-protect","pages":1,"kind":"preflight"}
    with tempfile.TemporaryDirectory(prefix="inv18-mprotect-witness-") as temporary:
        directory=Path(temporary)
        source=directory/"witness.c"
        source.write_text('#define main protocol_main\n#include "'+str(HERE/"src/protocol.c")+'"\n'
            '#undef main\nint main(int argc,char **argv) { (void)argv;\n'
            'if(argc==1) mprotect_failure((void*)16384,16<<20,7,-1,13,-1,0);\n'
            'else mprotect_failure((void*)16384,16<<20,7,0,0,-1,13);\nreturn 0; }\n')
        binary=directory/"witness"
        subprocess.run(["xcrun","clang","-O2","-std=c11","-pthread",str(source),"-o",str(binary)],
                       check=True,capture_output=True,timeout=30)
        for args,attempted in (([],False),(["restore"],True)):
            output=subprocess.run([str(binary),*args],check=True,capture_output=True,text=True,timeout=5)
            actual=run.mprotect_failure_witness(output.stderr,job,16384)
            assert actual["restore_attempted"] is attempted
            assert actual["protect_result"]==(0 if attempted else -1)
            assert actual["restore_result"]==(-1 if attempted else None)
            assert actual["restore_errno"]==(13 if attempted else None)
            for key in actual:
                changed=dict(actual);del changed[key]
                try: run.mprotect_failure_witness("INV18 MPROTECT_RESULT "+json.dumps(changed),job,16384)
                except Invalid: pass
                else: raise AssertionError("missing failure field accepted: "+key)
            changed=dict(actual,restore_attempted=not attempted)
            try: run.mprotect_failure_witness("INV18 MPROTECT_RESULT "+json.dumps(changed),job,16384)
            except Invalid: pass
            else: raise AssertionError("attempted/skipped restore confused")
        try: run.mprotect_failure_witness("INV18 UNAVAILABLE mprotect roundtrip errno=13",job,16384)
        except Invalid: pass
        else: raise AssertionError("historical ambiguous witness accepted")

        # Exercise the real child consumer: exit 78 cannot overwrite bad identity.
        import sys
        import types
        from unittest.mock import patch
        header=dict(schema="INV-01-18-trace-v2",run_id="run",build_id="build",
                    page_size=16384,numer=125,denom=3)
        command_code="import sys; print(sys.argv[1]) if sys.argv[1] else None; sys.stderr.write(sys.argv[2]); sys.exit(78)"
        stderr=output.stderr+"INV18 UNAVAILABLE mprotect roundtrip errno=13\n"
        variants=[header,dict(header,run_id="other"),dict(header,build_id="other"),None]
        for index,candidate in enumerate(variants):
            argv=[sys.executable,"-c",command_code,json.dumps(candidate) if candidate else "",stderr]
            with patch.object(run,"native_args",return_value=argv), \
                 patch.object(run,"check",return_value=dict(status="INVALID",invariant="INCOMPLETE")), \
                 patch.object(run,"command",return_value="synthetic"), \
                 patch.dict(sys.modules,clock_evidence=types.SimpleNamespace(validate=lambda *args:dict(status="unqualified"))):
                result=run.run_child(Path(sys.executable),job,directory/f"child-{index}",
                                     run_id="run",build_id="build",limits=run.STAGE_BUDGETS["preflight"])
            assert result["status"]==("unavailable" if index==0 else "INVALID"),result
        for changed in (dict(actual,length=16384),dict(actual,restore_protection=1)):
            try: run.mprotect_failure_witness("INV18 MPROTECT_RESULT "+json.dumps(changed),job,16384)
            except Invalid: pass
            else: raise AssertionError("wrong failure subarm context accepted")


def main():
    checks=0
    protection_checks=0
    with tempfile.TemporaryDirectory(prefix="inv18-fixtures-") as temporary:
        path=Path(temporary)/"trace.jsonl"
        def execute(job,rows,expected=None):
            nonlocal checks
            path.write_text("".join(json.dumps(row)+"\n" for row in rows))
            try:
                result=check(path,job)
            except Invalid as exc:
                assert expected and str(exc).startswith(expected),(expected,str(exc))
            else:
                assert expected is None,(expected,result)
                assert result["status"]=="correctness-qualified"
            checks+=1
        for arm in ("I","S","E","V","M-data-map","M-data-protect","M-jit-protect"):
            job,rows=fixture(arm); execute(job,rows)
        for pressure_mode in ("near-full","fragmented"):
            job,rows=pressure_fixture(pressure_mode); execute(job,rows)
        job,rows=pressure_fixture("near-full")
        for kind,column,value,expected in [
                ("allocator-reject",3,0,"ALLOCATOR_REJECTION_CAUSE"),
                ("setup-plan",0,2,"SETUP_CAP_ACCOUNTING"),
                ("pressure-hold",2,0,"PRESSURE_HOLD_BEFORE_CALL_WITNESS")]:
            changed=copy.deepcopy(rows)
            matches=[row for row in changed if row["kind"]==kind]
            matches[-1]["v"][column]=value
            execute(job,changed,expected)
        changed=copy.deepcopy(rows)
        changed.pop(max(i for i,row in enumerate(changed) if row["kind"]=="allocator-reject"))
        seq=collections.Counter()
        for row in changed[1:]: row["seq"]=seq[row["actor"]]; seq[row["actor"]]+=1
        execute(job,changed,"NEAR_FULL_REJECTION_AND_RESTORE")
        job,rows=fixture()
        mutations=[("call",2,0,"CALL_GENERATION"), ("admit",1,4,"RETIRED_ADMISSION"),
                   ("admit",4,99,"STALE_ADMISSION"), ("admit",5,0,"I_UNPROTECTED_ADMISSION"),
                   ("reuse",1,0,"REUSE_NOT_RETIRED"), ("ack",2,99,"ACK_PAGE_FLOOR"),
                   ("code-word",2,0,"CODE_INSTRUCTIONS"), ("allocation-plan",2,300<<20,"ALLOCATION_CAP"),
                   ("worker-done",0,127,"CALL_FLOOR"), ("invalidate-complete",4,99,"UNRELATED_PAGE_INVALIDATED")]
        mutations.append(("stress-begin",1,1,"POST_WARMUP_CALL_FLOOR"))
        # R1/REV-200 M4b: a falsified allocation-plan (tokens collapsed to 1
        # against the real 42-descriptor trace) must be rejected, not merely
        # checked against its own self-reported cap.
        mutations.append(("allocation-plan",0,1,"ALLOCATION_PLAN_TOKENS"))
        mutations.append(("setup-plan",4,1,"ALLOCATION_PLAN_INCONSISTENT"))
        for kind,column,value,expected in mutations:
            changed=copy.deepcopy(rows)
            next(row for row in changed if row["kind"]==kind)["v"][column]=value
            execute(job,changed,expected)
        for key,value in (("signing","C"),("seed",99),("size",256)):
            changed=copy.deepcopy(rows); changed[0][key]=value
            execute(job,changed,"IDENTITY_"+key.upper())
        changed=copy.deepcopy(rows); changed.pop(next(i for i,row in enumerate(changed) if row["kind"]=="call"))
        execute(job,changed,"TRACE_SEQUENCE")
        changed=copy.deepcopy(rows); at=next(i for i,row in enumerate(changed) if row["kind"]=="call")
        changed.insert(at,copy.deepcopy(changed[at])); execute(job,changed,"TRACE_SEQUENCE")
        path.write_bytes(b'{"kind":"header"}')
        try: check(path,job)
        except Invalid as exc: assert str(exc)=="TRACE_TRUNCATED"
        else: raise AssertionError("truncated tail accepted")
        checks+=1
        job,rows=fixture("S")
        changed=copy.deepcopy(rows)
        # Valid sequence numbers but an admission occurs before matching RESUME.
        last_resume=max(i for i,row in enumerate(changed) if row["kind"]=="resume")
        resume=changed.pop(last_resume)
        call_index=next(i for i,row in enumerate(changed) if row["kind"]=="call")
        changed.insert(call_index+1,resume)
        seq=collections.Counter()
        for row in changed[1:]: row["seq"]=seq[row["actor"]]; seq[row["actor"]]+=1
        execute(job,changed,"ADMISSION_BEFORE_RESUME")
        # R2/REV-200 M3 characterization: a well-formed `retire` record for the
        # live page-0 token, inserted before its first admission, is currently
        # ACCEPTED -- documenting the disclosed bound (module docstring, R2)
        # rather than a live defect. `check.py:432`'s RETIRED_ADMISSION check
        # only reads the admission's OWN recorded state field; it has no
        # cross-actor invariant relating this `retire` record to the many
        # ordinary admissions/calls of the same token that follow it in the
        # trace. If this ever starts being rejected, update this fixture and
        # the docstring/README bound it documents together.
        job,rows=fixture("I")
        changed=copy.deepcopy(rows)
        live_token=next(row["v"][0] for row in reversed(changed) if row["kind"]=="publish" and row["v"][1]==0)
        first_admit=next(i for i,row in enumerate(changed) if row["kind"]=="admit" and row["v"][0]==live_token)
        changed.insert(first_admit,dict(kind="retire",actor=0,seq=0,v=[live_token,0,0,0,0,0]))
        seq=collections.Counter()
        for row in changed[1:]: row["seq"]=seq[row["actor"]]; seq[row["actor"]]+=1
        execute(job,changed)
        def protection_test(job,rows,status,invalid=False):
            nonlocal protection_checks
            path.write_text("".join(json.dumps(row)+"\n" for row in rows))
            try: result=protection_result(path,job,status)
            except Invalid: assert invalid
            else: assert not invalid and result["status"]=="control-qualified"
            protection_checks+=1
        for arm,pages in (("M-jit-protect",1),("M-data-map",1),("M-data-map",4),("M-data-protect",1),("M-data-protect",4)):
            for control in ("forbidden","neighbor","restored"):
                job,rows,status=protection_fixture(arm,pages,control)
                protection_test(job,rows,status)
        job,rows,status=protection_fixture("M-jit-protect",1,"neighbor")
        for key,value in (("function",0x10000000),("data",0x10001000),("value",0)):
            changed=copy.deepcopy(rows)
            for row in changed:
                if row["kind"]=="protection-access": row[key]=value
            protection_test(job,changed,status,True)
        changed=copy.deepcopy(rows); changed.pop(1)
        protection_test(job,changed,status,True)
        job,rows,status=protection_fixture("M-data-protect",4,"neighbor")
        changed=copy.deepcopy(rows)
        for row in changed:
            if row["kind"]=="protection-access": row["data"]=0x10000000+16384
        protection_test(job,changed,status,True)
        job,rows,status=protection_fixture("M-jit-protect",1,"restored")
        changed=copy.deepcopy(rows); next(row for row in changed if row["kind"]=="protection-jit-return")["x"]=0
        protection_test(job,changed,status,True)
        job,rows,status=protection_fixture("M-jit-protect",1,"forbidden")
        protection_test(job,rows,dict(status,signal=signal.SIGILL,returncode=-signal.SIGILL),True)
    calibration={size:{i:(101 if size>=16 else 100,100) for i in range(64)} for size in (1,16,256,4096)}
    pairs={i:(0 if i%2 else 1,0 if i%3 else 1) for i in range(10000)}
    batches={i:(16,1000) for i in range(1000)}
    result=clock_result(pairs,calibration,batches,[(1,1000)],dict(numer=125,denom=3))
    assert result["status"]=="qualified" and result["batch_size"]==16 and result["pre_zero_count"]==5000
    for altered in ({i:(0,0) for i in pairs},{i:(a,0) for i,(a,b) in pairs.items()}):
        assert clock_result(altered,calibration,batches,[(1,1000)],dict(numer=1,denom=1))["status"]=="resolution-unresolved"
    assert clock_result(pairs,calibration,batches,[(1,100)],dict(numer=1,denom=1))["status"]=="resolution-unresolved"
    for malformed in (-1,1<<64):
        changed=dict(pairs); changed[0]=(malformed,1)
        try: clock_result(changed,calibration,batches,[(1,1000)],dict(numer=1,denom=1))
        except Invalid as exc: assert str(exc)=="CLOCK_RANGE"
        else: raise AssertionError("clock range accepted")
    job=dict(negative="lost-ack")
    timeout=dict(returncode=-signal.SIGKILL,signal=signal.SIGKILL,timed_out=True)
    assert not negative_result(job,timeout,{},"unrelated failure")
    assert negative_result(job,timeout,{},"INV18 NEGATIVE 8 token=1 detail=0\n")
    assert not negative_result(job,dict(timeout,timed_out=False),{},"INV18 NEGATIVE 8 token=1 detail=0\n")
    retained_campaign_fixtures()
    qualification_consumer_fixtures()
    remaining_preflight_fixtures()
    budget_fixtures()
    mprotect_failure_fixtures()
    print(json.dumps(dict(kind="synthetic-fixtures",evidence=False,trace_checks=checks,protection_checks=protection_checks,clock_cases=6,negative_classification_cases=3)))


if __name__=="__main__": main()
