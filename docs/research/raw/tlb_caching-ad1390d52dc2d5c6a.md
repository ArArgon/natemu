I now have everything needed. Let me compile the final findings.

## Summary of findings — verbatim, all at Linux v6.12

All quotes below are copied verbatim from `raw.githubusercontent.com/torvalds/linux/v6.12/...` (a mirror of Linus's tree tagged v6.12, identical content to git.kernel.org — `git.kernel.org` and `elixir.bootlin.com` both blocked/rendered no source text for automated fetching in this session, so I used the GitHub mirror and confirmed line numbers directly). Elixir URLs are given for cross-reference; git.kernel.org returned an anti-bot "Anubis" block page and elixir.bootlin.com only returned a JS-shell page to the fetch tool, so I quote from the raw GitHub mirror, byte-for-byte identical to the tagged v6.12 source.

### 1. `arch/x86/include/asm/tlbflush.h`

Elixir: https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/tlbflush.h

`pte_flags_need_flush()`, lines 296–347:
`pte_needs_flush()`, lines 349–370, and `huge_pmd_needs_flush()`, lines 372–392:
All source has now been directly verified. Here is the complete research report.

---

## 1. `arch/x86/include/asm/tlbflush.h` (v6.12)

`file:///arch/x86/include/asm/tlbflush.h:296-347` — elixir: https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/tlbflush.h#L296

```c
static inline bool pte_flags_need_flush(unsigned long oldflags,
					unsigned long newflags,
					bool ignore_access)
{
	/*
	 * Flags that require a flush when cleared but not when they are set.
	 * Only include flags that would not trigger spurious page-faults.
	 * Non-present entries are not cached. Hardware would set the
	 * dirty/access bit if needed without a fault.
	 */
	const pteval_t flush_on_clear = _PAGE_DIRTY | _PAGE_PRESENT |
					_PAGE_ACCESSED;
	const pteval_t software_flags = _PAGE_SOFTW1 | _PAGE_SOFTW2 |
					_PAGE_SOFTW3 | _PAGE_SOFTW4 |
					_PAGE_SAVED_DIRTY;
	const pteval_t flush_on_change = _PAGE_RW | _PAGE_USER | _PAGE_PWT |
			  _PAGE_PCD | _PAGE_PSE | _PAGE_GLOBAL | _PAGE_PAT |
			  _PAGE_PAT_LARGE | _PAGE_PKEY_BIT0 | _PAGE_PKEY_BIT1 |
			  _PAGE_PKEY_BIT2 | _PAGE_PKEY_BIT3 | _PAGE_NX;
	unsigned long diff = oldflags ^ newflags;

	BUILD_BUG_ON(flush_on_clear & software_flags);
	BUILD_BUG_ON(flush_on_clear & flush_on_change);
	BUILD_BUG_ON(flush_on_change & software_flags);

	/* Ignore software flags */
	diff &= ~software_flags;

	if (ignore_access)
		diff &= ~_PAGE_ACCESSED;

	/*
	 * Did any of the 'flush_on_clear' flags was clleared set from between
	 * 'oldflags' and 'newflags'?
	 */
	if (diff & oldflags & flush_on_clear)
		return true;

	/* Flush on modified flags. */
	if (diff & flush_on_change)
		return true;

	/* Ensure there are no flags that were left behind */
	if (IS_ENABLED(CONFIG_DEBUG_VM) &&
	    (diff & ~(flush_on_clear | software_flags | flush_on_change))) {
		VM_WARN_ON_ONCE(1);
		return true;
	}

	return false;
}
```
(Note: `clleared` is a verbatim typo present in upstream source — kept as-is.)

`file:///arch/x86/include/asm/tlbflush.h:348-392` — elixir: https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/tlbflush.h#L348

```c
/*
 * pte_needs_flush() checks whether permissions were demoted and require a
 * flush. It should only be used for userspace PTEs.
 */
static inline bool pte_needs_flush(pte_t oldpte, pte_t newpte)
{
	/* !PRESENT -> * ; no need for flush */
	if (!(pte_flags(oldpte) & _PAGE_PRESENT))
		return false;

	/* PFN changed ; needs flush */
	if (pte_pfn(oldpte) != pte_pfn(newpte))
		return true;

	/*
	 * check PTE flags; ignore access-bit; see comment in
	 * ptep_clear_flush_young().
	 */
	return pte_flags_need_flush(pte_flags(oldpte), pte_flags(newpte),
				    true);
}
#define pte_needs_flush pte_needs_flush

/*
 * huge_pmd_needs_flush() checks whether permissions were demoted and require a
 * flush. It should only be used for userspace huge PMDs.
 */
static inline bool huge_pmd_needs_flush(pmd_t oldpmd, pmd_t newpmd)
{
	/* !PRESENT -> * ; no need for flush */
	if (!(pmd_flags(oldpmd) & _PAGE_PRESENT))
		return false;

	/* PFN changed ; needs flush */
	if (pmd_pfn(oldpmd) != pmd_pfn(newpmd))
		return true;

	/*
	 * check PMD flags; do not ignore access-bit; see
	 * pmdp_clear_flush_young().
	 */
	return pte_flags_need_flush(pmd_flags(oldpmd), pmd_flags(newpmd),
				    false);
}
#define huge_pmd_needs_flush huge_pmd_needs_flush
```

**Important correction to the expectation in the task**: there is **no** comment string "If the PTE was not present, there is no need to flush" anywhere in the kernel tree that I could find. The actual, exact wording — unchanged since it was introduced — is the terse:
```c
	/* !PRESENT -> * ; no need for flush */
```
This appears twice, identically, in both `pte_needs_flush()` and `huge_pmd_needs_flush()`. I checked this against the original introducing commit (below) and it is byte-identical to what was merged in 2022 — the phrasing has never been the fuller English sentence. I state this explicitly rather than paraphrase it as a match.

The reasoning about *why* not-present entries don't need a flush is instead carried in the `pte_flags_need_flush()` header comment quoted above: *"Non-present entries are not cached. Hardware would set the dirty/access bit if needed without a fault."*

**Transitions that DO require a flush** (from the code above):
- `flush_on_clear = _PAGE_DIRTY | _PAGE_PRESENT | _PAGE_ACCESSED` — flush if any of these were **cleared** (present→not-present, dirty→clean, accessed→not-accessed).
- `flush_on_change = _PAGE_RW | _PAGE_USER | _PAGE_PWT | _PAGE_PCD | _PAGE_PSE | _PAGE_GLOBAL | _PAGE_PAT | _PAGE_PAT_LARGE | _PAGE_PKEY_BIT0..3 | _PAGE_NX` — flush on **any** change (set or clear) of these.
- PFN change — flush always.
- `_PAGE_SOFTW1..4`, `_PAGE_SAVED_DIRTY` (software-only bits) — never flush.
- `_PAGE_ACCESSED` is ignored (`ignore_access=true`) in `pte_needs_flush()` for PTEs, but **not** ignored for huge PMDs in `huge_pmd_needs_flush()`.

**Transition that does NOT require a flush**: not-present → present is short-circuited to `return false` immediately, before any flag comparison — it never even reaches `pte_flags_need_flush()`.

## 2. `arch/x86/include/asm/pgtable.h` and generic `include/linux/pgtable.h`

`arch/x86/include/asm/pgtable.h:1361` — elixir: https://elixir.bootlin.com/linux/v6.12/source/arch/x86/include/asm/pgtable.h#L1361
```c
#define flush_tlb_fix_spurious_fault(vma, address, ptep) do { } while (0)
```
On x86 this is a **no-op**. This overrides the generic default.

Generic default, `include/linux/pgtable.h:1187-1188` — elixir: https://elixir.bootlin.com/linux/v6.12/source/include/linux/pgtable.h#L1187
```c
#ifndef flush_tlb_fix_spurious_fault
#define flush_tlb_fix_spurious_fault(vma, address, ptep) flush_tlb_page(vma, address)
#endif
```
There is no comment attached to either definition in v6.12 (checked both files directly; none present).

`ptep_set_access_flags` declaration, `arch/x86/include/asm/pgtable.h:1295-1306`:
```c
/*
 * We only update the dirty/accessed state if we set
 * the dirty bit by hand in the kernel, since the hardware
 * will do the accessed bit for us, and we don't want to
 * race with other CPU's that might be updating the dirty
 * bit at the same time.
 */
struct vm_area_struct;

#define  __HAVE_ARCH_PTEP_SET_ACCESS_FLAGS
extern int ptep_set_access_flags(struct vm_area_struct *vma,
				 unsigned long address, pte_t *ptep,
				 pte_t entry, int dirty);
```

Implementation, `arch/x86/mm/pgtable.c:495-511` — elixir: https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/pgtable.c#L495
```c
/*
 * Used to set accessed or dirty bits in the page table entries
 * on other architectures. On x86, the accessed and dirty bits
 * are tracked by hardware. However, do_wp_page calls this function
 * to also make the pte writeable at the same time the dirty bit is
 * set. In that case we do actually need to write the PTE.
 */
int ptep_set_access_flags(struct vm_area_struct *vma,
			  unsigned long address, pte_t *ptep,
			  pte_t entry, int dirty)
{
	int changed = !pte_same(*ptep, entry);

	if (changed && dirty)
		set_pte(ptep, entry);

	return changed;
}
```
Note: `ptep_set_access_flags` itself performs **no TLB flush at all** — it just conditionally writes the PTE.

`pmdp_set_access_flags`, `arch/x86/mm/pgtable.c:515-533` (identical pattern for `pudp_set_access_flags` follows, lines 536-553), containing the explicit spurious-fault justification:
```c
int pmdp_set_access_flags(struct vm_area_struct *vma,
			  unsigned long address, pmd_t *pmdp,
			  pmd_t entry, int dirty)
{
	int changed = !pmd_same(*pmdp, entry);

	VM_BUG_ON(address & ~HPAGE_PMD_MASK);

	if (changed && dirty) {
		set_pmd(pmdp, entry);
		/*
		 * We had a write-protection fault here and changed the pmd
		 * to to more permissive. No need to flush the TLB for that,
		 * #PF is architecturally guaranteed to do that and in the
		 * worst-case we'll generate a spurious fault.
		 */
	}

	return changed;
}
```
(`to to` is a verbatim upstream typo, kept as-is.) The `pudp_set_access_flags` body at lines 536-553 has the identical comment verbatim.

`set_pte_at` on x86: x86 does **not** define its own `set_pte_at`; it inherits the generic macro from `include/linux/pgtable.h:280`:
```c
#define set_pte_at(mm, addr, ptep, pte) set_ptes(mm, addr, ptep, pte, 1)
```
which calls `set_ptes()` (generic, `include/linux/pgtable.h:264-278`) — a plain page-table write with no flush call anywhere in its body.

Generic `ptep_set_access_flags` guard, `include/linux/pgtable.h:282-286`:
```c
#ifndef __HAVE_ARCH_PTEP_SET_ACCESS_FLAGS
extern int ptep_set_access_flags(struct vm_area_struct *vma,
				 unsigned long address, pte_t *ptep,
				 pte_t entry, int dirty);
#endif
```
I found **no additional prose comment** here in v6.12 beyond the `#ifndef` guard — I looked and there is none to quote; I am not fabricating one.

## 3. `arch/x86/mm/fault.c` — `spurious_kernel_fault()`, full comment block

`arch/x86/mm/fault.c:951-1048` — elixir: https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/fault.c#L951

```c
static int spurious_kernel_fault_check(unsigned long error_code, pte_t *pte)
{
	if ((error_code & X86_PF_WRITE) && !pte_write(*pte))
		return 0;

	if ((error_code & X86_PF_INSTR) && !pte_exec(*pte))
		return 0;

	return 1;
}

/*
 * Handle a spurious fault caused by a stale TLB entry.
 *
 * This allows us to lazily refresh the TLB when increasing the
 * permissions of a kernel page (RO -> RW or NX -> X).  Doing it
 * eagerly is very expensive since that implies doing a full
 * cross-processor TLB flush, even if no stale TLB entries exist
 * on other processors.
 *
 * Spurious faults may only occur if the TLB contains an entry with
 * fewer permission than the page table entry.  Non-present (P = 0)
 * and reserved bit (R = 1) faults are never spurious.
 *
 * There are no security implications to leaving a stale TLB when
 * increasing the permissions on a page.
 *
 * Returns non-zero if a spurious fault was handled, zero otherwise.
 *
 * See Intel Developer's Manual Vol 3 Section 4.10.4.3, bullet 3
 * (Optional Invalidation).
 */
static noinline int
spurious_kernel_fault(unsigned long error_code, unsigned long address)
{
	pgd_t *pgd;
	p4d_t *p4d;
	pud_t *pud;
	pmd_t *pmd;
	pte_t *pte;
	int ret;

	/*
	 * Only writes to RO or instruction fetches from NX may cause
	 * spurious faults.
	 *
	 * These could be from user or supervisor accesses but the TLB
	 * is only lazily flushed after a kernel mapping protection
	 * change, so user accesses are not expected to cause spurious
	 * faults.
	 */
	if (error_code != (X86_PF_WRITE | X86_PF_PROT) &&
	    error_code != (X86_PF_INSTR | X86_PF_PROT))
		return 0;

	pgd = init_mm.pgd + pgd_index(address);
	if (!pgd_present(*pgd))
		return 0;

	p4d = p4d_offset(pgd, address);
	if (!p4d_present(*p4d))
		return 0;

	if (p4d_leaf(*p4d))
		return spurious_kernel_fault_check(error_code, (pte_t *) p4d);

	pud = pud_offset(p4d, address);
	if (!pud_present(*pud))
		return 0;

	if (pud_leaf(*pud))
		return spurious_kernel_fault_check(error_code, (pte_t *) pud);

	pmd = pmd_offset(pud, address);
	if (!pmd_present(*pmd))
		return 0;

	if (pmd_leaf(*pmd))
		return spurious_kernel_fault_check(error_code, (pte_t *) pmd);

	pte = pte_offset_kernel(pmd, address);
	if (!pte_present(*pte))
		return 0;

	ret = spurious_kernel_fault_check(error_code, pte);
	if (!ret)
		return 0;

	/*
	 * Make sure we have permissions in PMD.
	 * If not, then there's a bug in the page tables:
	 */
	ret = spurious_kernel_fault_check(error_code, (pte_t *) pmd);
	WARN_ONCE(!ret, "PMD has incorrect permission bits\n");

	return ret;
}
NOKPROBE_SYMBOL(spurious_kernel_fault);
```

Key point confirmed exactly as the task expected: the entire mechanism is scoped to **permission upgrades on already-present kernel pages** — "RO -> RW or NX -> X" — and the comment explicitly states *"Non-present (P = 0) and reserved bit (R = 1) faults are never spurious."* This is entirely orthogonal to not-present→present installs, and it's a **kernel-mapping-only** mechanism (`init_mm.pgd`), unrelated to user PTEs / `pte_needs_flush()`.

## 4. `arch/x86/mm/tlb.c`

`flush_tlb_mm_range()`, `arch/x86/mm/tlb.c:995-1032` — elixir: https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L995. Characterization: it bumps `mm->context.tlb_gen` via `inc_mm_tlb_gen()`, decides full-vs-range flush based on `tlb_single_page_flush_ceiling`, and either IPIs other CPUs via `flush_tlb_multi()` or calls `flush_tlb_func()` locally.

`flush_tlb_func()`, `arch/x86/mm/tlb.c:731-737` header comment, function body 737-885 — elixir: https://elixir.bootlin.com/linux/v6.12/source/arch/x86/mm/tlb.c#L737. It compares three generation counters — `local_tlb_gen`, `mm_tlb_gen`, `f->new_tlb_gen` — verbatim:
```c
/*
 * flush_tlb_func()'s memory ordering requirement is that any
 * TLB fills that happen after we flush the TLB are ordered after we
 * read active_mm's tlb_gen.  We don't need any explicit barriers
 * because all x86 flush operations are serializing and the
 * atomic64_read operation won't be reordered by the compiler.
 */
static void flush_tlb_func(void *info)
{
	/*
	 * We have three different tlb_gen values in here.  They are:
	 *
	 * - mm_tlb_gen:     the latest generation.
	 * - local_tlb_gen:  the generation that this CPU has already caught
	 *                   up to.
	 * - f->new_tlb_gen: the generation that the requester of the flush
	 *                   wants us to catch up to.
	 */
```
It elides work when `local_tlb_gen == mm_tlb_gen` ("There's nothing to do: we're already up to date...") or when `f->new_tlb_gen <= local_tlb_gen` ("The TLB is already up to date in respect to f->new_tlb_gen..."). **I searched the entire file** (`grep -n "spurious\|not present"`) and found **no occurrence of the words "spurious" or "not present"** anywhere in `arch/x86/mm/tlb.c` — I state this explicitly rather than inventing a quote. The tlb_gen elision machinery is orthogonal to the `pte_needs_flush()` decision (that decision happens upstream, in `mprotect.c`/`huge_memory.c`, before `flush_tlb_mm_range`/`tlb_flush_pte_range` is even called).

## 5. `mm/memory.c` — demand-fault paths

`do_anonymous_page()`, `mm/memory.c:4735-4847`, the install-and-cache-update tail at lines 4832-4838 — elixir: https://elixir.bootlin.com/linux/v6.12/source/mm/memory.c#L4832:
```c
setpte:
	if (vmf_orig_pte_uffd_wp(vmf))
		entry = pte_mkuffd_wp(entry);
	set_ptes(vma->vm_mm, addr, vmf->pte, entry, nr_pages);

	/* No need to invalidate - it was non-present before */
	update_mmu_cache_range(vmf, vma, addr, vmf->pte, nr_pages);
```
No flush call — confirmed.

`finish_fault()` (`mm/memory.c:5071-5152`) calls `set_pte_range()` (`mm/memory.c:5014-5045`), whose tail, lines 5042-5045, reads:
```c
	set_ptes(vma->vm_mm, addr, vmf->pte, entry, nr);

	/* no need to invalidate: a not-present page won't be cached */
	update_mmu_cache_range(vmf, vma, addr, vmf->pte, nr);
```
Again no flush call.

A third, near-identical instance exists at `mm/memory.c:4605`, inside `do_swap_page()`'s install path:
```c
	/* No need to invalidate - it was non-present before */
	update_mmu_cache_range(vmf, vma, address, ptep, nr_pages);
```
And a fourth variant at `mm/memory.c:757-761`, inside `restore_exclusive_pte()`, phrased slightly differently to flag the multi-CPU caveat:
```c
	set_pte_at(vma->vm_mm, address, ptep, pte);

	/*
	 * No need to invalidate - it was non-present before. However
	 * secondary CPUs may have mappings that need invalidating.
	 */
	update_mmu_cache(vma, address, ptep);
```
So there are (at least) two distinct exact wordings used across the file for this idea — "No need to invalidate - it was non-present before[.]" (three sites: lines 759, 4605, 4837) and "no need to invalidate: a not-present page won't be cached" (line 5044, in `set_pte_range()`).

`wp_page_reuse()`, `mm/memory.c:3229-3262` — elixir: https://elixir.bootlin.com/linux/v6.12/source/mm/memory.c#L3229:
```c
/*
 * Handle write page faults for pages that can be reused in the current vma
 *
 * This can happen either due to the mapping being with the VM_SHARED flag,
 * or due to us being the last reference standing to the page. In either
 * case, all we need to do here is to mark the page as writable and update
 * any related book-keeping.
 */
static inline void wp_page_reuse(struct vm_fault *vmf, struct folio *folio)
	__releases(vmf->ptl)
{
	...
	flush_cache_page(vma, vmf->address, pte_pfn(vmf->orig_pte));
	entry = pte_mkyoung(vmf->orig_pte);
	entry = maybe_mkwrite(pte_mkdirty(entry), vma);
	if (ptep_set_access_flags(vma, vmf->address, vmf->pte, entry, 1))
		update_mmu_cache_range(vmf, vma, vmf->address, vmf->pte, 1);
	pte_unmap_unlock(vmf->pte, vmf->ptl);
	count_vm_event(PGREUSE);
}
```
Called from `do_wp_page()` (`mm/memory.c:3655`, e.g. line 3731). This confirms the RO→RW write-fault-reuse path uses `ptep_set_access_flags()` + `update_mmu_cache_range()` — never an explicit `flush_tlb_*()` call — relying instead on `spurious_kernel_fault`/`#PF`-architectural-guarantee semantics described in fault.c (for kernel) and simply on hardware re-walk for user faults.

## 6. Nadav Amit's introducing commit

Commit `c9fe66560bf2dc7d109754414e309888cb8c9ba9`, "mm/mprotect: do not flush when not required architecturally", authored by Nadav Amit <namit@vmware.com>, dated 2022-05-09, merged for v6.0. (git.kernel.org and lore.kernel.org were unreachable/blocked in this session; I retrieved the patch from GitHub's mirror at `https://github.com/torvalds/linux/commit/c9fe66560bf2.patch`, which reproduces the exact commit metadata/hash and diff.) Full commit message, verbatim:

```
mm/mprotect: do not flush when not required architecturally

Currently, using mprotect() to unprotect a memory region or uffd to
unprotect a memory region causes a TLB flush.  However, in such cases the
PTE is often not modified (i.e., remain RO) and therefore not TLB flush is
needed.

Add an arch-specific pte_needs_flush() which tells whether a TLB flush is
needed based on the old PTE and the new one.  Implement an x86
pte_needs_flush().

Always flush the TLB when it is architecturally needed even when skipping
a TLB flush might only result in a spurious page-faults by skipping the
flush.

Even with such conservative manner, we can in the future further refine
the checks to test whether a PTE is present by only considering the
architectural _PAGE_PRESENT flag instead of {pte|pmd}_preesnt().  For not
be careful and use the latter.

Link: https://lkml.kernel.org/r/20220401180821.1986781-3-namit@vmware.com
Signed-off-by: Nadav Amit <namit@vmware.com>
```
(Typos "not TLB flush", "{pte|pmd}_preesnt()", "For not be careful" are verbatim upstream typos.)

The diff shows the *original* `pte_needs_flush()`/`huge_pmd_needs_flush()` bodies used the exact same `/* !PRESENT -> * ; no need for flush */` comment that survives unchanged into v6.12 — confirming there was never a fuller-English variant of that comment at any point in this code's history. The original `pte_flags_need_flush()` in the patch also already carried the "Non-present entries are not cached. Hardware would set the dirty/access bit if needed without a fault." line, word for word as in v6.12 (v6.12 only added `_PAGE_SAVED_DIRTY` to `software_flags` since then — checked via diff).

This same 2022 patch also added the generic (architecture-agnostic) conservative fallback that is still present verbatim in v6.12, `include/asm-generic/tlb.h:737-749`:
```c
#ifndef pte_needs_flush
static inline bool pte_needs_flush(pte_t oldpte, pte_t newpte)
{
	return true;
}
#endif

#ifndef huge_pmd_needs_flush
static inline bool huge_pmd_needs_flush(pmd_t oldpmd, pmd_t newpmd)
{
	return true;
}
#endif
```
i.e., any architecture that hasn't opted in with its own `pte_needs_flush()` always flushes.

Call site in `mm/mprotect.c:196-197` (v6.12) — elixir: https://elixir.bootlin.com/linux/v6.12/source/mm/mprotect.c#L196:
```c
			ptep_modify_prot_commit(vma, addr, pte, oldpte, ptent);
			if (pte_needs_flush(oldpte, ptent))
				tlb_flush_pte_range(tlb, addr, PAGE_SIZE);
```

## 7. `include/linux/pgtable.h` — generic comments near access-flags/spurious-fault hooks

I looked directly at both relevant spots in v6.12 and must report explicitly: **there is no prose comment** attached to either the `#ifndef __HAVE_ARCH_PTEP_SET_ACCESS_FLAGS` guard (`include/linux/pgtable.h:282-286`) or the `#ifndef flush_tlb_fix_spurious_fault` guard (`include/linux/pgtable.h:1187-1188`) in this kernel version. I did not find text resembling "we don't need to flush" or an architecture-spurious-fault discussion at those specific sites — only the bare `#ifndef`/`#define`/`#endif` guards shown above. I am flagging this rather than fabricating a quote to match the expected content. (The substantive commentary the task is likely thinking of lives instead in `arch/x86/mm/pgtable.c`'s `ptep_set_access_flags`/`pmdp_set_access_flags` comments and `arch/x86/mm/fault.c`'s `spurious_kernel_fault` block, both quoted in full above.)

---

### Bottom-line synthesis

- Not-present → present (the demand-fault install case in `do_anonymous_page`, `do_swap_page`, `finish_fault`/`set_pte_range`) never triggers a flush: `pte_needs_flush()`/`huge_pmd_needs_flush()` both short-circuit `return false` on `!(_PAGE_PRESENT)` in the old PTE with the comment `/* !PRESENT -> * ; no need for flush */`, and independently the `mm/memory.c` install sites carry their own comments ("No need to invalidate - it was non-present before" / "no need to invalidate: a not-present page won't be cached") right next to `update_mmu_cache*()` calls with no accompanying `flush_tlb_*()`.
- Permission **upgrades on already-present** entries (RO→RW, NX→X) also skip an eager flush, but through a *different* mechanism — `#PF`/`spurious_kernel_fault()` for the kernel mapping case, and `ptep_set_access_flags()` + `update_mmu_cache_range()` for user PTEs in `wp_page_reuse()` — justified by comments stating "#PF is architecturally guaranteed to do that and in the worst-case we'll generate a spurious fault," which is an entirely separate rationale (and separate code path) from the not-present→present rationale (which is that non-present entries simply aren't cached in the TLB at all).
- Permission **downgrades/clears** (`_PAGE_PRESENT`, `_PAGE_DIRTY`, `_PAGE_ACCESSED` cleared) and any change to `_PAGE_RW`, `_PAGE_USER`, `_PAGE_NX`, PKEY bits, `_PAGE_GLOBAL`, `_PAGE_PAT[_LARGE]`, `_PAGE_PWT`/`_PAGE_PCD`, `_PAGE_PSE`, or the PFN always require a flush per `pte_flags_need_flush()`.