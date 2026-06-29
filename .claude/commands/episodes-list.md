# List Episodes

List recent episodes from the registry.

Call `registry__list_episodes` with:
- `limit` — number of episodes to return (default 20)
- `unlabeled_only` — set to true to show only episodes awaiting a label

Display results as a table showing `episode_id`, `agent_principal`, `outcome` (or "unlabeled"), and `outcome_labeled_at`.

To label an episode, use `/skill-label`.
