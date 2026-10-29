import itertools
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # tools: runnable by path from the repo root
from models import ordering_model as model


class BoundedTSOTests(unittest.TestCase):
    def test_exact_independent_litmus_outcome_sets(self):
        bits = lambda width: set(itertools.product((0, 1), repeat=width))
        expected = {'sb': bits(2), 'mp': bits(2) - {(1, 0)}, 'lb': bits(2) - {(1, 1)},
                    'wrc': bits(3) - {(1, 1, 0)}, 'iriw': bits(4) - {(1, 0, 1, 0)},
                    'sb-mfence': bits(2) - {(0, 0)}, 'sb-xchg': bits(2) - {(0, 0)}}
        for name, outcomes in expected.items():
            with self.subTest(profile=name):
                result = model.enumerate_outcomes(name)
                self.assertEqual(result.outcomes, outcomes)
                self.assertLess(result.states, 1000)
                self.assertEqual(set(result.witnesses), outcomes)
                for outcome, path in result.witnesses.items():
                    actual, _ = model.replay_witness(name, path)
                    self.assertEqual(actual, outcome)

    def test_sb_all_zero_is_possible_but_not_required_in_a_sample(self):
        path = (('execute', 0), ('execute', 1), ('execute', 0), ('execute', 1), ('flush', 0), ('flush', 1))
        self.assertEqual(model.replay_witness('sb', path)[0], (0, 0))
        self.assertEqual(model.check_histogram('sb', [((1, 1), 100)], 100), 100)
        self.assertEqual(model.check_histogram('sb', [((0, 0), 1), ((1, 1), 99)], 100), 100)

    def test_fifo_store_visibility_is_load_bearing(self):
        ordinary = model._transitions

        def broken_fifo(test, state, addresses, registers):
            yield from ordinary(test, state, addresses, registers)
            for cpu, buffer in enumerate(state.buffers):
                if len(buffer) >= 2:
                    address, value = buffer[1]
                    yield ('bad-flush', cpu), model.State(
                        state.pcs, model.replace_at(state.buffers, cpu, buffer[:1] + buffer[2:]),
                        model.replace_at(state.memory, address, value), state.registers)

        with patch.object(model, '_transitions', broken_fifo):
            weak = model.enumerate_outcomes('mp').outcomes
        self.assertIn((1, 0), weak)
        with self.assertRaisesRegex(ValueError, 'forbidden outcome'):
            model.check_histogram('mp', [((1, 0), 1)], 1)

    def test_in_order_reads_and_read_before_write_are_load_bearing(self):
        lb = model.PROFILES['lb']
        weak = model.Litmus('reordered-lb', tuple(tuple(reversed(p)) for p in lb.programs), lb.initial, lb.outputs)
        self.assertIn((1, 1), model.enumerate_outcomes(weak).outcomes)
        with self.assertRaises(ValueError):
            model.check_outcome('lb', (1, 1))
        iriw = model.PROFILES['iriw']
        weak = model.Litmus('reordered-iriw', iriw.programs[:2] + (tuple(reversed(iriw.programs[2])), iriw.programs[3]),
                            iriw.initial, iriw.outputs)
        self.assertIn((1, 0, 1, 0), model.enumerate_outcomes(weak).outcomes)
        with self.assertRaises(ValueError):
            model.check_outcome('iriw', (1, 0, 1, 0))

    def test_removing_fence_or_locked_drain_exposes_forbidden_sb(self):
        fenced = model.PROFILES['sb-mfence']
        weak = model.Litmus('removed-mfence', tuple(tuple(op for op in p if op[0] != 'fence') for p in fenced.programs),
                            fenced.initial, fenced.outputs)
        self.assertIn((0, 0), model.enumerate_outcomes(weak).outcomes)
        locked = model.PROFILES['sb-xchg']
        weak = model.Litmus('not-locked', tuple(tuple(('store', op[2], op[3]) if op[0] == 'xchg' else op for op in p)
                                               for p in locked.programs), locked.initial, locked.outputs)
        self.assertIn((0, 0), model.enumerate_outcomes(weak).outcomes)
        for name in ('sb-mfence', 'sb-xchg'):
            with self.subTest(profile=name), self.assertRaises(ValueError):
                model.check_outcome(name, (0, 0))

    def test_youngest_same_address_forwarding_and_other_address_bypass(self):
        test = model.Litmus('forward',
                            ((('store', 'x', 1), ('store', 'x', 2), ('load', 'own', 'x'), ('load', 'other', 'y')),),
                            (('x', 0), ('y', 9)), ((0, 'own'), (0, 'other')))
        result = model.enumerate_outcomes(test)
        self.assertEqual(result.outcomes, {(2, 9)})
        path = (('execute', 0),) * 4 + (('flush', 0),) * 2
        outcome, final = model.replay_witness(test, path)
        self.assertEqual(outcome, (2, 9))
        self.assertEqual(final.memory, (2, 9))
        with self.assertRaises(ValueError):
            model.check_outcome(test, (0, 9))
        with self.assertRaises(ValueError):
            model.check_outcome(test, (1, 9))

    def test_fence_and_xchg_do_not_drain_another_owners_buffer(self):
        for op in (('fence',), ('xchg', 'old', 'd', 3)):
            test = model.Litmus('local-drain', ((op, ('load', 'r', 'x')),
                                              (('store', 'x', 1),)),
                                (('d', 0), ('x', 0)), ((0, 'r'),))
            addresses, registers = model.validate(test)
            state = model.initial_state(test)
            state = dict(model._transitions(test, state, addresses, registers))[('execute', 1)]
            after = dict(model._transitions(test, state, addresses, registers))[('execute', 0)]
            self.assertEqual(after.buffers[1], state.buffers[1])
            self.assertTrue(after.buffers[1])
            self.assertEqual(model.enumerate_outcomes(test).outcomes, {(0,), (1,)})

    def test_locked_exchange_has_one_global_effect_and_returns_old_word(self):
        test = model.Litmus('exchange-after-store',
                            ((('store', 'x', 7), ('xchg', 'old', 'x', 8), ('load', 'new', 'x')),),
                            (('x', 0),), ((0, 'old'), (0, 'new')))
        self.assertEqual(model.enumerate_outcomes(test).outcomes, {(7, 8)})
        addresses, registers = model.validate(test)
        first = dict(model._transitions(test, model.initial_state(test), addresses, registers))[('execute', 0)]
        self.assertNotIn(('execute', 0), dict(model._transitions(test, first, addresses, registers)))
        for outcome, path in model.enumerate_outcomes('sb-xchg').witnesses.items():
            actual, state = model.replay_witness('sb-xchg', path)
            self.assertEqual(actual, outcome)
            self.assertEqual(state.memory[:2], (1, 2))
            _, registers = model.validate(model.PROFILES['sb-xchg'])
            self.assertEqual(state.registers[0][registers[0].index('old0')], 0)
            self.assertEqual(state.registers[1][registers[1].index('old1')], 0)

    def test_register_dependencies_are_captured_at_instruction_execution(self):
        test = model.Litmus('data-dependency',
                            ((('load', 'a', 'x'), ('store', 'y', 'a'), ('xchg', 'a', 'x', 'a')),),
                            (('x', 7), ('y', 0)), ((0, 'a'),))
        result = model.enumerate_outcomes(test)
        outcome, state = model.replay_witness(test, result.witnesses[(7,)])
        self.assertEqual(outcome, (7,))
        self.assertEqual(state.memory, (7, 7))

    def test_bound_exhaustion_never_returns_a_partial_allowed_set(self):
        with self.assertRaisesRegex(ValueError, 'bound exceeded'):
            model.enumerate_outcomes('iriw', max_states=10)
        with self.assertRaises(ValueError):
            model.enumerate_outcomes('sb', max_states=True)
        with self.assertRaises(ValueError):
            model.replay_witness('sb', (('flush', 0),))
        with self.assertRaises(ValueError):
            model.replay_witness('sb', (('execute', 0),))

    def test_histogram_rejects_missing_duplicate_and_corrupted_results(self):
        cases = [([((1, 0), 1)], 1), ([((1, 1), 2)], 3), ([((1, 1), 1), ((1, 1), 1)], 2),
                 ([((2, 0), 1)], 1), ([((1,), 1)], 1), ([((True, 1), 1)], 1),
                 ([((1, 1), True)], 1), ([((1, 1), -1)], 1)]
        for histogram, trials in cases:
            with self.subTest(histogram=histogram), self.assertRaises(ValueError):
                model.check_histogram('mp', histogram, trials)
        # A fixed rectangular table may contain zero counts in forbidden bins.
        self.assertEqual(model.check_histogram('mp', [((1, 0), 0), ((1, 1), 10)], 10), 10)

    def test_outcome_sets_do_not_depend_on_owner_labels(self):
        test = model.PROFILES['wrc']
        expected = model.enumerate_outcomes(test).outcomes
        for order in itertools.permutations(range(3)):
            permuted = model.Litmus('rotated-wrc', tuple(test.programs[i] for i in order), test.initial,
                                   tuple((order.index(cpu), reg) for cpu, reg in test.outputs))
            self.assertEqual(model.enumerate_outcomes(permuted).outcomes, expected)


