"""Small, synthetic checker/driver fixtures. These are never host evidence.

Writes disposable fixture traces only in an OS temporary directory. Imports
do not execute native code, allocate executable memory, or start threads.
"""
import collections
import copy
import json
from pathlib import Path
import signal
import tempfile

from check import Invalid, check, clock_result, instruction_words, pair
from run import negative_result,protection_result


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
    emit(0,"allocation-plan",100,100,10000,256<<20,24,16)
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
        job,rows=fixture()
        mutations=[("call",2,0,"CALL_GENERATION"), ("admit",1,4,"RETIRED_ADMISSION"),
                   ("admit",4,99,"STALE_ADMISSION"), ("admit",5,0,"I_UNPROTECTED_ADMISSION"),
                   ("reuse",1,0,"REUSE_NOT_RETIRED"), ("ack",2,99,"ACK_PAGE_FLOOR"),
                   ("code-word",2,0,"CODE_INSTRUCTIONS"), ("allocation-plan",2,300<<20,"ALLOCATION_CAP"),
                   ("worker-done",0,127,"CALL_FLOOR"), ("invalidate-complete",4,99,"UNRELATED_PAGE_INVALIDATED")]
        mutations.append(("stress-begin",1,1,"POST_WARMUP_CALL_FLOOR"))
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
    print(json.dumps(dict(kind="synthetic-fixtures",evidence=False,trace_checks=checks,protection_checks=protection_checks,clock_cases=6,negative_classification_cases=3)))


if __name__=="__main__": main()
