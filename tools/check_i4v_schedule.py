#!/usr/bin/env python3
"""Independent seeded-decision replay, bound to actual protocol trace cuts."""
from i4v_check import exact, require, uint

SEEDS = (1, 37, 0x12345678, 0x9e3779b9)
NONE = (1 << 64) - 1
MAGIC = 0x5343484544000001
ADMIT, DETACH, ACK, RELEASE, MAINTENANCE_ACK, SOURCE_ACK, SOURCE_RELEASE = (
    i << 32 for i in range(1, 8))


def configuration(family, n, ordinal):
    for value in (family, n, ordinal): uint(value, 32, 'requested configuration')
    require(family <= 4 and 1 <= n <= 4 and ordinal < (2 if family == 4 else 4),
            'bounded declared scheduling matrix')
    return dict(family=family, n=n, actor=(family+ordinal) % n,
                seed_ordinal=ordinal, seed=SEEDS[ordinal],
                token_seed=max(1,SEEDS[ordinal]&0xffff) if family==3 else 0,
                pool_limit=1+ordinal % min(2, n-1) if family < 2 and n > 1 else n)


def random_step(value):
    # Explicit 32-bit arithmetic, independent of the resident Rust helper.
    value = (value ^ (value << 13)) & 0xffffffff
    value ^= value >> 17
    return (value ^ (value << 5)) & 0xffffffff


def decisions(record, expected_config, trace, contract, required_cuts, *, prefix=None):
    """contract(key, trace_position, done) -> cohort, eligible, phase, epoch, next event.

    The callback derives these from the underlying architectural replay. Neither
    an arbitrary reported mask nor a self-consistent PRNG log proves eligibility.
    A containment caller may supply exact independently proven final completion
    masks in prefix. This skips healthy termination checks, never draw/claim
    checks, and requires every recorded grant to have actually been consumed.
    """
    exact(set(record), {'case','config','summary','cuts','decisions','pool_events','supervision'}, 'schedule fields')
    exact(record['case'], 'schedule', 'schedule report kind')
    exact(record['config'], expected_config, 'requested seed and owner rotation')
    supervision=record['supervision']
    exact(set(supervision),set('host_pid joined completions destroyed statuses cancellations error elapsed_ms'.split()),'supervisor evidence fields')
    require(uint(supervision['host_pid'],32,'host process')>0,'live host process identity')
    n=expected_config['n']
    if prefix is None:
        exact((supervision['joined'],supervision['completions']),(n,n),'all owner threads complete and join')
        exact(sorted(supervision['destroyed']),[[i,0] for i in range(n)],'all native vCPU destruction calls succeed')
        exact(supervision['statuses'],[[i,1,0] for i in range(n)],'all healthy owners finish successfully')
        exact((supervision['error'],supervision['cancellations']),(None,[]),'healthy run has no hidden cancellation')
    else:
        exact(set(prefix),set(required_cuts),'exact replay-derived frozen cut set')
    uint(supervision['elapsed_ms'],64,'supervision elapsed time')
    summary = record['summary']
    exact(set(summary), set('magic version prng draws cut_count decision_count pool_event_count full_count readmitted full retried'.split()), 'schedule summary fields')
    for key in set(summary) - {'full','retried'}: uint(summary[key],64,key)
    exact((summary['magic'],summary['version']), (MAGIC,1), 'scheduler version')
    for name, bound in (('cuts',256),('decisions',768),('pool_events',128)):
        require(type(record[name]) is list and len(record[name]) <= bound, 'bounded '+name)
        exact(summary[{'cuts':'cut_count','decisions':'decision_count','pool_events':'pool_event_count'}[name]], len(record[name]), name+' count')
    cuts, done, arrived, previous_claim = {}, {}, {}, {}
    rng = expected_config['seed']
    previous_trace = 0
    for draw, d in enumerate(record['decisions']):
        exact(set(d), set('cut phase epoch cohort eligible arrived done_before prng_before prng_after draw chosen claimed trace_before claim_trace_before'.split()), 'decision fields')
        for name, value in d.items(): uint(value,64,'decision '+name)
        key, at, claim = d['cut'], d['trace_before'], d['claim_trace_before']
        require(previous_trace <= at <= claim < len(trace), 'decision and claim positions bind to real trace')
        previous_trace = at
        require(key in required_cuts, 'unexpected scheduling cut')
        require(previous_claim.get(key,-1) < at, 'prior grant must finish before next decision')
        completed = done.get(key,0)
        cohort, eligible, phase, epoch, _ = contract(key,at,completed)
        eligible &= ~completed
        exact((d['cohort'],d['eligible'],d['phase'],d['epoch'],d['done_before']),
              (cohort,eligible,phase,epoch,completed), 'independently derived eligibility and phase')
        require(eligible != 0 and d['arrived'] != 0 and not d['arrived'] & ~cohort
                and arrived.get(key,0) & ~d['arrived'] == 0, 'monotonic valid arrivals')
        exact((d['draw'],d['prng_before']), (draw,rng), 'PRNG continuity')
        rng = random_step(rng)
        choices = [owner for owner in range(expected_config['n']) if eligible & (1 << owner)]
        chosen = choices[rng % len(choices)]
        exact((d['prng_after'],d['chosen'],d['claimed']), (rng,chosen,1), 'seeded choice consumed exactly once')
        claim_cohort, claim_eligible, _, _, action = contract(key,claim,completed)
        exact(claim_cohort,cohort,'claim cohort remains valid')
        require(claim_eligible & (1 << chosen), 'chosen owner remains protocol eligible')
        exact(trace[claim], (action << 8) | chosen, 'grant immediately precedes its real operation')
        done[key] = completed | (1 << chosen)
        arrived[key] = d['arrived'] | (1 << chosen)
        previous_claim[key] = claim
        cuts[key] = cohort
    exact(set(cuts),set(required_cuts),'all required scheduling cuts observed')
    exact((summary['prng'],summary['draws']), (rng,len(record['decisions'])), 'final PRNG state')
    observed = {}
    for c in record['cuts']:
        exact(set(c),set('key cohort arrived completed chosen decision'.split()),'cut fields')
        for key,value in c.items(): uint(value,64,'cut '+key)
        require(c['key'] not in observed and c['key'] in cuts,'unique known cut')
        cohort = cuts[c['key']]
        completed = cohort if prefix is None else uint(prefix[c['key']],64,'frozen completed cohort')
        require(completed!=0 and completed&~cohort==0,'nonempty completed subset of actual cohort')
        exact(arrived[c['key']],completed,'no unconsumed arrival remains at this admitted terminal boundary')
        exact(c,dict(key=c['key'],cohort=cohort,arrived=completed,completed=completed,chosen=NONE,decision=NONE), 'every issued cohort grant consumed')
        exact(done[c['key']],completed,'every required owner scheduled before the frozen boundary')
        observed[c['key']] = c
    exact(set(observed),set(cuts),'complete cut ledger')
    return len(record['decisions'])


