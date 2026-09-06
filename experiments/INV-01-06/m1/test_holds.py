import copy
import unittest
from coverage import HOLDS
from check import two_pass_check,_rows_to_text,by_kind
from holds import check_hold
from controls import check_control
from trace_fixtures import valid_trace,held_trace

class Holds(unittest.TestCase):
    def test_forwarded_proof_and_receive_boundary_order(self):
        for mutation in ('initial-before-proof','premature-proof','early-forward'):
            rows=valid_trace(path='forward')
            if mutation=='initial-before-proof':
                moved=next(r for r in by_kind(rows,'teardown_restore') if r['label']=='restored-initial')
                target=by_kind(rows,'arm')[-1];offset=0
            elif mutation=='premature-proof':
                moved=by_kind(rows,'restoration_proof')[0];target=by_kind(rows,'arm')[-1];offset=1
            else:moved=by_kind(rows,'forward_call')[0];target=by_kind(rows,'wire')[0];offset=0
            rows.remove(moved);rows.insert(rows.index(target)+offset,moved)
            self.assertTrue(two_pass_check(_rows_to_text(rows)),mutation)
        # A recipient can resume before its sender logs successful send.
        rows=valid_trace(path='forward');sent=next(r for r in by_kind(rows,'send') if r['role']=='prior' and r['event']==0)
        rows.remove(sent);rows.insert(rows.index(by_kind(rows,'commit')[0]),sent)
        self.assertEqual(two_pass_check(_rows_to_text(rows)),[])
        rows,job=held_trace('server-after-receive')
        validated=next(r for r in by_kind(rows,'wire') if r['worker']==0)
        rows.remove(validated);rows.insert(rows.index(by_kind(rows,'hold_enter')[0]),validated)
        self.assertIn('HOLD_ORDER',check_hold(_rows_to_text(rows),job))
    def test_full_valid_topology_routing(self):
        for n in (1,2,4,8):
            for path in ('direct','forward'):
                for topology in ('serial','perworker'):
                    for access in ('load','store'):
                        rows=valid_trace(n,path,topology,access,total=2)
                        self.assertEqual(two_pass_check(_rows_to_text(rows)),[],(n,path,topology,access))
        rows=valid_trace(path='forward');rows[0]['arm']='restoration'
        self.assertEqual(check_control(_rows_to_text(rows),'restoration',dict(returncode=0,timed_out=False,stop_reason=None)),[])
    def test_all_hold_identities_and_topologies(self):
        for n in (1,2,4,8):
            for path in ('direct','forward'):
                for topology in ('serial','perworker'):
                    for point in HOLDS:
                        if path=='direct' and point in ('front-before-forward','prior-before-reply'):continue
                        for worker in range(n):
                            rows,job=held_trace(point,n,path,topology,worker=worker)
                            self.assertEqual(check_hold(_rows_to_text(rows),job),[],(n,path,topology,point,worker))
    def test_hold_must_observe_competitor_before_release(self):
        for point in HOLDS:
            rows,job=held_trace(point,path='forward')
            for kind in ('hold_enter','hold_competitor','hold_release','hold_exit','join_attempt'):
                changed=copy.deepcopy(rows);changed.remove(by_kind(changed,kind)[0])
                self.assertTrue(check_hold(_rows_to_text(changed),job),(point,kind))
            changed=copy.deepcopy(rows);release=by_kind(changed,'hold_release')[0];changed.remove(release);changed.insert(changed.index(by_kind(changed,'hold_enter')[0]),release)
            self.assertTrue(check_hold(_rows_to_text(changed),job),point)
        rows,job=held_trace('server-before-repair')
        by_kind(rows,'hold_competitor')[0]['completed']=0
        self.assertTrue(check_hold(_rows_to_text(rows),job))
    def test_full_sample_warmup_and_restored_proof(self):
        for point in ('prior-before-reply','teardown-before-restore'):
            rows,job=held_trace(point,path='forward',total=110)
            rows[0].update(warmup=10,checked=100)
            for arm in by_kind(rows,'arm'):arm['warmup']=arm['event']<10
            by_kind(rows,'summary')[0].update(events_checked=200,events_warmup=20)
            self.assertEqual(check_hold(_rows_to_text(rows),job),[],point)

if __name__=='__main__':unittest.main()
