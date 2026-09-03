# Raw-data manifests

One file per immutable raw artifact, as required by the
[evidence policy](../evidence-policy.md). The artifact itself stays outside
Git under the external raw-data root; only the manifest is committed. This
directory is empty until research is authorized.

- Identifier: `MAN-<investigation-id>-<nn>`, for example `MAN-INV-01-03-02`.
- Filename: `<identifier>.md`.
- Required contents: artifact path relative to the raw-data root, SHA-256
  digest, producing experiment code and revision, method, environment,
  inputs, and the investigation and result it belongs to.
