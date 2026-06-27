# Label Episode

Label the outcome of an episode to advance it through the skill pipeline.

Required from the user (or $ARGUMENTS):
- `episode_id` — UUID of the episode to label
- `outcome` — one of: RESOLVED, FAILED, ROLLED_BACK, HUMAN_OVERRIDE, INCONCLUSIVE
- `outcome_signal` — non-empty dict with signal metadata, e.g. `{"source": "sre", "confidence": 0.9}`

Call `registry__label_episode` with the provided values. Confirm the episode was updated by showing the returned episode record.

To find unlabeled episodes first, use `/episodes-list`.
