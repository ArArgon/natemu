import copy
from dataclasses import replace
import unittest

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # tools: runnable by path from the repo root
from models import coherence_model as m


class CoherenceReferenceTests(unittest.TestCase):
    def test_three_modes_fresh_read_then_write(self):
        for mode in m.MODES:
            tables, context, layout = m.fixture(mode)
            leaf = layout['first'] + 256 * layout['entry_bytes']
            read = m.walk(tables, context, 0x100023, 'read')
            self.assertEqual((read.route.gpa, read.route.leaf_bytes, read.route.dirty), (0x100023, 4096, False))
            self.assertEqual(read.tables[leaf], 0x100027)
            self.assertEqual(len(read.reads), {'legacy32': 2, 'pae': 2, 'four': 4}[mode])
            write = m.walk(read.tables, context, 0x100023, 'write')
            self.assertEqual(write.tables[leaf], 0x100067)
            self.assertEqual(write.writes, [(leaf, 0x100027, 0x100067)])
            self.assertEqual(tables[leaf], 0x100007)  # Input snapshots are immutable.

    def test_cleared_ad_without_invalidation_permits_both_histories(self):
        for mode in m.MODES:
            tables, context, layout = m.fixture(mode)
            first = m.walk(tables, context, 0x100000, 'write')
            cache = m.Cache(); cache.remember(context, first)
            leaf = first.route.leaf_address
            cleared = dict(first.tables); cleared[leaf] &= ~(m.A | m.D)
            permitted = m.access_outcomes(cleared, context, 0x100000, 'write', cache)
            self.assertEqual([p.policy for p in permitted], ['cached', 'fresh'])
            self.assertEqual([p.tables[leaf] & 0x60 for p in permitted], [0, 0x60])
            self.assertEqual(permitted[0].reads + permitted[0].writes, [])
            for observed in permitted:
                m.check_observation(observed, permitted)
            self.assertEqual(m.access_outcomes(cleared, context, 0x100000, 'write', cache, policy='fresh'), permitted[1:])
            self.assertEqual(m.access_outcomes(cleared, context, 0x100000, 'write', cache, policy='cached'), permitted[:1])

    def test_explicit_invalidation_requires_current_walk_and_new_ad(self):
        for mode in m.MODES:
            tables, context, layout = m.fixture(mode)
            first = m.walk(tables, context, 0x100000, 'write')
            cache = m.Cache(); cache.remember(context, first)
            cleared = dict(first.tables); cleared[first.route.leaf_address] &= ~0x60
            stale = m.access_outcomes(cleared, context, 0x100000, 'write', cache, policy='cached')[0]
            required = m.access_outcomes(cleared, context, 0x100000, 'write', cache, invalidated=True)
            self.assertEqual([r.policy for r in required], ['fresh'])
            with self.assertRaises(ValueError):
                m.check_observation(stale, required)
            cache.invalidate(0x100000, 4096)
            read = m.access_outcomes(cleared, context, 0x100000, 'read', cache)[0]
            self.assertEqual(read.tables[first.route.leaf_address] & 0x60, 0x20)
            cache.remember(context, read)
            write = m.access_outcomes(read.tables, context, 0x100000, 'write', cache)
            self.assertEqual([r.policy for r in write], ['fresh'])
            self.assertTrue(write[0].route.dirty)

    def test_shared_parent_replacement_and_aba_do_not_reuse_old_route(self):
        for mode in m.MODES:
            tables, context, layout = m.fixture(mode)
            old = m.walk(tables, context, 0x100080, 'write')
            caches = [m.Cache() for _ in range(4)]
            for cache in caches:
                cache.remember(context, old)
            changed = dict(old.tables)
            changed[layout['parent']] = layout['second'] | 7
            for cache in caches:
                before = m.access_outcomes(changed, context, 0x100080, 'read', cache)
                self.assertEqual([r.route.gpa for r in before], [0x100080, 0x101080])
                cache.invalidate()
                new = m.access_outcomes(changed, context, 0x100080, 'read', cache)[0]
                self.assertEqual(new.route.gpa, 0x101080)
                cache.remember(context, new)
            changed = dict(new.tables); changed[layout['parent']] = layout['first'] | 7
            for cache in caches:
                cache.invalidate()
                self.assertEqual(m.access_outcomes(changed, context, 0x100080, 'read', cache)[0].route.gpa, 0x100080)

    def test_parent_permission_restriction_faults_only_fresh_required_route(self):
        for mode in m.MODES:
            tables, context, layout = m.fixture(mode)
            old = m.walk(tables, context, 0x100000, 'write')
            cache = m.Cache(); cache.remember(context, old)
            changed = dict(old.tables); changed[layout['parent']] &= ~m.RW
            permitted = m.access_outcomes(changed, context, 0x100000, 'write', cache)
            self.assertEqual([(r.policy, r.vector, r.error) for r in permitted], [('cached', 0, 0), ('fresh', 14, 3)])
            fresh = m.access_outcomes(changed, context, 0x100000, 'write', cache, invalidated=True)[0]
            self.assertEqual((fresh.vector, fresh.error, fresh.address), (14, 3, 0x100000))
            self.assertIsNone(fresh.route)
            allowed = m.walk(changed, replace(context, wp=False), 0x100000, 'write')
            self.assertTrue(allowed.route.writable)
            user = m.walk(changed, replace(context, wp=False, cpl=3), 0x100000, 'write')
            self.assertEqual((user.vector, user.error), (14, 7))

    def test_2m_promotion_and_demotion_in_both_supported_modes(self):
        for mode in ('pae', 'four'):
            tables, context, layout = m.fixture(mode)
            # Begin with the swapped small-page mapping, then promote identity.
            tables[layout['parent']] = layout['second'] | 7
            cache = m.Cache()
            for address in (0x100000, 0x101000):
                result = m.walk(tables, context, address, 'write')
                tables = result.tables; cache.remember(context, result)
            promoted = dict(tables); promoted[layout['parent']] = 0x87
            cache.invalidate(0, 1 << 21)
            for address in (0x100023, 0x101057):
                result = m.access_outcomes(promoted, context, address, 'write', cache)[0]
                self.assertEqual((result.route.gpa, result.route.leaf_bytes, result.route.leaf_address),
                                 (address, 1 << 21, layout['parent']))
                self.assertNotIn(layout['second'] + ((address >> 12) & 511) * 8, result.reads)
                self.assertEqual(result.tables[layout['parent']], 0xe7)
                for at in range(layout['second'], layout['second'] + 4096, 8):
                    self.assertEqual(result.tables[at], promoted[at])
                cache.remember(context, result)
            demoted = dict(result.tables); demoted[layout['parent']] = layout['second'] | 7
            cache.invalidate(0, 1 << 21)
            self.assertEqual(m.access_outcomes(demoted, context, 0x100023, 'read', cache)[0].route.gpa, 0x101023)
            self.assertEqual(m.access_outcomes(demoted, context, 0x101057, 'read', cache)[0].route.gpa, 0x100057)

    def test_partial_native_invalidation_does_not_cover_whole_guest_large_leaf(self):
        tables, context, layout = m.fixture('four')
        tables[layout['parent']] = 0x87
        cache = m.Cache()
        for address in (0x100000, 0x101000):
            r = m.walk(tables, context, address, 'write'); tables = r.tables
            cache.remember(context, r)
        tables[layout['parent']] = layout['second'] | 7
        cache.invalidate(0x100000, 4096)
        stale = m.access_outcomes(tables, context, 0x101000, 'read', cache, policy='cached')[0]
        required = m.access_outcomes(tables, context, 0x101000, 'read', cache, invalidated=True)
        self.assertEqual(stale.route.gpa, 0x101000)
        self.assertEqual(required[0].route.gpa, 0x100000)
        with self.assertRaises(ValueError):
            m.check_observation(stale, required)

    def test_large_readonly_and_reserved_alignment_do_not_commit_leaf_ad(self):
        for mode in ('pae', 'four'):
            tables, context, layout = m.fixture(mode)
            tables[layout['parent']] = 0x85
            denied = m.walk(tables, context, 0x101234, 'write')
            self.assertEqual((denied.vector, denied.error), (14, 3))
            self.assertEqual(denied.tables[layout['parent']], 0x85)
            read = m.walk(tables, context, 0x101234, 'read')
            self.assertEqual(read.tables[layout['parent']], 0xa5)
            bypass = m.walk(tables, replace(context, wp=False), 0x101234, 'write')
            self.assertEqual(bypass.tables[layout['parent']], 0xe5)
            tables[layout['parent']] = 0x2087  # Reserved address bit in a 2MiB leaf.
            for access, error in (('read', 9), ('write', 11)):
                invalid = m.walk(tables, context, 0x101234, access)
                self.assertEqual((invalid.vector, invalid.error), (14, error))
                self.assertEqual(invalid.tables[layout['parent']], 0x2087)

    def test_pae_memory_root_mutation_and_invlpg_never_reload_hidden(self):
        tables, context, layout = m.fixture('pae')
        old = m.walk(tables, context, 0x100000, 'read')
        cache = m.Cache(); cache.remember(context, old)
        changed = dict(old.tables)
        changed[0x1020] = 0x5001
        changed[0x5000] = layout['second'] | 7
        cache.invalidate()
        same = m.access_outcomes(changed, context, 0x100000, 'read', cache)[0]
        self.assertEqual(same.route.gpa, 0x100000)
        self.assertNotIn(0x1020, same.reads)
        new_hidden, addresses = m.load_hidden(changed, context.cr3, context.hidden)
        self.assertEqual(addresses, [0x1020, 0x1028, 0x1030, 0x1038])
        self.assertEqual(new_hidden.generation, 2)
        new = m.walk(changed, replace(context, generation=2, hidden=new_hidden), 0x100000, 'read')
        self.assertEqual(new.route.gpa, 0x101000)
        self.assertEqual(context.hidden.entries[0], 0x2001)

    def test_pae_invalid_unused_slot_rejects_whole_snapshot_nonpresent_bits_ignored(self):
        tables, context, _ = m.fixture('pae')
        tables[0x1020], tables[0x1038] = 0x4001, 0x21
        hidden, reads = m.load_hidden(tables, context.cr3, context.hidden)
        self.assertIsNone(hidden)
        self.assertEqual(len(reads), 4)
        self.assertEqual(context.hidden.entries, (0x2001, 0, 0, 0))
        tables[0x1020], tables[0x1038] = 0xfffffffffffffffe, 0
        hidden, _ = m.load_hidden(tables, context.cr3, context.hidden)
        result = m.walk(tables, replace(context, hidden=hidden), 0x100000, 'read')
        self.assertEqual((result.vector, result.error, result.reads), (14, 0, []))

    def test_context_generation_prevents_same_tuple_stale_cache(self):
        tables, context, layout = m.fixture('four')
        old = m.walk(tables, context, 0x100000, 'write')
        cache = m.Cache(); cache.remember(context, old)
        tables = dict(old.tables); tables[layout['parent']] = layout['second'] | 7
        renewed = replace(context, generation=context.generation + 1)
        outcomes = m.access_outcomes(tables, renewed, 0x100000, 'read', cache)
        self.assertEqual([(r.policy, r.route.gpa) for r in outcomes], [('fresh', 0x101000)])

    def test_upper_alias_shares_table_bytes_but_not_cache_identity(self):
        tables, context, _ = m.fixture('four')
        high = 0xffff800000100000
        a = m.walk(tables, context, high, 'write')
        b = m.walk(a.tables, context, 0x100000, 'read')
        self.assertEqual(a.route.gpa, b.route.gpa)
        self.assertEqual(a.route.leaf_address, b.route.leaf_address)
        cache = m.Cache(); cache.remember(context, a); cache.remember(context, b)
        cache.invalidate(0x100000, 4096)
        self.assertEqual(len(cache.entries), 1)
        self.assertEqual(next(iter(cache.entries))[1], high)

    def test_present_reserved_nx_smep_and_legacy_ps_contracts(self):
        tables, context, layout = m.fixture('four')
        leaf = layout['first'] + 256 * 8
        for entry, access, error in ((0, 'read', 0), (0x100007 | (1 << 40), 'read', 9),
                                      (0x100007 | m.NX, 'fetch', 17)):
            changed = dict(tables); changed[leaf] = entry
            r = m.walk(changed, context, 0x100000, access)
            self.assertEqual((r.vector, r.error), (14, error))
            self.assertEqual(r.tables[leaf], entry)
        self.assertEqual(m.walk(tables, replace(context, smep=True), 0x100000, 'fetch').error, 17)
        tables, context, layout = m.fixture('legacy32')
        tables[layout['parent']] |= m.PS
        self.assertEqual(m.walk(tables, context, 0x100000, 'read').route.leaf_bytes, 4096)

    def test_corrupted_effect_oracles_and_unowned_backing_fail(self):
        tables, context, layout = m.fixture('four')
        good = m.walk(tables, context, 0x100000, 'read')
        bad = copy.deepcopy(good); bad.tables[good.route.leaf_address] |= m.D
        with self.assertRaises(ValueError):
            m.check_observation(bad, [good])
        bad = copy.deepcopy(good); bad.route = replace(bad.route, gpa=0x101000)
        with self.assertRaises(ValueError):
            m.check_observation(bad, [good])
        tables[layout['parent']] = 0x9007
        with self.assertRaisesRegex(ValueError, 'unowned'):
            m.walk(tables, context, 0x100000, 'read')



