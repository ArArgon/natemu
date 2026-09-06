import copy
import json
import signal
import struct
import unittest
from check import good_trace_rows, by_kind, _rows_to_text
from controls import check_control


def control_fixture(name):
    good=good_trace_rows();rows=good[:next(i for i,r in enumerate(good) if r['kind']=='arm')]
    rows[0]['negative']=name;rows[0]['arm']=''
    arm=copy.deepcopy(by_kind(good,'arm')[0]);rows.append(arm)
    event={k:copy.deepcopy(by_kind(good,k)[0]) for k in ('wire','receive','identity','repair','reply','send','resume')}
    trigger=dict(kind='negative_trigger',worker=0,event=0,name=name)
    terminal=dict(kind='terminal',reason='',code=0)
    status=dict(returncode=70,timed_out=False,stop_reason=None)
    if name=='prior-handler-unsupported':
        rows=rows[:3];rows[0]['negative']='';rows[0]['arm']=name
        rows=[r for r in rows if r.get('label')!='initial']
        rows += [dict(kind='snapshot',worker=0,label='before-decoy',count=0),
                 dict(kind='decoy_installed',worker=0,port=100,mask=2,behavior=1,flavor=5),
                 dict(kind='snapshot',worker=0,label='initial',count=1),
                 dict(kind='snapshot_tuple',worker=0,label='initial',index=0,mask=2,port=100,behavior=1,flavor=5),
                 dict(kind='prior_handler_unsupported',worker=0)]
        status['returncode']=78;return rows,status
    if name=='receive-right-destruction':
        rows += [trigger,dict(kind='negative_action',worker=0,event=0,action='destroy-receive',port=100,kr=0)]
        status['returncode']=-signal.SIGSEGV;return rows,status
    if name=='wrong-event-descriptor':
        rows.insert(len(rows)-1,trigger);arm['expected_pc']+=4;event['identity']['valid']=False
    if name=='unexpected-helper-fault':
        rows += [trigger,dict(kind='negative_action',worker=0,event=0,action='helper-access',address=32768,length=4096,protection=0)]
        event['receive']['code'][1]=32768;event['identity']['valid']=False
        raw=bytearray.fromhex(event['wire']['raw']);struct.pack_into('<Q',raw,48,32768);event['wire']['raw']=raw.hex()
    rows += [event[k] for k in ('wire','receive','identity')]
    if name in ('wrong-event-descriptor','unexpected-helper-fault'):
        terminal['reason']='identity mismatch'
    elif name=='server-stall':
        rows.append(trigger);status.update(returncode=-signal.SIGKILL,timed_out=True,stop_reason='no-progress timeout');return rows,status
    elif name=='prior-server-failure':
        rows[0]['path']='forward';by_kind(rows,'register')[0]['prior_recv_port']=101
        for k in ('wire','receive','identity'):
            r=copy.deepcopy(event[k]);r['role']='prior'
            if k=='receive':r['port']=101
            if k=='wire':
                raw=bytearray.fromhex(r['raw']);struct.pack_into('<I',raw,12,101);r['raw']=raw.hex()
            rows.append(r)
        rows.append(trigger);terminal['reason']='prior server failure'
    elif name=='terminal':
        rows[0]['negative']='';rows[0]['arm']=name
        rows += [trigger,event['send']];status['returncode']=-signal.SIGBUS;return rows,status
    else:
        if name=='skipped-repair-nominal-success':
            rows.append(trigger);event['repair']['skipped']=True
        rows.append(event['repair'])
        if name=='corrupted-repair-result':rows.append(trigger)
        rows.append(event['reply'])
        if name=='corrupted-repair-result':
            event['resume'].update(data=arm['expected_data']^1,data_ok=False)
            rows += [event['resume'],event['send']];terminal['reason']='resumed state/data mismatch'
        elif name=='skipped-repair-nominal-success':
            rows += [event['send'],copy.deepcopy(event['wire'])];terminal['reason']='repeated fault before commit'
        elif name=='reply-send-failure':
            rows += [trigger,dict(kind='negative_action',worker=0,event=0,action='drop-reply-right',port=200,kr=0)]
            event['send']['kr']=0x10000003;rows.append(event['send']);terminal.update(reason='reply send failure',code=event['send']['kr'])
    rows.append(terminal)
    return rows,status