class ExchangeChainTests(unittest.TestCase):
    def test_every_one_to_four_owner_serialization(self):
        for n in range(1, 5):
            tokens = {cpu: 0x11220000 + cpu for cpu in range(n)}
            for order in itertools.permutations(tokens):
                returned, previous = {}, 0
                for cpu in order:
                    returned[cpu] = previous
                    previous = tokens[cpu]
                self.assertEqual(model.check_xchg_chain(tokens, returned, previous), order)

    def test_chain_rejects_replayed_exchange_and_disconnected_cycles(self):
        cases = [({0: 1}, {0: 1}, 1),                       # same token exchanged twice
                 ({0: 1}, {0: 0}, 0),                       # replay with returned register restores initial memory
                 ({0: 1, 1: 2}, {0: 2, 1: 1}, 1),         # peer exchanged between commit and erroneous replay
                 ({0: 1, 1: 2, 2: 3}, {0: 0, 1: 3, 2: 2}, 1),  # valid-looking head hides a cycle
                 ({0: 1, 1: 2}, {0: 0, 1: 0}, 2),         # torn/non-atomic old-value observations
                 ({0: 1, 1: 2}, {0: 0, 1: 1}, 1),         # wrong final memory
                 ({0: 1, 1: 2}, {0: 0, 1: 99}, 2),        # fabricated predecessor
                 ({0: 1, 1: 1}, {0: 0, 1: 1}, 1),         # non-unique tokens
                 ({0: 1, 1: 2}, {0: 0}, 1),                # omitted owner
                 ({0: 0}, {0: 0}, 0)]                      # token aliases initial sentinel
        for tokens, returned, final in cases:
            with self.subTest(tokens=tokens, returned=returned), self.assertRaises(ValueError):
                model.check_xchg_chain(tokens, returned, final)
        with self.assertRaises(ValueError):
            model.check_xchg_chain({0: 1}, {0: False}, 1)

    def test_final_memory_outputs(self):
        # 2+2W: both final values 1 would need each first store after the other's second (a cycle): forbidden.
        two = model.Litmus('2+2w', ((('store', 'x', 1), ('store', 'y', 2)), (('store', 'y', 1), ('store', 'x', 2))),
                           (('x', 0), ('y', 0)), (('final', 'x'), ('final', 'y')))
        self.assertEqual(model.enumerate_outcomes(two).outcomes, {(1, 2), (2, 1), (2, 2)})
        # One exchange chain: every allowed outcome (returned values, final word) is one linear order.
        chain = model.Litmus('xchg', ((('xchg', 'o0', 't', 1),), (('xchg', 'o1', 't', 2),), (('xchg', 'o2', 't', 3),)),
                             (('t', 0),), ((0, 'o0'), (1, 'o1'), (2, 'o2'), ('final', 't')))
        outcomes = model.enumerate_outcomes(chain).outcomes
        self.assertEqual(len(outcomes), 6)
        for o in outcomes:
            model.check_xchg_chain({0: 1, 1: 2, 2: 3}, dict(enumerate(o[:3])), o[3])
        with self.assertRaises(ValueError):
            model.validate(model.Litmus('bad', ((('store', 'x', 1),),), (('x', 0),), (('final', 'y'),)))


