"""Cause-specific M06 controls. Partial traces never count as positive delivery.

The fixed auxiliary schedule starts worker 0 alone. A valid control therefore
has at most its one attempted event, except the deliberately repeated fault.
No terminal signal is predicted for unhandled or destroyed-receive cases.
"""
import signal
import struct
from check import Invalid, parse, by_kind, two_pass_check, SENTINEL


def check_control(text, name, outcome):
    try:
        return _check(parse(text), text, name, outcome)
    except (Invalid, KeyError, TypeError, ValueError, IndexError, struct.error) as exc:
        return ['CONTROL_RECORDS:'+str(exc)]


def _check(rows, text, name, outcome):
    failures=[]
    def need(ok,why):
        if not ok: failures.append(why)
    def one(kind, subset=None):
        found=by_kind(rows if subset is None else subset,kind)
        if len(found)!=1: raise Invalid(kind.upper()+'_COUNT')
        return found[0]
    positions={id(r):i for i,r in enumerate(rows)}
    def before(a,b): need(positions[id(a)]<positions[id(b)],'CONTROL_ORDER')
    meta=one('meta');need(rows[0] is meta and meta['schema']=='M06-M1-trace-v3','CONTROL_META')
    seen_snapshots=set()
    for snapshot in by_kind(rows,'snapshot'):
        key=snapshot['worker'],snapshot['label'];need(key not in seen_snapshots,'CONTROL_DUPLICATE_SNAPSHOT');seen_snapshots.add(key)
        tuples=[r for r in by_kind(rows,'snapshot_tuple') if (r['worker'],r['label'])==key]
        need(len(tuples)==snapshot['count'] and {r['index'] for r in tuples}==set(range(snapshot['count'])),'CONTROL_SNAPSHOT_COUNT')
    need(meta['arm']==name or meta['negative']==name,'CONTROL_IDENTITY')
    code=outcome['returncode'];timeout=outcome['timed_out'];stop=outcome.get('stop_reason')
    # A file/reserve/stage interruption cannot become a desired control timeout.
    need(stop in (None,'no-progress timeout','total timeout'),'CONTROL_RESOURCE_STOP')
    if name=='restoration':
        need(meta['path']=='forward' and code==0 and not timeout and stop is None,'RESTORATION_OUTCOME')
        failures+=two_pass_check(text)
        return failures
    need(not by_kind(rows,'summary') and not by_kind(rows,'commit') and not by_kind(rows,'disarm'),'CONTROL_COMMITTED')
    if name=='prior-handler-unsupported':
        decoy=one('decoy_installed');refusal=one('prior_handler_unsupported')
        need(code==78 and not timeout and not stop,'PRIOR_REFUSAL_OUTCOME')
        need(meta['n']==1 and decoy['worker']==refusal['worker']==0,'PRIOR_REFUSAL_WORKER')
        need((decoy['mask'],decoy['behavior'],decoy['flavor'])==(2,1,5) and decoy['port']>0,'DECOY_CONTRACT')
        initial=[r for r in by_kind(rows,'snapshot_tuple') if r['label']=='initial']
        need(len(initial)==1 and (initial[0]['mask'],initial[0]['port'],initial[0]['behavior'],initial[0]['flavor'])==(2,decoy['port'],1,5),'DECOY_SNAPSHOT')
        need(all(not r['port'] for r in by_kind(rows,'snapshot_tuple') if r['label']=='before-decoy'),'FOREIGN_PRIOR_REPLACED')
        need(len([r for r in by_kind(rows,'snapshot') if r['label']=='before-decoy'])==1,'PRIOR_PREFLIGHT_MISSING')
        before(decoy,refusal)
        need(not any(by_kind(rows,k) for k in ('register','arm','wire','receive','reply','resume')),'REFUSAL_AFTER_ADMISSION')
        return failures
    arm=one('arm');trigger=one('negative_trigger')
    need(arm['worker']==trigger['worker']==0 and arm['event']==trigger['event']==0 and trigger['name']==name,'TRIGGER_EVENT')
    need(meta['warmup']==0 and meta['checked']==1 and arm['nonce']==1,'CONTROL_SAMPLE')
    need(not by_kind(rows,'teardown_restore') and not by_kind(rows,'worker_exit'),'FATAL_CLAIMS_TEARDOWN')
    for row in rows:
        if 'event' in row and row['kind']!='admission_schedule':need(row.get('worker')==0 and row['event']==0,'EXTRANEOUS_CONTROL_EVENT')
    allowed={'meta','snapshot','snapshot_tuple','register','admission_schedule','arm','wire','receive','identity','repair','reply','send','resume','negative_trigger','negative_action','terminal','receive_failure'}
    need(all(r['kind'] in allowed for r in rows),'UNKNOWN_CONTROL_RECORD')
    registrations=by_kind(rows,'register')
    need(len(registrations)==meta['n'] and {r['worker'] for r in registrations}==set(range(meta['n'])),'CONTROL_REGISTRATION')
    reg=next(r for r in registrations if r['worker']==0)
    wires=by_kind(rows,'wire');receives=by_kind(rows,'receive');identities=by_kind(rows,'identity')
    # Decode actual bytes separately from the human-readable receive record.
    seen_roles=set()
    for wire in wires:
        raw=bytes.fromhex(wire['raw']);need(len(raw)==336 and raw[24:32]==b'\0\0\0\0\1\0\0\0','CONTROL_WIRE_NDR')
        bits,size,reply,port,voucher,routine=struct.unpack_from('<6I',raw)
        expected_port=reg['prior_recv_port'] if wire['role']=='prior' else reg['recv_port']
        need((size,port,routine)==(336,expected_port,2406) and bits&31==18 and not bits&0x80000000 and reply>0,'CONTROL_WIRE_HEADER')
        need((wire['size'],wire['bits'],wire['reply_right'],wire['trailer_size'],wire['ndr_exact'])==(size,bits,reply,8,True),'CONTROL_WIRE_DECODE')
        exception,count,category,address,flavor,statecount=struct.unpack_from('<2I2Q2I',raw,32)
        need((exception,count,category,flavor,statecount)==(1,2,2,6,68),'CONTROL_WIRE_SHAPE')
        matching=[r for r in receives if r['role']==wire['role']]
        if matching:
            received=matching[0]
            need(received['routine_id']==2406,'CONTROL_RECEIVED_ROUTINE')
            need(received['state']==list(struct.unpack_from('<68I',raw,64)),'CONTROL_RAW_STATE')
            need((received['port'],received['exception'],received['codeCnt'],received['code'],received['flavor'],received['old_stateCnt'])==(port,1,2,[2,address],6,68),'CONTROL_RECEIVED_FIELDS')
            before(arm,wire)
            if wire['role'] not in seen_roles:before(wire,received)
        seen_roles.add(wire['role'])
    for received in receives:
        identity=one('identity',[r for r in identities if r['role']==received['role']]);before(received,identity)
        state=received['state']
        def r64(n):return state[2*n]|state[2*n+1]<<32
        need(len(state)==68 and all(type(x) is int and 0<=x<2**32 for x in state),'CONTROL_STATE_WORDS')
        need(identity['cookie']==r64(9) and identity['nonce']==r64(10),'CONTROL_DECODED_IDENTITY')
        need(identity['pc']==r64(32) and identity['sp']==r64(31),'CONTROL_RAW_PC_SP')
        actual=(identity['pc']==arm['expected_pc'] and arm['sp_lo']<=identity['sp']<arm['sp_hi'] and
                r64(9)==r64(19)==arm['cookie'] and r64(10)==r64(20)==arm['nonce'] and r64(21)==SENTINEL and
                r64(22)==identity['sp'] and received['code']==[2,arm['address']])
        need(identity['valid'] is actual,'CONTROL_IDENTITY_ORACLE')
        if name not in ('wrong-event-descriptor','unexpected-helper-fault'):need(actual,'UNRELATED_IDENTITY_FAILURE')
        if name=='wrong-event-descriptor':
            need(arm['sp_lo']<=identity['sp']<arm['sp_hi'] and r64(9)==r64(19)==arm['cookie'] and r64(10)==r64(20)==arm['nonce'] and r64(21)==SENTINEL and r64(22)==identity['sp'] and received['code']==[2,arm['address']],'UNRELATED_DESCRIPTOR_FAILURE')
    if name!='receive-right-destruction':
        expected_roles={'front','prior'} if name=='prior-server-failure' else {'front'}
        need({r['role'] for r in receives}==expected_roles and len(receives)==len(expected_roles),'CONTROL_DELIVERY_COUNT')
        need(len(identities)==len(expected_roles),'CONTROL_IDENTITY_COUNT')
        need(len(wires)==len(expected_roles)+(name=='skipped-repair-nominal-success'),'CONTROL_WIRE_COUNT')
    replies=by_kind(rows,'reply');sends=by_kind(rows,'send');repairs=by_kind(rows,'repair');resumes=by_kind(rows,'resume')
    for reply in replies:
        received=one('receive',[r for r in receives if r['role']==reply['role']])
        need((reply['kr'],reply['flavor'],reply['new_stateCnt'],reply['state'])==(0,6,68,received['state']),'CONTROL_REPLY_STATE')
    terminal=by_kind(rows,'terminal')
    receive_errors=by_kind(rows,'receive_failure')
    need(not receive_errors or name=='receive-right-destruction','UNRELATED_RECEIVE_FAILURE')
    if name in ('corrupted-repair-result','skipped-repair-nominal-success','wrong-event-descriptor','reply-send-failure','prior-server-failure','unexpected-helper-fault'):
        t=one('terminal')
        contained=(code==70 or (name=='reply-send-failure' and code in (-signal.SIGBUS,-signal.SIGSEGV)))
        need(contained and not timeout and not stop,'CONTROL_TERMINAL_OUTCOME')
        reasons={'corrupted-repair-result':'resumed state/data mismatch','skipped-repair-nominal-success':'repeated fault before commit','wrong-event-descriptor':'identity mismatch','reply-send-failure':'reply send failure','prior-server-failure':'prior server failure','unexpected-helper-fault':'identity mismatch'}
        need(t['reason']==reasons[name],'CONTROL_CAUSE');before(trigger,t)
    if name in ('corrupted-repair-result','skipped-repair-nominal-success','reply-send-failure'):
        repair=one('repair');reply=one('reply');before(repair,reply)
        need(repair['mprotect_result']==repair['errno']==0 and repair['address']==arm['address'] and repair['length']==arm['page_size'],'CONTROL_REPAIR')
        need(repair['skipped'] is (name=='skipped-repair-nominal-success'),'CONTROL_REPAIR_SKIP')
    else:need(not repairs and not replies,'UNEXPECTED_CONTROL_REPAIR_REPLY')
    if name=='corrupted-repair-result':
        resume=one('resume');need(resume['data']==arm['expected_data']^1 and resume['data_ok'] is False,'CORRUPTION_NOT_OBSERVED')
        need(resume['sentinels_ok'] is True and resume['sp_ok'] is True,'UNRELATED_RESUME_FAILURE')
        need(resume['cookie']==arm['cookie'] and resume['nonce']==arm['nonce'] and resume['sentinel']==SENTINEL and resume['sp_before']==resume['sp_after'] and arm['sp_lo']<=resume['sp_before']<arm['sp_hi'],'CORRUPTION_RETURNED_STATE')
        before(trigger,resume);need(len(sends)<=1 and all(r['kr']==0 for r in sends),'CORRUPTION_SEND_FAILURE')
    else:need(not resumes,'CONTROL_RESUMED')
    if name=='skipped-repair-nominal-success':
        need(len(sends)==1 and sends[0]['kr']==0,'SKIPPED_REPAIR_SEND')
        before(sends[0],wires[-1]);before(trigger,wires[-1])
    if name=='wrong-event-descriptor':
        identity=one('identity');need(identity['valid'] is False and identity['pc']+4==arm['expected_pc'],'DESCRIPTOR_NOT_WRONG')
        before(trigger,arm)
    if name=='unexpected-helper-fault':
        action=one('negative_action');received=one('receive')
        need(action['action']=='helper-access' and action['protection']==0 and action['length']==arm['page_size'] and action['address']!=arm['address'] and received['code'][1]==action['address'],'HELPER_FAULT_NOT_OBSERVED')
        need(one('identity')['valid'] is False,'HELPER_ACCEPTED');before(action,wires[0])
    if name=='reply-send-failure':
        action=one('negative_action');sent=one('send')
        need(action['action']=='drop-reply-right' and action['kr']==0 and action['port']==wires[0]['reply_right'],'REPLY_RIGHT_NOT_DROPPED')
        need(sent['kr']==0x10000003 and sent['role']=='front','SEND_DID_NOT_FAIL')
        need(one('terminal')['code']==sent['kr'],'SEND_TERMINAL_CODE')
        before(one('identity'),one('repair'));before(one('reply'),trigger);before(trigger,action)
        before(action,sent);before(sent,one('terminal'))
    if name=='server-stall':
        need(timeout and code==-signal.SIGKILL and stop=='no-progress timeout' and not terminal and not sends,'STALL_NOT_CONTAINED')
        before(one('identity'),trigger)
    if name in ('terminal','receive-right-destruction'):
        port_death=(name=='receive-right-destruction' and not receive_errors and code==70 and not timeout and stop is None and
                    len(terminal)==1 and terminal[0]['reason']=='owned receive right destroyed' and
                    terminal[0]['code'] in (0x10004002,0x10004009))
        owned_error=False
        if receive_errors:
            error=one('receive_failure')
            valid=(error.get('schema')=='M06-M1-receive-failure-v1' and error.get('worker')==0 and
                   error.get('port')==reg['recv_port'] and error.get('role')=='front' and
                   error.get('code') in (0x10004002,0x10004006,0x10004009) and
                   error.get('destruction_observed') is True)
            same_terminal=(len(terminal)==1 and terminal[0]['reason']=='owned receive right invalidated' and
                           terminal[0]['code']==error.get('code'))
            owned_error=(valid and not timeout and stop is None and
                         ((code==70 and same_terminal) or
                          (code in (-signal.SIGBUS,-signal.SIGSEGV) and (not terminal or same_terminal))))
            before(one('negative_action'),error)
            for t in terminal:before(error,t)
        downstream=(not terminal and not receive_errors and ((code in (-signal.SIGBUS,-signal.SIGSEGV) and not timeout and stop is None) or
                    (code==-signal.SIGKILL and timeout and stop=='no-progress timeout')))
        need(port_death or owned_error or downstream,'UNHANDLED_UNRESOLVED_OR_UNRELATED')
        # This is parent-observed containment, not a claim of a specific signal or safe fallback.
        if name=='receive-right-destruction':
            action=one('negative_action');need(action['action']=='destroy-receive' and action['kr']==0 and action['port']==reg['recv_port'],'RECEIVE_NOT_DESTROYED')
            before(arm,trigger);before(trigger,action)
            need(not receives and not identities and not wires and not sends,'DESTROYED_ROUTE_DELIVERED')
            if port_death:before(action,terminal[0])
        else:
            before(one('identity'),trigger)
            need(len(sends)<=1 and all(r['kr']==0 for r in sends),'UNHANDLED_SEND_FAILURE')
            for sent in sends:before(trigger,sent)
    return failures
