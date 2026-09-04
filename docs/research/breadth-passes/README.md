# Breadth-pass records

The auditable record of each breadth pass the
[executor guide](../executor-guide.md) requires: the pass before a question
is answered, and the end-of-track pass from product, architecture,
correctness, and methodology perspectives.

- Identifier: `BP-<track>-<nn>-start` for the pass before a question,
  `BP-<track>-end` for the end-of-track pass, per the
  [identifier table](../README.md#identifiers), for example `BP-01-03-start`
  and `BP-01-end`.
- Filename: `<identifier>.md`.
- Bootstrap exception: when the backlog is empty, the first start pass reserves
  its question identifier in the pass and creates that question and all other
  surfaced questions atomically. This avoids treating an unaudited placeholder
  as a selected question. All later start passes select an existing question.
- Every newly surfaced issue is also filed in the
  [question backlog](../question-backlog.md); a pass that surfaces none
  records the exact phrase `no new issues found`.