def coherence(schedule, report, expected_config):
    import check_i4v_coherence as native
    cfg = expected_config
    exact(cfg['family'],2,'coherence scheduling family')
    profile = (0,4,0,5)[cfg['seed_ordinal']]
    native.check(report,profile,cfg['n'],cfg['actor'])
    trace = report['coordinator']['trace']
    model = native.Replay(report)
    model.terminal_probe_required = True
    model.terminal_probes = 0
    snapshots = []
    for word in trace:
        snapshots.append((model.phase,model.index,model.mutations,model.acks,model.released))
        model.step(word)
    all_owners = (1 << cfg['n'])-1
    def contract(key, at, completed):
        phase,index,mutation,acks,released = snapshots[at]
        kind, value = key >> 32, key & 0xffffffff
        eligible = all_owners
        if kind == 1:
            exact(value,index,'admission visit')
            epoch,action = index,0x1000+index*8
        elif kind == 2:
            exact(value,index,'retained fault visit')
            require(index in (2,3,6,7),'retained-route fault cut')
            epoch,action = index,2
        elif kind == 3:
            exact(value,mutation,'mutation acknowledgement')
            epoch,action = mutation,65
            delayed=(cfg['actor']+cfg['n']-1)%cfg['n']
            early=all_owners ^ (1 << delayed)
            eligible=all_owners if acks & early == early else early
        elif kind == 4:
            exact((value,index),(0,16),'terminal release cut')
            epoch,action = 16,69
            early=all_owners ^ (1 << cfg['actor'])
            eligible=all_owners if released & early == early else early
        else: raise ValueError('unknown coherence scheduling cut')
        return all_owners,eligible,phase,epoch,action
    required = {ADMIT|i for i in range(16)} | {DETACH|i for i in (2,3,6,7)} | {ACK|i for i in (1,2,3)} | {RELEASE}
    count = decisions(schedule,cfg,trace,contract,required)
    exact(schedule['pool_events'],[],'coherence has no reduced maintenance pool')
    for name in ('full_count','readmitted'): exact(schedule['summary'][name],0,name)
    for name in ('full','retried'): exact(schedule['summary'][name],[0]*4,name)
    return dict(decisions=count,capacity_exhaustion='not applicable to the all-owner retained-route barrier')


