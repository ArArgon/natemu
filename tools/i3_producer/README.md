# FEX export patch

`fex_export.py` patches the pinned compile-only tool (`CodeSizeValidation`) or validates its retained input/output.
The [I4-V producer](../i4v_producer/README.md) build applies it before its own adapter (`build.sh`, pinned in its
`producer.json`). The I3 allocated-map producer that once lived here is retired with the I3 cells
(docs/restructure/slice3-contract.md, S3a2).