class Controls(unittest.TestCase):
    def test_decoy_arm64_thread_state_none(self):
        rows,status=control_fixture('prior-handler-unsupported')
        for row in rows:
            if row['kind'] in ('decoy_installed','snapshot_tuple'):row['flavor']=5
        self.assertEqual(check_control(_rows_to_text(rows),'prior-handler-unsupported',status),[])
        for flavor in (0,6):
            bad=copy.deepcopy(rows)
            for row in bad:
                if row['kind'] in ('decoy_installed','snapshot_tuple'):row['flavor']=flavor
            self.assertTrue(check_control(_rows_to_text(bad),'prior-handler-unsupported',status))

    def test_reply_right_loss_terminal_race(self):
        name='reply-send-failure';rows,status=control_fixture(name)
        for code in (70,-signal.SIGBUS,-signal.SIGSEGV):
            self.assertEqual(check_control(_rows_to_text(rows),name,dict(status,returncode=code)),[])
        for field,value in [('code',0),('reason','unrelated failure')]:
            bad=copy.deepcopy(rows);by_kind(bad,'terminal')[0][field]=value
            self.assertTrue(check_control(_rows_to_text(bad),name,status))
        for kind,field,value in [('send','kr',0x10000004),('send','role','prior'),
                                ('negative_action','port',999),('negative_action','kr',5)]:
            bad=copy.deepcopy(rows);by_kind(bad,kind)[0][field]=value
            self.assertTrue(check_control(_rows_to_text(bad),name,status))
        for code in (0,-signal.SIGTERM,-signal.SIGXFSZ,-signal.SIGKILL):
            self.assertTrue(check_control(_rows_to_text(rows),name,dict(status,returncode=code)))
        bad=copy.deepcopy(rows);sent=by_kind(bad,'send')[0];bad.remove(sent);bad.append(sent)
        self.assertTrue(check_control(_rows_to_text(bad),name,status))
        for moved,after in [('negative_trigger','negative_action'),('negative_action','arm'),('repair','arm')]:
            bad=copy.deepcopy(rows);r=by_kind(bad,moved)[0];bad.remove(r)
            bad.insert(bad.index(by_kind(bad,after)[0])+1,r)
            self.assertIn('CONTROL_ORDER',check_control(_rows_to_text(bad),name,status),(moved,after))

    def test_owned_receive_error_witness(self):
        name='receive-right-destruction'
        for code in (0x10004002,0x10004006,0x10004009):
            rows,status=control_fixture(name)
            witness=dict(kind='receive_failure',schema='M06-M1-receive-failure-v1',
                         worker=0,port=100,role='front',code=code,destruction_observed=True)
            rows += [witness,dict(kind='terminal',reason='owned receive right invalidated',code=code)]
            for exitcode in (70,-signal.SIGBUS,-signal.SIGSEGV):
                self.assertEqual(check_control(_rows_to_text(rows),name,dict(status,returncode=exitcode)),[])
            self.assertEqual(check_control(_rows_to_text(rows[:-1]),name,status),[])
            self.assertTrue(check_control(_rows_to_text(rows[:-1]),name,dict(status,returncode=70)))
            for field,value in [('port',101),('worker',1),('role','prior'),('code',0x10004003),
                                ('destruction_observed',False),('schema','unknown')]:
                bad=copy.deepcopy(rows);by_kind(bad,'receive_failure')[0][field]=value
                self.assertTrue(check_control(_rows_to_text(bad),name,status),(code,field))
            bad=copy.deepcopy(rows);bad[-1]['reason']='receive failure'
            self.assertTrue(check_control(_rows_to_text(bad),name,status))
            for exitcode in (0,-signal.SIGTERM,-signal.SIGXFSZ,-signal.SIGKILL):
                self.assertTrue(check_control(_rows_to_text(rows),name,dict(status,returncode=exitcode)))
            bad=copy.deepcopy(rows);w=by_kind(bad,'receive_failure')[0];bad.remove(w)
            bad.insert(bad.index(by_kind(bad,'negative_action')[0]),w)
            self.assertTrue(check_control(_rows_to_text(bad),name,status))
        # The immutable run's generic terminal plus signal still cannot qualify.
        rows,status=control_fixture(name)
        rows.append(dict(kind='terminal',reason='receive failure',code=0x10004006))
        self.assertTrue(check_control(_rows_to_text(rows),name,dict(status,returncode=-signal.SIGBUS)))

    def test_actual_cause_required(self):
        names=('corrupted-repair-result','skipped-repair-nominal-success','wrong-event-descriptor','server-stall','receive-right-destruction','reply-send-failure','prior-server-failure','unexpected-helper-fault','terminal','prior-handler-unsupported')
        for name in names:
            with self.subTest(name=name):
                rows,status=control_fixture(name)
                self.assertEqual(check_control(_rows_to_text(rows),name,status),[])
                changed=dict(status,returncode=0,timed_out=False,stop_reason=None)
                self.assertTrue(check_control(_rows_to_text(rows),name,changed))
                if name!='prior-handler-unsupported':
                    self.assertTrue(check_control(_rows_to_text([r for r in rows if r['kind']!='negative_trigger']),name,status))
                for kind in ('wire','receive','identity','negative_action','terminal','decoy_installed'):
                    if not by_kind(rows,kind):continue
                    cut=list(rows);cut.remove(by_kind(rows,kind)[0])
                    self.assertTrue(check_control(_rows_to_text(cut),name,status),(name,kind))
                self.assertTrue(check_control(_rows_to_text(rows)+json.dumps(dict(kind='commit',worker=0,event=0))+'\n',name,status))
                self.assertTrue(check_control(_rows_to_text(rows),name,dict(status,stop_reason='stage output cap')))
    def test_marker_is_not_cause(self):
        for name,kind,field,value in [('corrupted-repair-result','resume','data',17),('skipped-repair-nominal-success','repair','skipped',False),('wrong-event-descriptor','identity','valid',True),('receive-right-destruction','negative_action','kr',5),('reply-send-failure','send','kr',0),('unexpected-helper-fault','negative_action','address',4096),('prior-handler-unsupported','snapshot_tuple','port',999)]:
            rows,status=control_fixture(name);by_kind(rows,kind)[0][field]=value
            self.assertTrue(check_control(_rows_to_text(rows),name,status),name)
    def test_partial_or_unrelated_timeout(self):
        rows,status=control_fixture('server-stall')
        self.assertTrue(check_control(_rows_to_text(rows)[:-1],'server-stall',status))
        self.assertTrue(check_control(_rows_to_text(rows),'server-stall',dict(status,stop_reason='total timeout')))

    def test_terminal_categories_and_raw_identity(self):
        for name in ('terminal','receive-right-destruction'):
            rows,status=control_fixture(name)
            for code in (70,-signal.SIGXFSZ,-signal.SIGXCPU,-signal.SIGKILL,-signal.SIGTERM):
                self.assertTrue(check_control(_rows_to_text(rows),name,dict(status,returncode=code)),(name,code))
            rows.append(dict(kind='terminal',reason='unrelated failed setup',code=5))
            self.assertTrue(check_control(_rows_to_text(rows),name,dict(status,returncode=70)))
        for code in (0x10004002,0x10004009):
            rows,status=control_fixture('receive-right-destruction')
            rows.append(dict(kind='terminal',reason='owned receive right destroyed',code=code))
            self.assertEqual(check_control(_rows_to_text(rows),'receive-right-destruction',dict(status,returncode=70)),[])
            terminal=rows.pop();rows.insert(rows.index(by_kind(rows,'negative_action')[0]),terminal)
            self.assertTrue(check_control(_rows_to_text(rows),'receive-right-destruction',dict(status,returncode=70)))
        rows,status=control_fixture('receive-right-destruction')
        rows.append(dict(kind='terminal',reason='owned receive right destroyed',code=0x10004009))
        trigger=by_kind(rows,'negative_trigger')[0];rows.remove(trigger);rows.append(trigger)
        self.assertIn('CONTROL_ORDER',check_control(_rows_to_text(rows),'receive-right-destruction',dict(status,returncode=70)))
        rows,status=control_fixture('terminal')
        trigger=by_kind(rows,'negative_trigger')[0];rows.remove(trigger);rows.insert(rows.index(by_kind(rows,'arm')[0]),trigger)
        self.assertIn('CONTROL_ORDER',check_control(_rows_to_text(rows),'terminal',status))
        for name in ('skipped-repair-nominal-success','wrong-event-descriptor','server-stall'):
            for register in (31,32):
                rows,status=control_fixture(name)
                for wire in by_kind(rows,'wire'):
                    raw=bytearray.fromhex(wire['raw']);struct.pack_into('<Q',raw,64+register*8,0xdeadbeec);wire['raw']=raw.hex()
                for row in by_kind(rows,'receive')+by_kind(rows,'reply'):
                    row['state'][2*register:2*register+2]=[0xdeadbeec,0]
                self.assertIn('CONTROL_RAW_PC_SP',check_control(_rows_to_text(rows),name,status))

if __name__=='__main__':unittest.main()
