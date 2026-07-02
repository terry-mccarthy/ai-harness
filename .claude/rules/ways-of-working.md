These rules apply to every phase of this project. Follow them without being asked.

**1. Update docs when tests go green.**
Before declaring a phase done, update:
- `README.md` — stack section, test count, config table, project layout
- `CLAUDE.md` — any new gotchas, changed startup commands, updated flow description
- `ARCHITECTURE.md` — the current architecture and ADRs.

**2. Document gotchas immediately, not at end-of-phase.**
If something takes more than one attempt to get right — a library quirk, an API difference from docs, a config flag that was removed, a subtle ordering issue — add it to the relevant section of `CLAUDE.md` *before* moving on. Future sessions start cold; anything not written here will be re-discovered the hard way.

**3. Note divergences from the spec explicitly.**
Record deliberate skips, pragmatic simplifications, and upstream differences in `PROGRESS.md` under that phase's Notes section. Don't silently drift.

**4. Red before green.**
Write the test file first. Run it, confirm it fails for the right reason, then implement. A test that was never red proves nothing.

**5. Code health >= 9.**
Maintain code-health score at or above 9 at all times. Run /forensics before any commit.
