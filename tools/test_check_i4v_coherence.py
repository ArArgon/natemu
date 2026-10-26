import copy
import gzip
import json
from pathlib import Path
from dataclasses import asdict
import unittest

import check_i4v_coherence as c
from check_i4v_ordering import native_seed


class CoherenceOracleTests(unittest.TestCase):
    def test_exact_sixty_fixture_bindings(self):
        cases = [(p, n, a) for p in range(6) for n in range(1, 5) for a in range(n)]
        self.assertEqual(len(cases), 60)
        for p, n, a in cases:
            self.assertIn(c.configuration(p, n, a), c.model.MODES)
        for args in ((6, 1, 0), (0, 0, 0), (0, 5, 0), (0, 2, 2), (0, 1, -1)):
            with self.assertRaises(ValueError):
                c.configuration(*args)

    def test_dword_load_zero_extends_and_store_preserves_all_other_bytes(self):
        initial = native_seed(1)
        initial['x'][4] = 0x12345678fedcba98
        image = [0xa5] * 4096 + [0x5a] * 4096
        final, changed = c.memory_access(image, initial, 'store', c.DATA + 4096 + 256)
        self.assertEqual(final, initial)
        self.assertEqual(changed[:4352], image[:4352])
        self.assertEqual(changed[4352:4356], [0x98, 0xba, 0xdc, 0xfe])
        self.assertEqual(changed[4356:], image[4356:])
        loaded, preserved = c.memory_access(changed, initial, 'load', c.DATA + 4096 + 256)
        expected = copy.deepcopy(initial); expected['x'][4] = 0xfedcba98
        self.assertEqual(loaded, expected)
        self.assertEqual(preserved, changed)
        self.assertEqual(image, [0xa5] * 4096 + [0x5a] * 4096)

    def test_route_flags_and_leaf_identity_are_not_reduced_to_gpa(self):
        tables, context, _ = c.model.fixture('four')
        route = c.model.walk(tables, context, c.DATA, 'write').route
        observed = {k: int(v) if type(v) is bool else v for k, v in asdict(route).items()}
        c.exact_route(observed, route)
        for key in observed:
            bad = observed.copy(); bad[key] ^= 1
            with self.assertRaises(ValueError):
                c.exact_route(bad, route)

    def test_packets_keep_decode_mode_owner_and_source_distinct(self):
        for profile in range(6):
            for n in range(1,5):
                for actor in range(n):
                    for cpu in range(n):
                        for kind in (0,1):
                            p=c.packet(37,profile,n,actor,cpu,kind)
                            r=c.reply(37,profile,n,actor,cpu,kind)
                            self.assertEqual(p[8],64 if profile in (0,3,5) else 32)
                            self.assertEqual(p[3],cpu)
                            self.assertEqual(p[7],c.RIP+cpu*256+kind*16)
                            self.assertEqual(r[1:14],p[1:14])
                            self.assertEqual(r[14].to_bytes(8,'little'),c.BODIES['load' if kind else 'store'])

    def test_staged_pae_root_memory_is_deliberately_different(self):
        for profile in (2,4):
            tables,context,layout=c.initial_tables(profile)
            tables[0x1020]=0x6001
            before=c.model.walk(tables,context,c.DATA,'read')
            hidden,_=c.model.load_hidden(tables,0x1020,context.hidden)
            from dataclasses import replace
            after=c.model.walk(tables,replace(context,hidden=hidden),c.DATA,'read')
            self.assertNotEqual(before.route.gpa,after.route.gpa)
            self.assertEqual(context.hidden.entries,(0x2001,0,0,0))

    def test_ordered_walk_does_not_accept_aggregate_ad_counts(self):
        for profile in range(6):
            tables,context,_=c.initial_tables(profile)
            out=c.model.walk(tables,context,c.DATA,'write')
            observed=c.zero_walk()
            observed.update(address=c.DATA,intent=1,read_count=len(out.reads),write_count=len(out.writes))
            observed['reads'][:len(out.reads)]=[[at,tables[at]] for at in out.reads]
            observed['writes'][:len(out.writes)]=[list(row) for row in out.writes]
            for key in ('gpa','leaf_bytes','writable','dirty','user','executable','wb'):
                observed[key]=int(getattr(out.route,key))
            self.assertEqual(c.validate_walk(observed,tables,context,c.DATA,'write'),out)
            bad=copy.deepcopy(observed);bad['reads'][0][1]^=c.model.A
            with self.assertRaises(ValueError):c.validate_walk(bad,tables,context,c.DATA,'write')
            bad=copy.deepcopy(observed);bad['writes'].reverse()
            with self.assertRaises(ValueError):c.validate_walk(bad,tables,context,c.DATA,'write')

    def test_counters_cannot_replace_complete_native_evidence(self):
        with self.assertRaises((ValueError,KeyError)):
            c.check(dict(status=1, completed=1, n=1, profile=0, actor=0))


class NativeReplayCorruptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(gzip.decompress((Path(__file__).parent/'testdata/i4v_coherence_parent_n1.json.gz').read_bytes()))

    def audit(self, report):
        return c.audit_development(report, terminal_probe=False)

    def rejects(self, mutate):
        report=copy.deepcopy(self.raw)
        mutate(report)
        with self.assertRaises((ValueError,KeyError,TypeError,IndexError)):
            self.audit(report)

    def test_actual_native_diagnostic_replays_full_state(self):
        self.assertEqual(self.audit(self.raw)['status'],'PASS')
        with self.assertRaisesRegex(ValueError,'last pin observed'):
            c.audit_development(self.raw)

    def test_full_native_and_canonical_preservation(self):
        for key in ('initial','fault','captured'):
            self.rejects(lambda r,k=key:r['owners'][0]['visits'][2][k]['x'].__setitem__(30,0))
            self.rejects(lambda r,k=key:r['owners'][0]['visits'][2][k]['q'][31].__setitem__(15,0))
        self.rejects(lambda r:r['owners'][0]['guest_after']['gpr'].__setitem__(15,0))
        self.rejects(lambda r:r['owners'][0]['guest_after']['control'].__setitem__(1,0))
        self.rejects(lambda r:r['owners'][0]['bank_after'].__setitem__(300,0))

    def test_precise_abort_and_saved_route(self):
        for bit in (7,8,10):
            self.rejects(lambda r,b=bit:r['owners'][0]['visits'][2].__setitem__('esr',r['owners'][0]['visits'][2]['esr']|(1<<b)))
        for key in ('pc','resume_pc','far','spsr','saved_gpa','saved_leaf_bytes','saved_writable'):
            self.rejects(lambda r,k=key:r['owners'][0]['visits'][2].__setitem__(k,r['owners'][0]['visits'][2][k]^4))
        self.rejects(lambda r:r['owners'][0]['visits'][14].__setitem__('completed',1))

    def test_complete_memory_and_table_snapshots(self):
        for key in ('data_before','data_after','source_before','source_after','tables_before','tables_after'):
            self.rejects(lambda r,k=key:r[k].__setitem__(100,r[k][100]^1))
        self.rejects(lambda r:r['images'].__setitem__(5,r['images'][5][:200]+'ff'+r['images'][5][202:]))
        self.rejects(lambda r:r['owners'][0]['visits'][4]['memory_after'].__setitem__(0,1))
        self.rejects(lambda r:r['owners'][0]['visits'][4]['walks'][1]['writes'][0].__setitem__(2,0))

    def test_invalidation_and_cache_attributes(self):
        self.rejects(lambda r:r['owners'][0]['ack_leaves'][0].__setitem__(0,123))
        self.rejects(lambda r:r['owners'][0]['visits'][12]['native_after'].__setitem__(0,r['owners'][0]['visits'][12]['native_after'][0]&~128))
        self.rejects(lambda r:r['owners'][0]['mapping_leaf_bytes'].__setitem__(1,2*1024*1024))
        self.rejects(lambda r:r['owners'][0]['native_leaves'].__setitem__(2,0))
        self.rejects(lambda r:r['owners'][0]['hidden_after'].__setitem__('generation',1))

    def test_native_code_and_compiler_identity(self):
        for field in ('requests','replies','compiler_requests'):
            for word in (2,3,5,6,7,8,9,12):
                self.rejects(lambda r,f=field,w=word:r['owners'][0][f][0].__setitem__(w,r['owners'][0][f][0][w]^1))
        for field in ('code','code_before_clear','code_after_clear','published_code','code_before'):
            self.rejects(lambda r,f=field:r['owners'][0][f].__setitem__(7,r['owners'][0][f][7]^1))
        self.rejects(lambda r:r['owners'][0].__setitem__('entry_rejected',0))

    def test_trace_cannot_move_effects_across_lifetime_cuts(self):
        def move_before(r,event,before):
            t=r['coordinator']['trace'];i=next(i for i,w in enumerate(t) if w>>8==event)
            word=t.pop(i);j=next(i for i,w in enumerate(t) if w>>8==before);t.insert(j,word)
        for event,before in ((64,0x3003),(65,64),(68,65),(70,69),(71,70),(74,4),(4,3),(66,64)):
            self.rejects(lambda r,e=event,b=before:move_before(r,e,b))
        def remove(r,event):
            t=r['coordinator']['trace'];i=next(i for i,w in enumerate(t) if w>>8==event);t.pop(i);r['coordinator']['trace_length']-=1
        for event in (67,66,63,73,74,60,61):
            self.rejects(lambda r,e=event:remove(r,e))

    def test_second_admission_after_completion_is_not_a_new_visit(self):
        def duplicate(r):
            t=r['coordinator']['trace'];i=t.index(0x3000<<8)
            t.insert(i+1,1<<8);r['coordinator']['trace_length']+=1
        self.rejects(duplicate)

    def test_new_terminal_probe_is_required_and_bound_to_last_pin(self):
        # Synthetic extension tests the new trace contract only; this is not a
        # claim that the older retained diagnostic executed the new probe.
        r=copy.deepcopy(self.raw);o=r['owners'][0];trace=r['coordinator']['trace']
        index=next(i for i,w in enumerate(trace) if w>>8==69)
        trace.insert(index,75<<8);r['coordinator']['trace_length']+=1
        o.update(terminal_reuse_blocked=1,code_while_held=o['code'].copy())
        self.assertEqual(c.audit_development(r)['status'],'PASS')
        for field in ('terminal_reuse_blocked','code_while_held'):
            bad=copy.deepcopy(r)
            bad['owners'][0][field]=0 if field=='terminal_reuse_blocked' else [0]*32
            with self.assertRaises(ValueError):c.audit_development(bad)
        bad=copy.deepcopy(r);t=bad['coordinator']['trace'];i=t.index(75<<8);t[i],t[i+1]=t[i+1],t[i]
        with self.assertRaises(ValueError):c.audit_development(bad)


class MultiOwnerPAEReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=json.loads(gzip.decompress((Path(__file__).parent/'testdata/i4v_coherence_size_pae_n4.json.gz').read_bytes()))

    def rejects(self, mutate):
        report=copy.deepcopy(self.raw);mutate(report)
        with self.assertRaises((ValueError,KeyError,TypeError,IndexError)):
            c.audit_development(report)

    def test_requested_identity_cannot_be_replaced_by_another_valid_run(self):
        c.bind_requested(self.raw,4,4,3)
        for args in ((3,4,3),(4,3,2),(4,4,2)):
            with self.assertRaises(ValueError):c.bind_requested(self.raw,*args)

    def test_actual_four_owner_large_page_native_report(self):
        result=c.audit_development(self.raw)
        self.assertEqual((result['visits'],result['snapshots'],result['actual_compiles']),(64,24,8))

    def test_hidden_root_memory_perturbation_does_not_reload_any_owner(self):
        for cpu in range(4):
            self.rejects(lambda r,i=cpu:r['owners'][i]['hidden_after']['entries'].__setitem__(0,0x6001))
            self.rejects(lambda r,i=cpu:r['owners'][i].__setitem__('hidden_loads',2))
            self.rejects(lambda r,i=cpu:r['owners'][i]['hidden_reads'].__setitem__(1,0x1030))
        def remove(r):
            t=r['coordinator']['trace'];t.remove((72<<8)|3);r['coordinator']['trace_length']-=1
        self.rejects(remove)

    def test_each_page_size_replacement_and_both_subpages_are_bound(self):
        for cpu in range(4):
            for visit in (4,5,8,9,12,13):
                self.rejects(lambda r,i=cpu,v=visit:r['owners'][i]['visits'][v].__setitem__('saved_leaf_bytes',4096 if v in (4,5,12,13) else 2*1024*1024))
        self.rejects(lambda r:r['owners'][2]['visits'][5].__setitem__('saved_gpa',c.DATA+512))
        self.rejects(lambda r:r['owners'][1]['mapping_leaf_bytes'].__setitem__(0,4096))

    def test_fifo_service_cannot_change_owner_or_skip_delayed_ack(self):
        def change_selected(r):
            t=r['coordinator']['trace'];i=next(i for i,w in enumerate(t) if w>>8==4);t[i]^=1
        self.rejects(change_selected)
        def premature_ack(r):
            t=r['coordinator']['trace'];blocked=next(i for i,w in enumerate(t) if w>>8==66)
            ack=next(i for i in range(blocked+1,len(t)) if t[i]>>8==65)
            t[blocked],t[ack]=t[ack],t[blocked]
        self.rejects(premature_ack)
        self.rejects(lambda r:r['owners'][0].__setitem__('entry_blocked',3))

    def test_last_actor_reference_is_required_without_active_memory(self):
        def early_clear(r):
            t=r['coordinator']['trace'];release=next(i for i,w in enumerate(t) if w==(69<<8)|3)
            clear=next(i for i,w in enumerate(t) if w>>8==70);word=t.pop(clear);t.insert(release,word)
        self.rejects(early_clear)
        self.rejects(lambda r:r['owners'][3]['code_while_held'].__setitem__(0,0))
        self.rejects(lambda r:r['owners'][1].__setitem__('terminal_reuse_blocked',1))

    def test_peer_compilations_and_results_cannot_be_reassigned(self):
        self.rejects(lambda r:r['owners'][1].__setitem__('compiler_requests',r['owners'][0]['compiler_requests']))
        self.rejects(lambda r:r['owners'][2]['visits'][11].__setitem__('captured',r['owners'][1]['visits'][11]['captured']))
        self.rejects(lambda r:r['owners'][0]['guest_after']['gpr'].__setitem__(3,r['owners'][1]['guest_after']['gpr'][3]))


class CachedADReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=json.loads(gzip.decompress((Path(__file__).parent/'testdata/i4v_coherence_ad_four_n3.json.gz').read_bytes()))

    def rejects(self, mutate):
        report=copy.deepcopy(self.raw);mutate(report)
        with self.assertRaises((ValueError,KeyError,TypeError,IndexError)):
            c.check(report,3,3,1)

    def test_public_acceptance_binds_actual_current_native_report(self):
        result=c.check(self.raw,3,3,1)
        self.assertEqual((result['status'],result['visits'],result['actual_compiles']),('PASS',48,6))
        for args in ((0,3,1),(3,4,1),(3,3,2)):
            with self.assertRaises(ValueError):c.check(self.raw,*args)

    def test_cached_write_keeps_dirty_history_without_new_table_dirty(self):
        self.rejects(lambda r:r['owners'][0]['ack_leaves'][0].__setitem__(0,0))
        self.rejects(lambda r:r['owners'][1]['visits'][4].__setitem__('saved_writable',0))
        self.rejects(lambda r:r['owners'][2]['visits'][5].__setitem__('faults',1))
        def set_new_dirty(r):
            i=next(i for i,s in enumerate(r['common']['snapshots']) if s['kind']==1 and s['visit']==4)
            b=bytearray.fromhex(r['images'][i]);b[8192+0x4800-0x1000]|=64;r['images'][i]=b.hex()
        self.rejects(set_new_dirty)

    def test_explicit_invalidation_clears_both_aliases_and_requires_read_then_dirty_write(self):
        self.rejects(lambda r:r['owners'][0]['ack_leaves'][1].__setitem__(2,r['owners'][0]['ack_leaves'][0][2]))
        self.rejects(lambda r:r['owners'][1]['visits'][8].__setitem__('retries',0))
        self.rejects(lambda r:r['owners'][2]['visits'][10]['walks'][1].__setitem__('dirty',0))
        self.rejects(lambda r:r['owners'][2]['visits'][10]['walks'][1].__setitem__('write_count',r['owners'][2]['visits'][10]['walks'][1]['write_count']^1))
        def read_dirty(r):
            i=next(i for i,s in enumerate(r['common']['snapshots']) if s['kind']==1 and s['visit']==8)
            b=bytearray.fromhex(r['images'][i]);b[8192+0x4800-0x1000]|=64;r['images'][i]=b.hex()
        self.rejects(read_dirty)

    def test_cached_phase_cannot_hide_a_fresh_operand_walk(self):
        def insert(r):
            t=r['coordinator']['trace'];i=next(i for i,w in enumerate(t) if w==((0x2000+4)<<8))
            t.insert(i+1,((0x1000+4*8+1)<<8));r['coordinator']['trace_length']+=1
        self.rejects(insert)

    def test_terminal_permission_fault_preserves_memory_and_cr2_prefix(self):
        self.rejects(lambda r:r['owners'][0]['visits'][15].__setitem__('cr2_before',0))
        self.rejects(lambda r:r['owners'][1]['visits'][14].__setitem__('error',7))
        self.rejects(lambda r:r['owners'][2]['visits'][15]['memory_after'].__setitem__(1,255))


if __name__ == '__main__':
    unittest.main()
