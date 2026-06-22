# Architecture Decision Records

The **canonical ADR index is the Decision Log table** in
[`../../ARCHITECTURE.md`](../../ARCHITECTURE.md#decision-log). Every decision is a one-line
row there, numbered in a single sequence (0001, 0002, …).

This directory holds **optional long-form files** for the subset of ADRs that need more than a
line — full context, alternatives considered, and consequences. A file shares the **same number**
as its Decision Log row and the row links to it.

## Convention

- **Single number space.** A `docs/adr/NNNN-*.md` file and Decision Log row `NNNN` are the *same*
  ADR. Never create a file whose number already names a different row — that is a collision, not a
  new ADR.
- **Table first.** New decisions are added as a Decision Log row. Promote a decision to a file here
  only when a single line cannot carry the rationale.
- **Linking.** When a file exists, its Decision Log row links to it (see row 0036). The file's
  `Status:` line should match the table and name any superseding ADRs.
- **Supersession.** Mark superseded decisions in *both* places: update the table `Status` column and
  the file's `Status:` line. Reference superseding ADRs by number.

## Files

| ADR  | File                              | Notes |
|------|-----------------------------------|-------|
| 0036 | `0036-architect-mcp-server.md`    | Superseded in part by 0038, 0039 |

All other decisions (0001–0035, 0037–0039) are table-only in the Decision Log.

## Tooling note

The `review_server__architecture_review` tool reads invariants from **both** `ARCHITECTURE.md`
(including this Decision Log table) and every file in `docs/adr/`. Because the table is part of
`ARCHITECTURE.md`, all decisions are visible to the tool whether or not they have a file here.
