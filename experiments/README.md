# Experiment code

Reviewed code for separately authorized experiments, one directory per
investigation. This directory is empty until research is authorized; nothing
here is inherited from commit `a121702`, and nothing here is implementation.

```text
experiments/
  <investigation-id>/
    README.md       what it measures, method reference, manifest links
    src/            the code the code gate reviews
    prototype/      only when the experiment measures prototype code
    replication/    the independent second run's code, when one exists
```

- Directory name: the investigation identifier, for example `INV-01-03`.
- The code gate reviews `src/` at a recorded Git revision; manifests cite
  that revision.
- Runs write raw data only under the external raw-data root named by
  `NATEMU_RAW_ROOT`, never under this directory.
- Prototype code exists to test a mechanism and is discarded afterward; it
  never becomes the emulator.
