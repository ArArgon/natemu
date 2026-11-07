"""Deterministic counterexample for resident two-page access vs split-lock owners.

An initial owner of B requests our shadow reset. Once we acknowledge, it finishes
and an owner of A starts while our holding bit is zero. Checking A only before
waiting on B lets the old algorithm access A while its new owner is active.
"""
import unittest


class Handoff:
    def __init__(self):
        self.holding = True
        self.owner = 'B'
        self.serves = 0
        self.trace = []

    def mark(self):
        self.holding = True
        self.trace.append(('mark', self.owner))

    def held(self, page):
        return self.owner == page

    def serve(self):
        self.serves += 1
        self.holding = False
        if self.serves == 1:
            # B receives its requested reset, completes, and A acquires without
            # waiting for us: it observes holding=0 before we mark it again.
            self.owner = 'A'
        else:
            # Let A finish so a correct waiter eventually returns.
            self.owner = None
        self.trace.append(('reset-and-handoff', self.owner))

    def access(self, pages):
        assert self.holding and self.owner not in pages, self.trace


def wait_for_set(state, pages):
    while True:
        state.mark()
        if not any(state.held(page) for page in pages):
            return
        while any(state.held(page) for page in pages):
            state.serve()


class SplitWaitTest(unittest.TestCase):
    def test_old_sequential_wait_has_counterexample(self):
        state = Handoff()
        for page in ('A', 'B'):
            wait_for_set(state, (page,))
        self.assertEqual(state.serves, 1)
        with self.assertRaises(AssertionError):
            state.access(('A', 'B'))

    def test_combined_wait_rechecks_first_page_after_reset(self):
        state = Handoff()
        wait_for_set(state, ('A', 'B'))
        self.assertEqual(state.serves, 2)
        state.access(('A', 'B'))

    def test_unrelated_single_page_does_not_wait(self):
        state = Handoff()
        wait_for_set(state, ('C',))
        self.assertEqual(state.serves, 0)
        state.access(('C',))


if __name__ == '__main__':
    unittest.main()
