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

    def test_cr3_pwt_pcd_accepted_and_do_not_change_the_walk(self):
        for mode in m.MODES:
            tables, context, layout = m.fixture(mode)
            base = m.walk(tables, context, 0x100023, 'read')
            for bits in (0x08, 0x10, 0x18):
                got = m.walk(tables, replace(context, cr3=context.cr3 | bits), 0x100023, 'read')
                self.assertEqual((got.vector, got.route, got.reads), (0, base.route, base.reads), (mode, bits))
        tables, context, _ = m.fixture('four')
        for bad in (1 << 40, 1 << 63, 0x20, 0x4):           # bits 63:40 stay reserved; other low bits as before
            with self.assertRaises(ValueError):
                m.walk(tables, replace(context, cr3=context.cr3 | bad), 0x100000, 'read')
        tables, context, _ = m.fixture('legacy32')
        with self.assertRaises(ValueError):
            m.walk(tables, replace(context, cr3=context.cr3 | 1 << 32), 0x100000, 'read')


# The SDM's own tables, transcribed independently of the model's encoding.
TYPES = {'UC': m.UC, 'UC-': m.UC_MINUS, 'WC': m.WC, 'WT': m.WT, 'WB': m.WB, 'WP': m.WP}
TABLE_14_7 = [        # (MTRR type, PAT entry value, effective type), Vol.3 Table 14-7 rows in print order
    ('UC', 'UC', 'UC'), ('UC', 'UC-', 'UC'), ('UC', 'WC', 'WC'), ('UC', 'WT', 'UC'), ('UC', 'WB', 'UC'), ('UC', 'WP', 'UC'),
    ('WC', 'UC', 'UC'), ('WC', 'UC-', 'WC'), ('WC', 'WC', 'WC'), ('WC', 'WT', 'UC'), ('WC', 'WB', 'WC'), ('WC', 'WP', 'UC'),
    ('WT', 'UC', 'UC'), ('WT', 'UC-', 'UC'), ('WT', 'WC', 'WC'), ('WT', 'WT', 'WT'), ('WT', 'WB', 'WT'), ('WT', 'WP', 'WP'),
    ('WB', 'UC', 'UC'), ('WB', 'UC-', 'UC'), ('WB', 'WC', 'WC'), ('WB', 'WT', 'WT'), ('WB', 'WB', 'WB'), ('WB', 'WP', 'WP'),
    ('WP', 'UC', 'UC'), ('WP', 'UC-', 'WC'), ('WP', 'WC', 'WC'), ('WP', 'WT', 'WT'), ('WP', 'WB', 'WP'), ('WP', 'WP', 'WP')]
# Table 14-9: (register, first address, bytes per field)
FIXED = [(0x250, 0x00000, 0x10000), (0x258, 0x80000, 0x4000), (0x259, 0xa0000, 0x4000)] + [
    (0x268 + k, 0xc0000 + 0x8000 * k, 0x1000) for k in range(8)]


def mtrr_state(default=m.WB, fe=False, e=True, fixed=None, variable=()):
    """{MSR: value}: default type, E/FE, optional {register: value}, and variable (base, size, type[, valid]) ranges."""
    state = {0x2ff: default | fe << 10 | e << 11, **(fixed or {})}
    for n, (base, size, kind, *valid) in enumerate(variable):
        state[0x200 + 2 * n] = base | kind
        state[0x201 + 2 * n] = (~(size - 1) & 0xfffffff000) | (0x800 if not valid or valid[0] else 0)
    return state


