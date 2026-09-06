"""Independently reconstruct hold/competitor/teardown partial orders."""
from check import Invalid, parse, by_kind, two_pass_check, _rows_to_text

EXTRA={'hold_configuration','hold_enter','hold_release','hold_exit','hold_competitor',
       'teardown_request','teardown_deferred','fault_attempt','join_attempt','received_message'}

def check_hold(text,job):
    try:return _check(parse(text),job)
    except (Invalid,KeyError,TypeError,ValueError,IndexError) as exc:return ['HOLD_RECORDS:'+str(exc)]

def _check(rows,job):
    failures=two_pass_check(_rows_to_text([r for r in rows if r['kind'] not in EXTRA]))
    def need(ok,why):
        if not ok:failures.append(why)
    def one(kind,subset=None):
        found=by_kind(rows if subset is None else subset,kind)
        if len(found)!=1:raise Invalid(kind.upper()+'_COUNT')
        return found[0]
    pos={id(r):i for i,r in enumerate(rows)}
    def order(*items):need(all(pos[id(a)]<pos[id(b)] for a,b in zip(items,items[1:])),'HOLD_ORDER')
    meta=one('meta');point=job['hold']['hold'];worker=job['hold']['worker'];n=meta['n']
    need(len(by_kind(rows,'received_message'))==int(point=='server-after-receive'),'RECEIVE_BOUNDARY_COUNT')
    config=one('hold_configuration');enter=one('hold_enter');release=one('hold_release');leave=one('hold_exit');competitor=one('hold_competitor')
    need(config==dict(kind='hold_configuration',point=point,worker=worker,controller_threads=1),'HOLD_CONFIGURATION')
    need(all(r['worker']==worker and r['point']==point for r in (enter,release,leave)) and competitor['worker']==worker,'HELD_IDENTITY')
    need(leave['event']==enter['event'],'HELD_EVENT_IDENTITY')
    order(config,enter,competitor,release,leave)
    total=meta['warmup']+meta['checked'];teardown=point in ('teardown-before-restore','before-right-disposal')
    held=[r for r in rows if r.get('worker')==worker and r.get('event')==0]
    def event(kind,role=None):
        return one(kind,[r for r in held if role is None or r.get('role')==role])
    def restore():return one('teardown_restore',[r for r in rows if r.get('worker')==worker and r.get('label')=='restored-initial'])
    attempts=by_kind(rows,'fault_attempt')
    expected={(w,e) for w in range(n) for e in range(total+(meta['path']=='forward'))}
    need(len(attempts)==len(expected) and {(r['worker'],r['event']) for r in attempts}==expected,'ATTEMPT_COVERAGE')
    for attempt in attempts:
        local=[r for r in rows if (r.get('worker'),r.get('event'))==(attempt['worker'],attempt['event'])]
        arm=one('arm',local);wires=by_kind(local,'wire');order(arm,attempt)
        for wire in wires:order(attempt,wire)
    joins=by_kind(rows,'join_attempt')
    need(len(joins)==n and {r['worker'] for r in joins}==set(range(n)),'JOIN_ATTEMPT_COVERAGE')
    if teardown:
        need(enter['outstanding']==0 and enter['event']==total-1+(meta['path']=='forward'),'TEARDOWN_HELD_STATE')
        need(enter['role']=='worker','HELD_ROLE')
        join=one('join_attempt',[r for r in joins if r['worker']==worker])
        order(enter,join,competitor)
        need(competitor['operation']=='join' and competitor['target']==worker,'TEARDOWN_COMPETITOR')
        need(not by_kind(rows,'teardown_request') and not by_kind(rows,'teardown_deferred'),'EXTRANEOUS_TEARDOWN_REQUEST')
        if point=='teardown-before-restore':order(leave,restore())
        else:
            order(restore(),enter)
            for disposed in by_kind(rows,'disposal'):
                if disposed['worker']==worker and (disposed['right'] in ('send','receive','thread_self','data_page','prior_send','prior_receive') or disposed['right'].startswith(('initial-','saved-prior-'))):order(leave,disposed)
        for disarm in by_kind(rows,'disarm'):order(disarm,enter)
    else:
        need(enter['outstanding']==1 and enter['event']==leave['event']==0,'EVENT_HELD_STATE')
        request=one('teardown_request');deferred=one('teardown_deferred')
        need(request['worker']==deferred['worker']==worker and deferred['outstanding']==1 and deferred['completed']==0,'TEARDOWN_NOT_PENDING')
        order(enter,request,deferred,competitor)
        for restoration in by_kind(rows,'teardown_restore'):order(leave,restoration)
        if n==1:need(competitor['operation']=='teardown' and competitor['target']==worker,'SINGLE_WORKER_COMPETITOR')
        else:
            other=(worker+1)%n
            need(competitor['operation']=='fault' and competitor['target']==other,'FAULT_COMPETITOR')
            attempt=one('fault_attempt',[r for r in attempts if r['worker']==other and r['event']==0])
            order(enter,attempt,competitor)
            independent=meta['topology']=='perworker'
            need(competitor['independent_server'] is independent,'INDEPENDENT_TOPOLOGY')
            need(type(competitor['completed']) is int and competitor['completed'] in (0,1),'COMPETITOR_COMPLETION_COUNT')
            if independent:
                need(competitor['completed']==1,'INDEPENDENT_PROGRESS_MISSING')
            if competitor['completed']==1:
                disarm=one('disarm',[r for r in rows if r.get('worker')==other and r.get('event')==0]);order(disarm,competitor)
            if not independent and point in ('server-after-receive','server-before-repair','server-before-reply','front-before-forward','prior-before-reply'):
                need(not any(r['kind']=='wire' and r.get('role')=='front' and r['worker']!=worker and pos[id(enter)]<pos[id(r)]<pos[id(leave)] for r in rows),'HELD_SERIAL_SERVER_SERVICED_PEER')
        role='prior' if meta['path']=='forward' else 'front'
        if point=='worker-armed-before-fault':order(event('arm'),enter,leave,event('fault_attempt'));need(enter['role']=='worker','HELD_ROLE')
        elif point=='server-after-receive':
            receipt=event('received_message','front');validated=event('wire','front')
            need(receipt['raw']==validated['raw'] and receipt['size']==validated['size'],'RECEIVE_BOUNDARY_BYTES')
            order(event('fault_attempt'),receipt,enter,leave,validated,event('receive','front'))
            need(enter['role']=='front','HELD_ROLE')
        elif point=='server-before-repair':order(event('identity',role),enter,leave,event('repair',role));need(enter['role']==role,'HELD_ROLE')
        elif point=='server-before-reply':
            order(event('forward_call') if meta['path']=='forward' else event('repair','front'),enter,leave,event('reply','front'));need(enter['role']=='front','HELD_ROLE')
        elif point=='front-before-forward':order(event('identity','front'),enter,leave,event('wire','prior'));need(enter['role']=='front','HELD_ROLE')
        elif point=='prior-before-reply':order(event('repair','prior'),enter,leave,event('reply','prior'));need(enter['role']=='prior','HELD_ROLE')
        elif point=='worker-resumed-before-disarm':order(event('resume'),enter,leave,event('commit'));need(enter['role']=='worker','HELD_ROLE')
        else:need(False,'UNKNOWN_HOLD')
        if point!='worker-resumed-before-disarm':order(leave,event('resume'))
    return failures
