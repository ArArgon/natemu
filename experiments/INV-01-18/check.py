"""Strict streamed raw-trace checker; no subprocess, JIT or thread execution.

Two passes deliberately tolerate collector interleaving between actor streams.
Actor sequence numbers are local order, never a global timestamp/SC-order proof.
"""
import collections
import json
import math
from pathlib import Path

MASK = (1 << 64) - 1


class Invalid(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Invalid(code)


def mix(value):
    value = (value + 0x9E3779B97F4A7C15) & MASK
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
    return value ^ (value >> 31)


def pair(token, seed, nonce=0):
    value=token ^ seed ^ mix(nonce+0x18DEC0DE)
    return mix(value), mix(value ^ 0xA596C3F02D7841BE)


def records(path):
    with Path(path).open("rb") as stream:
        for number, line in enumerate(stream, 1):
            require(line.endswith(b"\n"), "TRACE_TRUNCATED")
            try:
                value = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise Invalid(f"TRACE_JSON:{number}") from exc
            require(isinstance(value, dict) and isinstance(value.get("kind"), str), "TRACE_SCHEMA")
            yield value


EVENTS = frozenset("""failure unavailable resolution-unresolved capacity-unresolved
qos capability owned-map descriptor built code-word call admit retry ack resume
request all-ack resume-issued invalidate-start invalidate-complete translation-rejected
scan retire reuse publish round complete worker-done grant grant-complete producer-request
hook-arrive hook-release invalidation-attempt old-progress competitor-attempt mapping
mapping-witness restore-thread backpressure pressure-capacity retire-unpublished pressure-hold
allocation-plan calibration coordinator-pair worker-pair cost-batch cost-phase""".split())
EVENTS |= frozenset(("negative-trigger","held-retirement","held-revalidation","held-epoch"))
EVENTS |= frozenset(("guest-stimulus","stimulus-worker","pressure-release"))
EVENTS |= frozenset(("throughput","retention"))
EVENTS |= frozenset(("mapping-offset",))
EVENTS |= frozenset(("seed-delay",))
EVENTS |= frozenset(("stress-begin",))
EVENTS |= frozenset(("participants",))
EVENTS |= frozenset("stage setup-plan setup-summary setup-call-witness scratch-reserve scratch-discard scratch-reuse allocator-reject pressure-swap pressure-inplace pressure-layout".split())


def percentile(values, fraction):
    require(bool(values), "EMPTY_DISTRIBUTION")
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def clock_result(pairs, calibration, batches, phases, header):
    require(set(pairs) == set(range(10000)), "CLOCK_PAIR_COUNT")
    pre = [pairs[i][0] for i in range(10000)]
    post = [pairs[i][1] for i in range(10000)]
    require(all(type(x) is int and 0 <= x <= MASK for x in pre + post), "CLOCK_RANGE")
    pre99, post99 = percentile(pre, .99), percentile(post, .99)
    require(max(pre99, post99) <= MASK // 20, "CLOCK_THRESHOLD_OVERFLOW")
    before, final = max(100, pre99 * 20), max(100, pre99 * 20, post99 * 20)
    require(set(calibration)=={1,16,256,4096}, "CALIBRATION_SIZES")
    selected = None
    for size in (1, 16, 256, 4096):
        rows = calibration[size]
        require(set(rows) == set(range(64)), "CALIBRATION_COUNT")
        require(all(floor == before for _, floor in rows.values()), "CALIBRATION_THRESHOLD")
        if selected is None and all(elapsed > before for elapsed, _ in rows.values()):
            selected = size
    require(set(batches) == set(range(1000)), "COST_BATCH_COUNT")
    require(selected is not None and all(size == selected for size, _ in batches.values()), "BATCH_SELECTION")
    gross = [batches[i][1] for i in range(1000)]
    require(phases, "COST_PHASES_MISSING")
    unresolved = not max(pre) or not max(post) or min(gross) <= final or any(value <= final for _, value in phases)
    result = dict(status="resolution-unresolved" if unresolved else "qualified",
                  batch_size=selected, pre_threshold=before, final_threshold=final,
                  pre_zero_count=pre.count(0), post_zero_count=post.count(0),
                  pre_zero_fraction=[pre.count(0),10000],post_zero_fraction=[post.count(0),10000],
                  samples_are_nested=True, isolated_API_cost=False)
    if not unresolved:
        numerator, denominator = header["numer"], header["denom"]
        require(numerator > 0 and denominator > 0, "TIMEBASE_ZERO")
        # Python integers do not overflow; conversion stays an exact rational.
        result["gross_ticks"] = dict(p50=percentile(gross,.5), p99=percentile(gross,.99), maximum=max(gross))
        result["gross_ns_rationals"] = {key: [value*numerator,denominator] for key,value in result["gross_ticks"].items()}
        result["divide_by_batch_ns_rationals"] = {key: [value*numerator,denominator*selected] for key,value in result["gross_ticks"].items()}
        groups=collections.defaultdict(list)
        for phase,value in phases: groups[phase].append(value)
        result["phase_ticks"]={phase:dict(count=len(values),p50=percentile(values,.5),p99=percentile(values,.99),maximum=max(values)) for phase,values in groups.items()}
    return result


def check(path, job):
    header = None
    sequence = collections.Counter()
    descriptors, built, requests, retired, published = {}, set(), {}, set(), set()
    nonces, built_pairs, code_words = {}, {}, collections.defaultdict(dict)
    rounds, done, calls = {}, {}, collections.Counter()
    generations = collections.defaultdict(set)
    complete, allocation = None, None
    hook_arrivals = hook_releases = reuse_count = 0
    ack = collections.defaultdict(dict)
    resumes = collections.defaultdict(set)
    coordinator_pairs = {}
    worker_pairs = collections.defaultdict(set)
    calibration = collections.defaultdict(dict)
    batches, phases = {}, []
    throughput, retention = None, {}
    mapping_seen = collections.defaultdict(set)
    unresolved = None
    stimuli, remote = {}, {}
    owned_map, arena = None, None
    setup_plan, setup_summary = None, None
    setup_witnesses=collections.defaultdict(set)
    pressure_holds, pressure_layout, pressure_swaps, releases = {}, {}, [], {}
    retire_epochs={}
    stress_start={}
    restored_threads=collections.defaultdict(set)
    mapping_epochs=set()
    coordinator_epoch=0
    participants=None
    qos_actors=set()
    held_retirements,held_revalidations,retried = [],[],set()
    scratch_reserved, scratch_discarded, scratch_reuses, rejected = {}, set(), [], []
    active_grant=None
    for event in records(path):
        kind = event["kind"]
        if kind == "header":
            require(header is None and not sequence, "HEADER_DUPLICATE_OR_LATE")
            header = event
            require(header.get("investigation") == "INV-01-18" and header.get("method") == "M18-2", "IDENTITY")
            for key in ("cell", "mode", "n", "seed", "signing", "size"):
                require(header.get(key) == job[key], "IDENTITY_" + key.upper())
            for key in ("negative","hook","baseline"):
                require(header.get(key,"") == job.get(key,""), "IDENTITY_" + key.upper())
            require(header.get("arm") == job["arm"][0], "IDENTITY_ARM")
            prologue=20 if job.get("hook") else 0
            offset={"normal":0,"cache-boundary":48-prologue,"page-boundary":header["page_size"]-16-prologue}[job.get("layout","normal")]
            require(header.get("code_offset")==offset, "CHANGED_FIELD_BOUNDARY")
            continue
        require(header is not None, "HEADER_MISSING")
        require(kind in EVENTS, "UNKNOWN_EVENT")
        actor, seq, v = event.get("actor"), event.get("seq"), event.get("v")
        require(type(actor) is int and 0 <= actor < 13, "ACTOR")
        require(seq == sequence[actor], "TRACE_SEQUENCE")
        sequence[actor] += 1
        require(isinstance(v,list) and len(v)==6 and all(type(x) is int and 0 <= x <= MASK for x in v), "EVENT_VALUES")
        if kind == "failure":
            raise Invalid("NATIVE_INVARIANT")
        if kind == "qos":
            require(actor not in qos_actors, "QOS_DUPLICATE")
            qos_actors.add(actor)
        if kind == "participants":
            require(participants is None, "PARTICIPANTS_DUPLICATE")
            participants=v
        if kind in ("unavailable", "resolution-unresolved", "capacity-unresolved"):
            unresolved = kind
        elif kind == "allocation-plan":
            require(allocation is None, "ALLOCATION_DUPLICATE")
            allocation = v
        elif kind == "owned-map":
            require(owned_map is None and v[0]%header["page_size"]==0 and v[1]==job.get("pages",1)*header["page_size"], "OWNED_MAPPING")
            owned_map=v
        elif kind == "capability":
            require(arena is None and v[1]%header["page_size"]==0 and v[2]==16<<20, "ARENA_IDENTITY")
            arena=v
        elif kind == "setup-plan":
            require(setup_plan is None, "SETUP_PLAN_DUPLICATE")
            setup_plan=v
        elif kind == "setup-summary":
            require(setup_summary is None, "SETUP_SUMMARY_DUPLICATE")
            setup_summary=v
        elif kind == "setup-call-witness":
            require(v[1] not in setup_witnesses[v[0]] and v[1]<header["n"], "SETUP_WITNESS_DUPLICATE")
            setup_witnesses[v[0]].add(v[1])
        elif kind == "pressure-hold":
            require(v[0] in retired and v[0] in published and v[0] not in pressure_holds, "PRESSURE_NOT_GENUINELY_RETIRED")
            if v[2]==0:
                require(v[0] in setup_witnesses, "PRESSURE_HOLD_BEFORE_CALL_WITNESS")
            pressure_holds[v[0]]=v
        elif kind == "pressure-release":
            require(v[0] in pressure_holds and v[0] not in releases, "PRESSURE_RELEASE_IDENTITY")
            releases[v[0]]=v
        elif kind == "pressure-layout":
            require(v[0] not in pressure_layout, "PRESSURE_LAYOUT_DUPLICATE")
            pressure_layout[v[0]]=v
        elif kind == "scratch-reserve":
            require(v[0] not in scratch_reserved and v[3]==0, "SCRATCH_RESERVATION")
            scratch_reserved[v[0]]=v
        elif kind == "scratch-discard":
            require(v[0] in scratch_reserved and v[0] not in scratch_discarded, "SCRATCH_DISCARD")
            scratch_discarded.add(v[0])
        elif kind == "scratch-reuse":
            require(v[0] in scratch_discarded and v[1] in scratch_reserved and v[0]!=v[1], "SCRATCH_ABA")
            scratch_reuses.append(v)
        elif kind == "allocator-reject":
            require(v[0] in scratch_reserved and v[0] not in scratch_discarded and v[2:4]==[0,1], "ALLOCATOR_REJECTION_CAUSE")
            rejected.append(v)
        elif kind == "pressure-swap":
            require(v[0] in releases and v[1] in published and v[2] in pressure_holds and v[4] in scratch_reserved and v[4] not in scratch_discarded, "PRESSURE_SWAP_CHAIN")
            pressure_swaps.append(v)
        elif kind == "descriptor":
            token, slot, version, owner, page, _ = v
            require(token and token not in descriptors and page < 2 and owner < header["n"], "DESCRIPTOR_IDENTITY")
            require(slot * header["page_size"] * 2 < 16 << 20, "SLOT_RANGE")
            descriptors[token] = (slot, version, owner, page)
            nonces[token]=v[5]
        elif kind == "built":
            require(v[0] not in built, "BUILD_DUPLICATE")
            built_pairs[v[0]]=tuple(v[4:6])
            built.add(v[0])
        elif kind == "code-word":
            require(v[1] not in code_words[v[0]], "CODE_WORD_DUPLICATE")
            code_words[v[0]][v[1]]=v[2]
        elif kind == "request":
            require(v[0] not in requests and v[1] < 2 and v[3] in (0,1), "REQUEST_IDENTITY")
            requests[v[0]] = tuple(v[1:4])
            coordinator_epoch=v[0]
        elif kind == "restore-thread":
            require(actor not in restored_threads[v[0]], "RESTORE_THREAD_DUPLICATE")
            restored_threads[v[0]].add(actor)
        elif kind == "guest-stimulus":
            require(v[0] not in stimuli and v[1]==(v[0]-1)%4 and v[2]<2, "GUEST_STIMULUS_IDENTITY")
            require(v[4]==v[3]+(v[1]==2), "GUEST_BACKING_TRANSITION")
            stimuli[v[0]]=v
        elif kind == "stimulus-worker":
            require(v[0] not in remote and actor==v[4]+1 and v[3]==v[2]+1, "REMOTE_WRITE_WITNESS")
            remote[v[0]]=v
        elif kind == "invalidate-complete":
            require(v[3]==v[4], "UNRELATED_PAGE_INVALIDATED")
        elif kind == "ack":
            require(1 <= actor <= header["n"] and v[0] not in ack[actor], "ACK_IDENTITY")
            ack[actor][v[0]] = tuple(v[1:3])
        elif kind == "resume":
            require(v[0] not in resumes[actor], "RESUME_DUPLICATE")
            resumes[actor].add(v[0])
        elif kind in ("retire", "retire-unpublished", "translation-rejected"):
            require(v[0] not in retired, "DOUBLE_RETIREMENT")
            retired.add(v[0])
            if kind=="retire": retire_epochs[v[0]]=v[2]
        elif kind == "grant":
            require(active_grant is None, "DOUBLE_WRITER_GRANT")
            active_grant=tuple(v[:3])
        elif kind == "grant-complete":
            require(active_grant==tuple(v[:3]), "GRANT_COMPLETION_IDENTITY")
            active_grant=None
        elif kind == "publish":
            require(v[0] not in published, "TOKEN_REPUBLISHED")
            published.add(v[0])
        elif kind == "reuse":
            require(v[0] != v[1], "TOKEN_ABA")
            require(v[0] in retired, "REUSE_BEFORE_RETIRE_RECORD")
            reuse_count += 1
        elif kind == "round":
            require(v[0] not in rounds, "ROUND_DUPLICATE")
            rounds[v[0]] = v
        elif kind == "seed-delay":
            require(v[1]==mix(header["seed"]^v[0])&3, "SEEDED_DELAY_PLAN")
        elif kind == "complete":
            require(complete is None, "COMPLETE_DUPLICATE")
            complete = v
        elif kind == "worker-done":
            require(actor not in done, "WORKER_DUPLICATE")
            done[actor] = v
        elif kind == "stress-begin":
            require(v[0]+1 not in stress_start and v[0]<header["n"], "STRESS_START_IDENTITY")
            stress_start[v[0]+1]=v[1]
        elif kind == "hook-arrive":
            hook_arrivals += 1
        elif kind == "hook-release":
            hook_releases += 1
        elif kind == "old-progress":
            require(v[1] > v[0], "VALID_OLD_PROGRESS_MISSING")
        elif kind == "held-retirement":
            require(v[0]==v[1] and v[2]==3, "HELD_RETIRED_EARLY")
            held_retirements.append(v[0])
        elif kind == "held-revalidation":
            require(v[0]!=v[1], "HELD_REVALIDATION_NO_REPLACEMENT")
            held_revalidations.append(v[0])
        elif kind == "retry":
            retried.add(v[0])
        elif kind == "held-epoch":
            require(v[0]>v[1] and v[2]<header["n"] and v[3]==header["n"]-1, "HELD_EPOCH_NOT_ALL_BUT_ONE")
        elif kind == "mapping":
            require(v[4] == 1, "MAPPING_API_RESULT")
            extent=(arena[1:3] if v[0]==3 and arena else owned_map[:2] if owned_map else None)
            require(extent is not None and v[1:3]==extent, "MAPPING_TARGET_OWNERSHIP")
            if v[0] in (2,3):
                require(v[3] == 1 and v[5] == 0, "MAPPING_PROTECTION_RESULT")
            if v[0]==3: mapping_epochs.add(coordinator_epoch)
        elif kind == "mapping-offset":
            require(owned_map is not None and v[0]==(v[1]-1)*v[2] and v[1] in (1,2) and v[2]==owned_map[1] and v[3]==owned_map[0] and v[4]==0, "MAPPING_OFFSET_RESULT")
        elif kind == "mapping-witness":
            require(v[1] == v[2], "MAPPING_CONTENTS")
            mapping_seen[actor].add(v[2])
        elif kind == "coordinator-pair":
            require(actor == 0 and v[0] not in coordinator_pairs, "CLOCK_PAIR_IDENTITY")
            coordinator_pairs[v[0]] = tuple(v[1:3])
        elif kind == "worker-pair":
            require(v[0] not in worker_pairs[actor], "WORKER_CLOCK_DUPLICATE")
            worker_pairs[actor].add(v[0])
        elif kind == "calibration":
            require(v[1] not in calibration[v[0]], "CALIBRATION_DUPLICATE")
            calibration[v[0]][v[1]] = (v[2],v[3])
        elif kind == "cost-batch":
            require(v[0] not in batches, "COST_BATCH_DUPLICATE")
            batches[v[0]] = (v[1],v[2])
        elif kind == "cost-phase":
            require(v[4] >= v[3] and v[2] == v[4]-v[3], "CLOCK_BACKWARD_OR_INTERVAL")
            # Inclusion is fixed by the preregistered warmup round boundary,
            # never by an interval's value. Warmup records remain in raw data.
            if v[1]>=1027:
                phases.append((v[0],v[2]))
        elif kind == "retention":
            require(v[0] not in retention and max(v[1:4])<=16<<20 and v[3]>=v[1], "RETENTION_RANGE")
            retention[v[0]]=v
        elif kind == "throughput":
            require(throughput is None and v[1]>=v[0], "THROUGHPUT_INTERVAL")
            throughput=v
    require(header is not None, "EMPTY_TRACE")
    if unresolved:
        return dict(status=unresolved, completed_question=False, evidence=False)
    require(complete is not None and allocation is not None, "INCOMPLETE")
    require(active_grant is None, "GRANT_NOT_COMPLETED")
    require(allocation[2] <= allocation[3] == 256 << 20, "ALLOCATION_CAP")
    require(allocation[4:6]==[24,16], "METADATA_LAYOUT")
    expected_workers = set(range(1,header["n"]+1))
    require(set(done) == expected_workers, "WORKERS_MISSING")
    producer_count=2 if job.get("producers",1)==2 else 0
    require(participants==[header["n"],1,1,producer_count,int(bool(job.get("hook"))),0], "PARTICIPANT_COUNT")
    expected_roles=expected_workers|{0,12}|({9,10} if producer_count else set())|({11} if job.get("hook") else set())
    require(qos_actors==expected_roles, "PARTICIPANT_QOS_MISSING")
    require(set(stress_start)==expected_workers, "STRESS_START_MISSING")
    if job["arm"]=="M-jit-protect" and not job.get("baseline"):
        participating=expected_workers|{0}|({9,10} if job.get("producers",1)==2 else set())
        require(mapping_epochs and all(participating<=restored_threads[e] for e in mapping_epochs), "JIT_PARTICIPANT_RESTORATION_MISSING")
    control = job.get("kind") in ("hook", "negative", "preflight", "sensitivity")
    expected_warmup, expected_publications, expected_floor = (4,32,128) if control else (1024,4096,16384)
    if header["mode"] == "cost":
        expected_publications = 1000 * complete[3]
    require(complete[:3] == [expected_warmup,expected_publications,expected_floor], "SAMPLE_PLAN")
    require(set(rounds) == set(range(expected_warmup+expected_publications)), "PUBLICATION_FLOOR")
    require(set(retention)==set(rounds), "RETENTION_SAMPLE_COVERAGE")
    expected_stimuli=range(1,math.ceil((expected_warmup+expected_publications)/job["rate"])+1)
    require(set(stimuli)==set(expected_stimuli), "GUEST_STIMULUS_COVERAGE")
    for sequence,v in stimuli.items():
        if v[1]==1:
            require(sequence in remote and remote[sequence][1]==v[2] and remote[sequence][4]+1==v[5], "REMOTE_WRITE_MISSING")
    require(published <= built <= descriptors.keys(), "UNBUILT_PUBLICATION")
    require(set(descriptors)==set(range(1,complete[4]+1)), "DESCRIPTOR_SEQUENCE")
    owners=header["n"] if header["arm"]=="V" else 1
    pressure=job.get("pressure","normal")
    slots=(16<<20)//(2*header["page_size"])
    held_slots={s for s in range(2*owners+1,slots) if pressure=="near-full" or pressure=="fragmented" and s%2}
    held_count=len(held_slots)
    scratch_count=2 if pressure=="near-full" else 0
    replacement_count=int(pressure=="near-full" and header["arm"] not in "SM")
    setup_publications=held_count+replacement_count
    require(len(published)==(expected_warmup+expected_publications+2)*owners+setup_publications, "PUBLICATION_COUNT")
    require(not (published & scratch_reserved.keys()) and not (built & scratch_reserved.keys()), "SCRATCH_ACQUIRED_EXECUTION_RIGHT")
    if pressure!="normal":
        require(setup_plan is not None and setup_plan[:4]==[held_count,scratch_count,replacement_count,2*held_count+2*replacement_count], "SETUP_CAP_ACCOUNTING")
        require(setup_summary is not None and setup_summary[:3]==[held_count,setup_publications,scratch_count], "PRESSURE_SETUP_FLOOR")
        originals={token for token,v in pressure_holds.items() if v[2]==0}
        require(len(originals)==held_count and {descriptors[t][0] for t in originals}==held_slots, "PRESSURE_POPULATION_SET")
        for token in originals:
            request=requests.get(retire_epochs.get(token))
            require(request is not None and request[0]==descriptors[token][3] and request[1]>descriptors[token][1], "PRESSURE_REMOVAL_DRAIN_MISSING")
        require(len(setup_witnesses)==setup_publications and originals<=setup_witnesses.keys(), "PRESSURE_CALL_WITNESS_MISSING")
        require(set(pressure_layout)==set(range(slots)), "PRESSURE_LAYOUT_INCOMPLETE")
        final_held={v[1] for v in pressure_layout.values() if v[2]}
        require(len(final_held)==held_count and all(v[3]==4 for v in pressure_layout.values() if v[2]), "PRESSURE_FINAL_RETAINED_SET")
        require(len(scratch_reserved)==scratch_count and scratch_discarded==scratch_reserved.keys(), "SCRATCH_COUNT_OR_TAIL")
        if pressure=="near-full":
            require(setup_summary[4]==1 and len(scratch_reuses)==1 and len(rejected)==2, "NEAR_FULL_REJECTION_AND_RESTORE")
            require(len(pressure_swaps)==replacement_count, "REAL_PRESSURE_SWAP_MISSING")
            if replacement_count:
                released,new,old,owner,scratch,_=pressure_swaps[0]
                require(final_held==(originals-{released})|{old}, "PRESSURE_RETAINED_SET_SWAP")
                require(descriptors[released][0]==descriptors[new][0] and owner==descriptors[new][2] and new in setup_witnesses, "PRESSURE_REAL_REUSE")
                if header["arm"]=="V":
                    require(descriptors[released][2]==owner, "PRESSURE_V_OWNER_TRANSFER")
            else:
                require(final_held==originals, "INPLACE_PRESSURE_SET_CHANGED")
    for token in built:
        require(built_pairs[token] == pair(token,header["seed"],nonces[token]), "BUILD_GENERATION")
    if not job.get("baseline"):
        require(code_words, "CODE_WORDS_MISSING")
        for token, words in code_words.items():
            require(words==instruction_words(pair(token,header["seed"],nonces[token]),header["size"],bool(job.get("hook"))), "CODE_INSTRUCTIONS")
    require(reuse_count > 0, "REUSE_CONTROL_MISSING")
    require(set(requests) == set(range(1,complete[5]+1)), "EPOCH_SEQUENCE")
    for actor in expected_workers:
        require(set(ack[actor]) == set(requests), "ACK_MISSING_OR_STALE")
        for epoch,(page,floor,stop) in requests.items():
            require(ack[actor][epoch] == (page,floor), "ACK_PAGE_FLOOR")
            if stop:
                require(epoch in resumes[actor], "RESUME_MISSING")
    pending = {}
    reader_epochs=collections.Counter()
    resumed_epochs=collections.Counter()
    reader_floors=collections.defaultdict(lambda:[0,0])
    actual_setup_calls=collections.defaultdict(set)
    for event in records(path):
        if event["kind"] == "header":
            continue
        kind, actor, v = event["kind"], event["actor"], event["v"]
        if kind == "ack":
            require(v[0] > reader_epochs[actor], "ACK_REORDERED")
            reader_epochs[actor]=v[0]
            reader_floors[actor][v[1]]=v[2]
        elif kind == "resume":
            require(v[0]==reader_epochs[actor], "RESUME_WRONG_EPOCH")
            resumed_epochs[actor]=v[0]
        elif kind == "admit":
            require(actor in expected_workers and actor not in pending, "ADMISSION_IDENTITY")
            require(v[0] in descriptors and v[1] in (2,3), "RETIRED_ADMISSION")
            slot,version,owner,page = descriptors[v[0]]
            require(page == v[3] and version >= v[4], "STALE_ADMISSION")
            require(v[2] == reader_epochs[actor] and v[4] == reader_floors[actor][page], "POST_ACK_RELOAD_FLOOR")
            if v[2] and requests[v[2]][2]:
                require(resumed_epochs[actor]==v[2], "ADMISSION_BEFORE_RESUME")
            require(header["arm"] != "V" or owner == actor-1, "V_WRONG_WORKER")
            require(header["arm"] != "I" or v[5] == v[0], "I_UNPROTECTED_ADMISSION")
            pending[actor] = v[0]
        elif kind == "call":
            token = pending.pop(actor,None)
            require(token == v[0] and token in published, "CALL_WITHOUT_ADMISSION")
            slot,version,_,_ = descriptors[token]
            require(v[1] == version and v[5] == slot, "CALL_DESCRIPTOR")
            require(tuple(v[2:4]) == pair(token,header["seed"],nonces[token]), "CALL_GENERATION")
            calls[actor] += 1
            generations[actor].add(token)
            if token in setup_witnesses or token in pressure_holds:
                actual_setup_calls[token].add(actor-1)
        elif kind == "reuse":
            require(v[0] in retired and v[0] in descriptors and v[1] in descriptors, "REUSE_NOT_RETIRED")
            require(descriptors[v[0]][0] == descriptors[v[1]][0] == v[2], "REUSE_ADDRESS")
            old,new=(pair(t,header["seed"],nonces[t]) for t in v[:2])
            require(all((a>>(16*h))&65535 != (b>>(16*h))&65535 for a,b in zip(old,new) for h in range(4)), "UNCHANGED_IMMEDIATE_FIELD")
    require(not pending, "INCOMPLETE_CALL")
    for token,witnessed in setup_witnesses.items():
        intended={descriptors[token][2]} if header["arm"]=="V" else set(range(header["n"]))
        require(witnessed==intended and actual_setup_calls[token]==intended, "SETUP_ACTUAL_CALLS_MISSING")
    for token in pressure_holds:
        intended={descriptors[token][2]} if header["arm"]=="V" else set(range(header["n"]))
        require(actual_setup_calls[token]==intended, "RETAINED_GENERATION_NEVER_EXECUTED")
    for actor in expected_workers:
        require(calls[actor] == done[actor][0] and calls[actor] >= expected_floor, "CALL_FLOOR")
        require(calls[actor]-stress_start[actor]>=expected_floor, "POST_WARMUP_CALL_FLOOR")
        require(len(generations[actor]) >= 2, "GENERATION_POSITIVE_CONTROL")
        if job["arm"] == "M-data-map" and not job.get("baseline"):
            require(mapping_seen[actor] == {1,2}, "MAPPING_OLD_NEW_CONTROL")
    if job.get("hook"):
        require(hook_arrivals == hook_releases == 1, "HOOK_COVERAGE")
        if job["arm"]=="I" and job["rate"]==64:
            hook=job["hook"]
            if hook=="reader-after-load":
                require(len(held_revalidations)==1 and held_revalidations[0] in retried, "HELD_RETRY_MISSING")
            elif hook in ("reader-after-hazard","reader-after-validation","reader-in-call","reader-after-call"):
                require(len(held_retirements)==1, "PROTECTED_SUPERSEDED_WITNESS_MISSING")
                if hook=="reader-after-hazard": require(held_retirements[0] in retried, "HELD_RETRY_MISSING")
    else:
        require(hook_arrivals == hook_releases == 0, "UNEXPECTED_HOOK")
    result = dict(status="correctness-qualified", calls=dict(calls), generations={k:len(v) for k,v in generations.items()},
                  post_warmup_calls={actor:calls[actor]-stress_start[actor] for actor in expected_workers},
                  reuse_count=reuse_count, empirical_only=True, completed_question=False)
    if header["mode"] == "cost":
        require(all(worker_pairs[a] == set(range(10000)) for a in expected_workers), "WORKER_CLOCK_COUNT")
        result["cost"] = clock_result(coordinator_pairs,calibration,batches,phases,header)
        require(throughput is not None and throughput[3]==expected_publications*(header["n"] if header["arm"]=="V" else 1), "THROUGHPUT_COUNT")
        if throughput[1]-throughput[0]<=result["cost"]["final_threshold"]:
            result["cost"]={"status":"resolution-unresolved","reason":"throughput interval below fixed threshold"}
        elif result["cost"]["status"]=="qualified":
            elapsed_ns=(throughput[1]-throughput[0])*header["numer"]
            result["cost"]["throughput_rationals_per_second"]={"calls":[throughput[2]*header["denom"]*10**9,elapsed_ns],"publications":[throughput[3]*header["denom"]*10**9,elapsed_ns]}
            result["cost"]["peak_retired_unreclaimed_bytes"]=throughput[4]
    if header["mode"] == "sensitivity":
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "No observed mismatch does not negate cache/ISB obligations."
    return result


def instruction_words(values,size,hook):
    """Independent instruction-field encoding used only for artifact checking."""
    words={i:0xD503201F for i in range(size//4)}
    prologue=[0xA9BF7BFD,0xAA0003F0,0xAA0103E0,0xD63F0200,0xA8C17BFD] if hook else []
    for i,word in enumerate(prologue):
        words[i]=word
    for reg,value in enumerate(values):
        for half in range(4):
            words[len(prologue)+4*reg+half]=(0xD2800000 if half==0 else 0xF2800000)|(half<<21)|(((value>>(16*half))&65535)<<5)|reg
    words[size//4-1]=0xD65F03C0
    return words


if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("trace",type=Path)
    parser.add_argument("job",type=Path)
    args=parser.parse_args()
    try:
        print(json.dumps(check(args.trace,json.loads(args.job.read_text())),sort_keys=True))
    except Invalid as exc:
        print(json.dumps(dict(status="INVALID",invariant=str(exc))))
        raise SystemExit(1)