class MemoryTypeTests(unittest.TestCase):
    def test_table_14_7_every_row(self):
        self.assertEqual(len(TABLE_14_7), 30)
        self.assertEqual(len({(a, b) for a, b, _ in TABLE_14_7}), 30)
        for mtrr, pat, effective in TABLE_14_7:
            self.assertEqual(m.effective_type(TYPES[mtrr], TYPES[pat]), TYPES[effective], (mtrr, pat))
        self.assertIsNone(m.effective_type(None, m.WB))

    def test_table_14_7_through_pat_msr_mtrr_and_route(self):
        for mtrr, pat, effective in TABLE_14_7:
            for index in range(8):
                value = m.PAT_RESET & ~(0xff << 8 * index) | TYPES[pat] << 8 * index
                route = replace(m.walk(*m.fixture('four')[:2], 0x100000, 'read').route, pat_index=index)
                state = mtrr_state(default=TYPES[mtrr])
                self.assertEqual(m.route_type(route, value, state), TYPES[effective], (mtrr, pat, index))

    def test_pat_msr_reset_value_and_validation(self):
        self.assertEqual(m.PAT_RESET, 0x0007040600070406)
        self.assertEqual([m.pat_type(m.PAT_RESET, i) for i in range(8)],
                         [m.WB, m.WT, m.UC_MINUS, m.UC, m.WB, m.WT, m.UC_MINUS, m.UC])
        for bad in (0x02, 0x03, 0x08, 0x80, 0xff):          # reserved encodings #GP at WRMSR
            with self.assertRaises(ValueError):
                m.pat_type(m.PAT_RESET & ~0xff | bad, 1)
        with self.assertRaises(ValueError):
            m.pat_type(m.PAT_RESET | 1 << 63, 0)               # reserved high bits of PA7
        with self.assertRaises(ValueError):
            m.pat_type(m.PAT_RESET & ~(0xff << 40) | 3 << 40, 0)    # reserved encoding in PA5

    def test_default_pat_indexes_with_firmware_mtrrs(self):
        # Default PAT, WB-default MTRR: PWT/PCD pick WB, WT, UC, UC for both PAT-bit values.
        route = m.walk(*m.fixture('four')[:2], 0x100000, 'read').route
        want = [m.WB, m.WT, m.UC, m.UC] * 2
        self.assertEqual([m.route_type(replace(route, pat_index=i), m.PAT_RESET, mtrr_state()) for i in range(8)], want)

    def test_mtrr_disabled_is_uc_even_with_fe_and_variable_ranges(self):
        for address in (0, 0x9ffff, 0x100000, 0x7fffffff, 0x200000000):
            for fe in (False, True):
                state = mtrr_state(e=False, fe=fe, fixed={0x250: 0x0606060606060606},
                                   variable=[(0, 1 << 40, m.WB)])
                self.assertEqual(m.mtrr_type(state, address), m.UC)
        self.assertEqual(m.mtrr_type({}, 0x1000), m.UC)           # reset state

    def test_default_type_for_unmatched_addresses(self):
        for kind in m.MTRR_TYPES:
            state = mtrr_state(default=kind, variable=[(0x40000000, 0x40000000, m.UC)])
            self.assertEqual(m.mtrr_type(state, 0x200000), kind)
            self.assertEqual(m.mtrr_type(state, 0x40000000 - 1), kind)
            self.assertEqual(m.mtrr_type(state, 0x100000000), kind)
            self.assertEqual(m.mtrr_type(state, 0x40000000), m.UC)

    def test_fixed_range_fields_follow_table_14_9(self):
        wb, wt = 0x0606060606060606, m.WT
        self.assertEqual(len(FIXED), 11)
        for register, start, size in FIXED:
            for field in range(8):
                state = mtrr_state(fe=True, fixed={r: wb for r, _, _ in FIXED} | {register: wb & ~(0xff << 8 * field) | wt << 8 * field})
                first = start + field * size
                for address in (first, first + 1, first + size - 1):
                    self.assertEqual(m.mtrr_type(state, address), wt, (hex(register), field, hex(address)))
                for address in (first - 1, first + size) if (first, first + size) != (0, 0) else ():
                    if 0 <= address < 0x100000:
                        self.assertEqual(m.mtrr_type(state, address), m.WB, (hex(register), field, hex(address)))
        # The ranges tile 0..1 MiB exactly.
        self.assertEqual(sum(size * 8 for _, _, size in FIXED), 0x100000)

    def test_fixed_ranges_take_priority_below_1mib_only_when_enabled(self):
        fixed = {0x250: 0x0101010101010101, 0x258: 0x0505050505050505, 0x259: 0, 0x268: 0x0404040404040404}
        for kinds, address in ((m.WC, 0x70000), (m.WP, 0x80000), (m.UC, 0xa0000), (m.WT, 0xc0000)):
            on = mtrr_state(fe=True, fixed=fixed, variable=[(0, 1 << 30, m.WB)])
            self.assertEqual(m.mtrr_type(on, address), kinds)
            off = mtrr_state(fixed=fixed, variable=[(0, 1 << 30, m.WB)])
            self.assertEqual(m.mtrr_type(off, address), m.WB)       # variable range still maps it
            self.assertEqual(m.mtrr_type(mtrr_state(fixed=fixed, default=m.WP), address), m.WP)
        self.assertEqual(m.mtrr_type(mtrr_state(fe=True, fixed=fixed, default=m.WP), 0x100000), m.WP)   # not fixed at 1 MiB
        self.assertEqual(m.mtrr_type(mtrr_state(fe=True, fixed=fixed, default=m.WP), 0xc7fff), m.WT)

    def test_variable_range_matching_valid_bit_size_and_base(self):
        state = mtrr_state(default=m.UC, variable=[(0x00200000, 0x00200000, m.WB), (0x10000000, 0x1000, m.WT),
                                                   (0x30000000, 0x10000000, m.WP, False)])
        for address, want in ((0x1fffff, m.UC), (0x200000, m.WB), (0x3fffff, m.WB), (0x400000, m.UC),
                              (0x0fffffff, m.UC), (0x10000000, m.WT), (0x10000fff, m.WT), (0x10001000, m.UC),
                              (0x30000000, m.UC)):              # last pair has V clear
            self.assertEqual(m.mtrr_type(state, address), want, hex(address))
        high = mtrr_state(default=m.WB, variable=[(0xff00000000, 1 << 32, m.UC)])    # bits up to MAXPHYADDR
        self.assertEqual((m.mtrr_type(high, 0xff00000000), m.mtrr_type(high, 0xffffffffff), m.mtrr_type(high, 0xfeffffffff)),
                         (m.UC, m.UC, m.WB))

    def test_variable_overlap_precedence(self):
        both = lambda a, b, default=m.WB: mtrr_state(default=default, variable=[(0, 1 << 30, a), (0, 1 << 20, b)])
        for a in m.MTRR_TYPES:
            self.assertEqual(m.mtrr_type(both(a, a), 0x1000), a)              # identical types
            self.assertEqual(m.mtrr_type(both(a, m.UC), 0x1000), m.UC)        # UC wins
            self.assertEqual(m.mtrr_type(both(m.UC, a), 0x1000), m.UC)
            self.assertEqual(m.mtrr_type(both(a, a), 0x2000000), a)           # only the larger range
        self.assertEqual(m.mtrr_type(both(m.WT, m.WB), 0x1000), m.WT)         # WT + WB = WT, either order
        self.assertEqual(m.mtrr_type(both(m.WB, m.WT), 0x1000), m.WT)
        for pair in ((m.WC, m.WT), (m.WC, m.WB), (m.WC, m.WP), (m.WP, m.WT), (m.WP, m.WB)):
            for a, b in (pair, pair[::-1]):
                self.assertIsNone(m.mtrr_type(both(a, b), 0x1000), pair)      # otherwise undefined
                self.assertEqual(m.mtrr_type(both(a, b), 0x2000000), a)       # but a single match is fine
        triple = lambda *kinds: mtrr_state(variable=[(0, 1 << 20, k) for k in kinds])
        self.assertEqual(m.mtrr_type(triple(m.WT, m.WB, m.WB), 0), m.WT)
        self.assertEqual(m.mtrr_type(triple(m.WB, m.WT, m.WT, m.WB), 0), m.WT)
        self.assertEqual(m.mtrr_type(triple(m.WT, m.WB, m.UC), 0), m.UC)
        self.assertIsNone(m.mtrr_type(triple(m.WT, m.WB, m.WC), 0))
        self.assertIsNone(m.mtrr_type(triple(m.WT, m.WB, m.WP), 0))
        self.assertIsNone(m.route_type(m.walk(*m.fixture('four')[:2], 0x100000, 'read').route, m.PAT_RESET, triple(m.WC, m.WB), address=0x1000))

    def test_variable_ranges_do_not_override_fixed_below_1mib(self):
        fixed = {0x250: 0x0606060606060606}
        state = mtrr_state(fe=True, fixed=fixed, variable=[(0, 1 << 20, m.UC)])
        self.assertEqual(m.mtrr_type(state, 0x10000), m.WB)       # fixed WB beats variable UC
        self.assertEqual(m.mtrr_type(mtrr_state(fixed=fixed, variable=[(0, 1 << 20, m.UC)]), 0x10000), m.UC)

    def test_invalid_mtrr_encodings_are_refused_not_guessed(self):
        for state in ({0x2ff: 0x802}, {0x2ff: 0x1800}, {0x2ff: 0xc06, 0x250: 2}, {0x2ff: 0xc06, 0x250: 0x02}):
            with self.assertRaises(ValueError):
                m.mtrr_type(state, 0)

    def test_profile_firmware_state_agrees_with_independent_reading(self):
        from models import cpu_profile
        profile = cpu_profile.load()
        for limit in (None, 0x80000000, 0x60000000):
            state = profile.mtrr_state(limit)
            top = 0x80000000 if limit is None else limit
            for address, want in ((0, m.WB), (0x9ffff, m.WB), (0xa0000, m.UC), (0xfffff, m.UC), (0x100000, m.WB),
                                  (top - 1, m.WB), (top, m.UC), (0xf0000000, m.UC), (0xfee00000, m.UC),
                                  (0xffffffff, m.UC), (0x100000000, m.WB), (0x1ffffffff, m.WB)):
                self.assertEqual(m.mtrr_type(state, address), want, (limit, hex(address)))
                self.assertEqual(m.mtrr_type(state, address), profile.memory_type(state, address))

    def test_route_type_defaults_to_the_route_gpa_and_reset_mtrrs(self):
        route = m.walk(*m.fixture('four')[:2], 0x100023, 'read').route
        self.assertEqual(m.route_type(route), m.UC)               # E clear: UC under PAT0 = WB
        state = mtrr_state(variable=[(0x100000, 0x1000, m.UC)])
        self.assertEqual(m.route_type(route, m.PAT_RESET, state), m.UC)
        self.assertEqual(m.route_type(route, m.PAT_RESET, state, address=0x300000), m.WB)