class LockedReadModifyWriteTests(unittest.TestCase):
    """I3: ('rmw', destination, address, function, operand), a LOCKed read-modify-write."""
    SB = model.Litmus('sb-rmw', ((('store', 'x', 1), ('rmw', None, 'd0', 'add', 1), ('load', 'r0', 'y')),
                                 (('store', 'y', 1), ('rmw', 'o1', 'd1', 'or', 4), ('load', 'r1', 'x'))),
                      (('d0', 0), ('d1', 0), ('x', 0), ('y', 0)), ((0, 'r0'), (1, 'r1')))

    def test_locked_rmw_is_a_full_barrier_in_sb(self):
        self.assertEqual(model.enumerate_outcomes(self.SB).outcomes, {(0, 1), (1, 0), (1, 1)})
        with self.assertRaises(ValueError):
            model.check_outcome(self.SB, (0, 0))

    def test_counter_chain_returns_one_linear_order(self):
        # n LOCK XADD +1 on one counter: the returned words are a permutation of 0..n-1 and the counter ends at n.
        for n in (2, 3, 4):
            chain = model.Litmus(f'xadd@{n}', tuple((('rmw', f'o{i}', 'c', 'add', 1),) for i in range(n)), (('c', 0),),
                                 tuple((i, f'o{i}') for i in range(n)) + (('final', 'c'),))
            outcomes = model.enumerate_outcomes(chain).outcomes
            self.assertEqual(outcomes, {perm + (n,) for perm in itertools.permutations(range(n))})

    def test_compare_and_swap_has_one_winner(self):
        race = model.Litmus('cas@3', tuple((('rmw', f'o{i}', 'c', 'cas', (0, i + 1)),) for i in range(3)), (('c', 0),),
                            tuple((i, f'o{i}') for i in range(3)) + (('final', 'c'),))
        for o in model.enumerate_outcomes(race).outcomes:
            winners = [i for i in range(3) if o[i] == 0]
            self.assertEqual(len(winners), 1, o)
            self.assertEqual(o[3], winners[0] + 1)
            self.assertTrue(all(o[i] == o[3] for i in range(3) if i not in winners))

    def test_functions_and_old_word(self):
        for function, operand, new in [('add', 0xffffffff, 4), ('and', 6, 4), ('or', 1, 5), ('xor', 7, 2), ('neg', 0, 0xfffffffb),
                                       ('cas', (5, 9), 9), ('cas', (4, 9), 5)]:
            test = model.Litmus('one', ((('rmw', 'old', 'c', function, operand), ('load', 'now', 'c')),), (('c', 5),),
                                ((0, 'old'), (0, 'now')))
            self.assertEqual(model.enumerate_outcomes(test).outcomes, {(5, new)}, function)

    def test_rmw_waits_for_its_own_buffer_and_writes_memory_directly(self):
        test = model.Litmus('rmw-after-store', ((('store', 'x', 7), ('rmw', 'old', 'x', 'add', 1)), (('load', 'r', 'x'),)),
                            (('x', 0),), ((0, 'old'), (1, 'r')))
        addresses, registers = model.validate(test)
        first = dict(model._transitions(test, model.initial_state(test), addresses, registers))[('execute', 0)]
        self.assertNotIn(('execute', 0), dict(model._transitions(test, first, addresses, registers)))   # buffer not empty
        self.assertEqual(model.enumerate_outcomes(test).outcomes, {(7, 0), (7, 7), (7, 8)})
        flushed = dict(model._transitions(test, first, addresses, registers))[('flush', 0)]
        after = dict(model._transitions(test, flushed, addresses, registers))[('execute', 0)]
        self.assertEqual((after.buffers[0], after.memory), ((), (8,)))

    def test_positive_controls_atomicity_and_drain_are_load_bearing(self):
        ordinary = model._transitions

        def no_drain(test, state, addresses, registers):     # an rmw that ignores its own buffer (as a plain RMW would)
            yield from ordinary(test, state, addresses, registers)
            for cpu, program in enumerate(test.programs):
                pc = state.pcs[cpu]
                if pc < len(program) and program[pc][0] == 'rmw' and state.buffers[cpu]:
                    op = program[pc]; a = addresses.index(op[2]); old = state.memory[a]
                    regs = state.registers
                    if op[1] is not None:
                        regs = model.replace_at(regs, cpu, model.replace_at(regs[cpu], registers[cpu].index(op[1]), old))
                    yield ('execute', cpu), model.State(model.replace_at(state.pcs, cpu, pc + 1), state.buffers,
                                                        model.replace_at(state.memory, a, model.RMW_FUNCTIONS[op[3]](old, op[4])), regs)
        with patch.object(model, '_transitions', no_drain):
            self.assertIn((0, 0), model.enumerate_outcomes(self.SB).outcomes)
        # Two loads may both read 0 before either store (what lets a non-atomic increment lose an update); the locked adds'
        # exact outcome set below, with no (0, 0, 1), is the load-bearing atomicity check.
        split = model.Litmus('split-add', tuple((('load', f'o{i}', 'c'), ('store', 'c', i + 1)) for i in range(2)), (('c', 0),),
                             ((0, 'o0'), (1, 'o1'), ('final', 'c')))
        self.assertIn((0, 0, 2), model.enumerate_outcomes(split).outcomes)     # both read 0: one update lost
        locked = model.Litmus('locked-add', tuple((('rmw', f'o{i}', 'c', 'add', 1),) for i in range(2)), (('c', 0),),
                              ((0, 'o0'), (1, 'o1'), ('final', 'c')))
        self.assertNotIn((0, 0, 1), model.enumerate_outcomes(locked).outcomes)
        self.assertEqual(model.enumerate_outcomes(locked).outcomes, {(0, 1, 2), (1, 0, 2)})

    def test_rmw_shapes_are_validated(self):
        for op in (('rmw', 'o', 'c', 'mul', 2), ('rmw', 'o', 'c', 'cas', 1), ('rmw', 'o', 'c', 'add', 1 << 32), ('rmw', 'o', 'z', 'add', 1),
                   ('rmw', '', 'c', 'add', 1), ('rmw', 'o', 'c', 'add')):
            with self.subTest(op=op), self.assertRaises(ValueError):
                model.validate(model.Litmus('bad', ((op,),), (('c', 0),), ((0, 'o'),) if op[1] else (('final', 'c'),)))


if __name__ == '__main__':
    unittest.main()
