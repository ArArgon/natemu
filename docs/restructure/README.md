# docs/restructure: design pages and historical records

Most pages in this folder are plans, contracts, handoffs and measurement notes written while natemu's execution path,
devices and test suite were being built (October and November 2026). They are kept as the record of what was decided,
run and measured at the time. Commands in them that run `tools/run_suite.py`, `tools/with-env` or a script under
`tools/` no longer run and do not describe how to test today. Newer design pages (such as `m2-32bit-design.md`) are
current; how to test is always [docs/testing.md](../testing.md).

- Tests run only through `tools/natemu-test` ([docs/testing.md](../testing.md)). The old suite (`tools/run_suite.py`), the
  `tools/with-env` launcher and `tools/vm_lock.py` are gone.
- The runner scripts these pages name as `tools/run_*.py` now live with the families that run them, under `tests/`
  ([tests/README.md](../../tests/README.md)), and refuse to start outside the framework.
- Paths under `natemu-raw/` name where a run's evidence was kept. The framework builds or downloads its own inputs and
  does not read them.

The current state lives in [docs/implementation.md](../implementation.md) and
[INV-03-01](../research/investigations/INV-03-01.md). The pages `docs/implementation.md` links as design descriptions
(such as `translated-code-layout.md` and `cross-vcpu-reuse-plan.md`) still describe that design; their commands are
historical like the rest.
