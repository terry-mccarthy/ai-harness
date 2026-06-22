import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_WHEN_TO_USE_RE = re.compile(r"\*\*When to use:\*\*\s*(.+)", re.IGNORECASE)


def _extract_signature(text: str) -> str | None:
    match = _WHEN_TO_USE_RE.search(text)
    return match.group(1).strip() if match else None


async def seed_runbooks(runbook_dir: Path, store) -> int:
    """Ingest all *.md files from runbook_dir into the memory store.

    Each runbook's **When to use:** line is the embedded signature; the full
    body is stored as the value. Files missing the signature are skipped with
    a warning. Returns the count of runbooks successfully seeded.
    """
    seeded = 0
    for path in sorted(runbook_dir.glob("*.md")):
        slug = path.stem
        body = path.read_text(encoding="utf-8")
        signature = _extract_signature(body)
        if not signature:
            logger.warning("runbook_seed: skipping %s — missing **When to use:** line", slug)
            continue
        await store.write(
            namespace="runbooks",
            key=slug,
            value={"id": slug, "signature": signature, "body": body},
        )
        seeded += 1
        logger.info("runbook_seed: seeded %s", slug)
    return seeded