def check(records, expected_config=None):
    require(type(records) is list and len(records)==2,'one scheduling and one native report')
    schedule, native = records
    reported=schedule['config']
    cfg=configuration(reported['family'],reported['n'],reported['seed_ordinal'])
    if expected_config is not None: exact(cfg,expected_config,'requested scheduling cell')
    if cfg['family'] in (0,1):
        import check_i4v_progress_protocols as protocols
        return (protocols.stack if cfg['family']==0 else protocols.controls)(schedule,native,cfg)
    if cfg['family']==2: return coherence(schedule,native,cfg)
    if cfg['family']==3:
        import check_i4v_atomic_progress as atomic
        return atomic.atomic(schedule,native,cfg)
    if cfg['family']==4: return partial(schedule,native,cfg)
    raise ValueError('scheduling family has no completed architectural oracle yet')


def partial(schedule,report,cfg):
    import check_i4v_partial_progress as helper
    model=helper.check(report,cfg)
    trace=report['coordinator']['trace'];all_owners=(1<<cfg['n'])-1
    def contract(key,at,done):
        phase,acks,opened=model.snapshots[at]
        kind,value=key>>32,key&0xffffffff
        eligible=all_owners
        if kind==1:
            exact(value,int(bool(opened)),'helper/probe admission epoch')
            epoch,action=(2,1) if opened else (1,0x1000)
        elif kind==2:
            exact(value,0,'helper fault cut');epoch,action=1,2
        elif kind==3:
            exact(value,0,'helper mutation acknowledgement cut');epoch,action=2,0x1003
            early=all_owners ^ (1<<((cfg['actor']+cfg['n']-1)%cfg['n']))
            eligible=all_owners if acks&early==early else early
        elif kind==4:
            exact(value,0,'helper route release cut');epoch,action=1,146
        else:raise ValueError('unknown partial scheduling cut')
        return all_owners,eligible,phase,epoch,action
    count=decisions(schedule,cfg,trace,contract,{ADMIT,ADMIT|1,DETACH,ACK,RELEASE})
    exact(schedule['pool_events'],[],'partial helper uses all-owner retained-route barrier')
    for name in ('full_count','readmitted'):exact(schedule['summary'][name],0,name)
    for name in ('full','retried'):exact(schedule['summary'][name],[0]*4,name)
    return dict(decisions=count,helper_elements=2*cfg['n'],probe_stores=cfg['n'])


def verify_producer(records, directory):
    from pathlib import Path
    import i4v_coherence_worker as worker
    directory=Path(directory)
    if records[0]['config']['family'] in (0,1):
        import check_i4v_progress_protocols as protocols
        return protocols.verify_producer(records,directory)
    if records[0]['config']['family']==4:
        require(not directory.exists() or not list(directory.iterdir()),'semantic helper must not claim compiler work')
        return []
    if records[0]['config']['family']==3:
        import i4v_ordering_worker as ordering
        result=[]
        for owner in records[1]['owners']:
            root=directory/f"cpu-{owner['id']}"
            exact(owner['compiler_requests'],[owner['request']],'one actual atomic compiler request')
            ordering.verify_compilations(root,[owner['request']],[owner['reply']])
            path=root/'request-000'
            result.append((path,ordering.verify_request(path,owner['request'])))
        exact(len(list(directory.glob('cpu-*'))),records[0]['config']['n'],'one compiler root per atomic owner')
        return result
    require(records[0]['config']['family']==2,'implemented producer association family')
    result=[]
    for owner in records[1]['owners']:
        root=directory/f"cpu-{owner['id']}"
        exact(owner['compiler_requests'],owner['requests'],'actual compiler requests')
        exports=worker.verify_compilations(root,owner['requests'],owner['replies'])
        result.extend((root/f'request-{index:03}',export) for index,export in enumerate(exports))
    exact(len(list(directory.glob('cpu-*'))),records[0]['config']['n'],'one compiler root per owner')
    return result


def main():
    import argparse
    import json
    from pathlib import Path
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports',type=Path,nargs='+')
    args=parser.parse_args()
    for path in args.reports:
        result=check([json.loads(line) for line in path.read_text().splitlines() if line.strip()])
        print(f'{path}: PASS {result}')


if __name__=='__main__': main()
