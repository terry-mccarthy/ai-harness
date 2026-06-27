# Sync Skills

Sync active skills from the registry to `.claude/commands/` as generated slash commands.

Run:
```
make sync-skills
```

This calls `scripts/sync_skills.py`, which:
1. Fetches all active skills from the registry
2. Writes `.claude/commands/skill-<name>.md` for each active skill
3. Removes any stale `skill-*.md` files for skills that are no longer active
4. Prints a summary: `synced N skills, removed M stale commands`

Generated `skill-*.md` files are gitignored. Run this command after promoting, revoking, or creating skills to keep your local slash commands in sync with the live registry.
