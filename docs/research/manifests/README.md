# Raw-data manifests

One file per immutable raw artifact, as required by the
[evidence policy](../evidence-policy.md). The artifact itself stays outside
Git under the external raw-data root; only the manifest is committed.

- Identifier: `MAN-<track>-<nn>-<nn>`, the investigation number then a
  sequence, per the [identifier table](../README.md#identifiers), for example
  `MAN-01-03-02`.
- Filename: `<identifier>.md`.
- Required contents: artifact path relative to the raw-data root, SHA-256
  digest, producing experiment code and revision, method, environment,
  inputs, and the investigation and result it belongs to.