class PatIndexTests(unittest.TestCase):
    """Leaf PAT/PCD/PWT -> Route.pat_index at both leaf sizes, under every upper-level PWT/PCD pattern."""
    def setup(self, mode, size):
        """(tables, context, leaf address, upper-entry addresses, leaf base value)."""
        tables, context, layout = m.fixture(mode)
        # Non-leaf entries above the leaf: legacy PDE | PAE PDPTE (hidden) + PDE | four PML4E + PDPTE + PDE.
        above = {'legacy32': [], 'pae': ['pdpte'], 'four': [0x1000, 0x2000]}[mode]
        if size == 4096:
            leaf, value = layout['first'] + 256 * layout['entry_bytes'], 0x100007
            upper = above + [layout['parent']]
        else:
            leaf, value, upper = layout['parent'], 0x87, above
        return tables, context, leaf, upper, value

    def route(self, mode, size, leaf_bits, upper_bits):
        tables, context, leaf, upper, value = self.setup(mode, size)
        for level, at in enumerate(upper):
            bits = upper_bits >> 2 * level & 3
            if at == 'pdpte':
                tables[0x1020] |= bits << 3
                hidden, _ = m.load_hidden(tables, context.cr3, context.hidden)
                context = replace(context, hidden=hidden, generation=2)
            else:
                tables[at] |= bits << 3
        tables[leaf] = value | leaf_bits
        return m.walk(tables, context, 0x100023, 'read')

    @staticmethod
    def leaf_bits(pat, pcd, pwt, size):
        return pwt << 3 | pcd << 4 | pat << (7 if size == 4096 else 12)

    def test_index_from_leaf_alone_at_4k_and_2m_under_all_upper_patterns(self):
        for mode, size in (('legacy32', 4096), ('pae', 4096), ('four', 4096), ('pae', 1 << 21), ('four', 1 << 21)):
            levels = len(self.setup(mode, size)[3])
            for upper_bits in range(1 << 2 * levels):
                for index in range(8):
                    pat, pcd, pwt = index >> 2, index >> 1 & 1, index & 1
                    r = self.route(mode, size, self.leaf_bits(pat, pcd, pwt, size), upper_bits)
                    self.assertEqual((r.vector, r.route.pat_index, r.route.leaf_bytes, r.route.gpa),
                                     (0, index, size, 0x100023), (mode, size, upper_bits, index))

    def test_upper_level_counts(self):
        self.assertEqual([len(self.setup(*c)[3]) for c in (('legacy32', 4096), ('pae', 4096), ('four', 4096),
                                                           ('pae', 1 << 21), ('four', 1 << 21))], [1, 2, 3, 1, 2])

    def test_pat_bit_position_depends_on_leaf_size(self):
        for mode in ('pae', 'four'):
            # 4KiB: bit 12 is address, bit 7 is PAT.  2MiB: bit 7 is PS, bit 12 is PAT.
            r = self.route(mode, 4096, 1 << 7, 0)
            self.assertEqual((r.route.pat_index, r.route.gpa), (4, 0x100023))
            r = self.route(mode, 4096, 1 << 12, 0)
            self.assertEqual((r.route.pat_index, r.route.gpa), (0, 0x101023))
            r = self.route(mode, 1 << 21, 1 << 12, 0)
            self.assertEqual((r.route.pat_index, r.route.gpa), (4, 0x100023))
            r = self.route(mode, 1 << 21, 0, 0)
            self.assertEqual((r.route.pat_index, r.route.leaf_bytes), (0, 1 << 21))

    def test_no_route_is_refused_for_its_memory_type_and_data_is_unchanged(self):
        for mode in m.MODES:
            for bits in (0, 8, 16, 24, 0x80, 0x98):
                tables, context, layout = m.fixture(mode)
                leaf = layout['first'] + 256 * layout['entry_bytes']
                tables[leaf] |= bits
                for access in ('read', 'write'):
                    r = m.walk(tables, context, 0x100000, access)
                    self.assertEqual((r.vector, r.route.gpa, r.route.leaf_bytes), (0, 0x100000, 4096))

    def test_cached_route_keeps_its_index_until_invalidated(self):
        tables, context, layout = m.fixture('four')
        leaf = layout['first'] + 256 * 8
        first = m.walk(tables, context, 0x100000, 'read')
        cache = m.Cache(); cache.remember(context, first)
        changed = dict(first.tables); changed[leaf] |= 0x98                    # PAT=1, PCD=1, PWT=1
        permitted = m.access_outcomes(changed, context, 0x100000, 'read', cache)
        self.assertEqual([(r.policy, r.route.pat_index) for r in permitted], [('cached', 0), ('fresh', 7)])
        after = m.access_outcomes(changed, context, 0x100123, 'read', cache, invalidated=True)
        self.assertEqual([r.route.pat_index for r in after], [7])
        # A cached route keeps the index at another byte offset in the same page.
        self.assertEqual(m.access_outcomes(changed, context, 0x100123, 'read', cache, policy='cached')[0].route.pat_index, 0)

    def test_all_eight_pat_entries_are_independent(self):
        # A rewritten PAT makes all eight entries independent (the default repeats PA0-PA3 in PA4-PA7).
        route = m.walk(*m.fixture('four')[:2], 0x100000, 'read').route
        pat = 0x0705040100070601      # PA0..PA7 = WC, WB, UC-, UC, WC, WT, WP, UC-
        want = [m.WC, m.WB, m.UC_MINUS, m.UC, m.WC, m.WT, m.WP, m.UC_MINUS]
        self.assertEqual([m.pat_type(pat, i) for i in range(8)], want)
        state = mtrr_state(default=m.WB)
        self.assertEqual([m.route_type(replace(route, pat_index=i), pat, state) for i in range(8)],
                         [m.WC, m.WB, m.UC, m.UC, m.WC, m.WT, m.WP, m.UC])

if __name__ == '__main__':
    unittest.main()
