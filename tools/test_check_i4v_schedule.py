import copy
import unittest
import check_i4v_schedule as oracle


def example():
    cfg = oracle.configuration(2,3,0)
    # Seed1 chooses owners 0,2,1 from an initially three-owner cohort.
    values = (1,270369,67634689,2647435461)
    owners = (0,2,1)
    done = 0
    ledger = []
    for draw, owner in enumerate(owners):
        ledger.append(dict(cut=oracle.ADMIT,phase=0,epoch=1,cohort=7,
            eligible=7 ^ done,arrived=7,done_before=done,prng_before=values[draw],
            prng_after=values[draw+1],draw=draw,chosen=owner,claimed=1,
            trace_before=draw,claim_trace_before=draw))
        done |= 1 << owner
    record = dict(case='schedule',config=cfg,summary=dict(magic=oracle.MAGIC,
        version=1,prng=values[-1],draws=3,cut_count=1,decision_count=3,
        pool_event_count=0,full_count=0,readmitted=0,full=[0]*4,retried=[0]*4),
        cuts=[dict(key=oracle.ADMIT,cohort=7,arrived=7,completed=7,
                   chosen=oracle.NONE,decision=oracle.NONE)],
        decisions=ledger,pool_events=[],supervision=dict(host_pid=123,joined=3,completions=3,destroyed=[[i,0] for i in range(3)],statuses=[[i,1,0] for i in range(3)],cancellations=[],error=None,elapsed_ms=1))
    return record, cfg, [(1 << 8) | owner for owner in owners]


def check(record, cfg, trace):
    return oracle.decisions(record,cfg,trace,lambda key,at,done:(7,7,0,1,1),{oracle.ADMIT})


class ScheduleReplayTests(unittest.TestCase):
    def test_atomic_token_seeds_preserve_full_width_scheduling_seeds(self):
        configs=[oracle.configuration(3,4,i) for i in range(4)]
        self.assertEqual([c['token_seed'] for c in configs],[1,37,0x5678,0x79b9])
        self.assertEqual([c['seed'] for c in configs],list(oracle.SEEDS))

    def test_declared_draws_bind_to_actual_actions(self):
        self.assertEqual(check(*example()),3)

    def test_actor_rotation_and_real_capacity(self):
        for family in range(4):
            for n in range(1,5):
                configs=[oracle.configuration(family,n,i) for i in range(4)]
                self.assertEqual({c['actor'] for c in configs},set(range(n)))
                self.assertTrue(all((c['pool_limit']<n)==(family<2 and n>1) for c in configs))

    def test_log_corruption_cannot_become_replay(self):
        changes = {
            'destroy failure':lambda r:r['supervision']['destroyed'][0].__setitem__(1,1),
            'missing join':lambda r:r['supervision'].__setitem__('joined',2),
            'hidden cancellation':lambda r:r['supervision']['cancellations'].append(0),
            'seed':lambda r:r['config'].__setitem__('seed',37),
            'choice':lambda r:r['decisions'][0].__setitem__('chosen',1),
            'eligibility':lambda r:r['decisions'][0].__setitem__('eligible',1),
            'phase':lambda r:r['decisions'][0].__setitem__('phase',2),
            'epoch':lambda r:r['decisions'][0].__setitem__('epoch',2),
            'claim position':lambda r:r['decisions'][0].__setitem__('claim_trace_before',1),
            'no claim':lambda r:r['decisions'][0].__setitem__('claimed',0),
            'prior done':lambda r:r['decisions'][1].__setitem__('done_before',0),
            'PRNG':lambda r:r['decisions'][1].__setitem__('prng_before',37),
            'repeat grant':lambda r:r['decisions'].append(copy.deepcopy(r['decisions'][0])),
            'missing owner':lambda r:r['cuts'][0].__setitem__('completed',3),
            'duplicate cut':lambda r:r['cuts'].append(copy.deepcopy(r['cuts'][0])),
        }
        for label,change in changes.items():
            with self.subTest(label=label):
                record,cfg,trace=example()
                record=copy.deepcopy(record)
                change(record)
                with self.assertRaises(ValueError):check(record,cfg,trace)

    def test_self_consistent_log_does_not_authorize_ineligible_owner(self):
        record,cfg,trace=example()
        with self.assertRaises(ValueError):
            oracle.decisions(record,cfg,trace,lambda key,at,done:(7,6,0,1,1),{oracle.ADMIT})

    def test_actual_action_owner_must_match(self):
        record,cfg,trace=example()
        trace[1],trace[2]=trace[2],trace[1]
        with self.assertRaises(ValueError):check(record,cfg,trace)


if __name__ == '__main__': unittest.main()