class DeclaredPolicyTests(unittest.TestCase):
    """Contracts §3 items 4 and 12: choices the model declares as natemu policy and CPU profile."""
    def four(self, leaf=None, level=None, bits=0):
        tables, context, layout = m.fixture('four')
        at = {1: layout['first'] + 256 * 8, 2: layout['parent'], 3: 0x2000}[level] if level else None
        if at is not None:
            tables[at] = (tables[at] | bits) if leaf is None else leaf
        return tables, replace(context, cpl=3), layout

    def test_faulting_access_sets_parent_a_not_leaf_a(self):
        tables, context, layout = self.four(level=1, leaf=0x100005)          # leaf read-only at CPL3
        leaf = layout['first'] + 256 * 8
        r = m.walk(tables, context, 0x100000, 'write')
        self.assertEqual((r.vector, r.error), (14, 7))
        self.assertEqual(r.tables[leaf] & m.A, 0)
        self.assertTrue(all(r.tables[p] & m.A for p in (0x1000, 0x2000, layout['parent'])))
        saved, m.LEAF_A_ON_FAULT = m.LEAF_A_ON_FAULT, True
        try:
            self.assertEqual(m.walk(tables, context, 0x100000, 'write').tables[leaf] & m.A, m.A)
        finally:
            m.LEAF_A_ON_FAULT = saved

    def test_maxphyaddr_40_and_no_1gib_pages(self):
        self.assertEqual(m.MAXPHYADDR, 40)
        r = m.walk(*self.four(level=1, bits=1 << 40)[:2], 0x100000, 'read')
        self.assertEqual((r.vector, r.error), (14, 13))                      # RSVD | P | U
        r = m.walk(*self.four(level=1, bits=1 << 39)[:2], 0x100000, 'read')
        self.assertEqual((r.vector, r.route.gpa), (0, 0x8000100000))
        r = m.walk(*self.four(level=3, bits=m.PS)[:2], 0x100000, 'read')     # PS in the PDPTE
        self.assertEqual((r.vector, r.error), (14, 13))

    def test_pwt_pcd_or_pat_make_the_route_non_write_back(self):
        self.assertTrue(m.walk(*self.four()[:2], 0x100000, 'read').route.wb)
        for level, bit in ((1, 8), (1, 16), (1, m.PS), (2, 8), (3, 16)):
            self.assertFalse(m.walk(*self.four(level=level, bits=bit)[:2], 0x100000, 'read').route.wb, (level, bit))

if __name__ == '__main__':
    unittest.main()
