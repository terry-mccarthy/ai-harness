import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def seed_logs(log_dir: Path, store) -> int:
    """Ingest all *.jsonl files from log_dir into the memory store.

    Each line is a JSON log entry with at minimum a "message" field.
    The message is the embedded text; the full entry is stored as value.
    Returns the count of entries successfully seeded.
    """
    seeded = 0
    for path in sorted(log_dir.glob("*.jsonl")):
        incident = path.stem
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("log_seed: skipping malformed line in %s:%d", path.name, idx)
                continue
            if not entry.get("message"):
                logger.warning("log_seed: skipping entry without message in %s:%d", path.name, idx)
                continue
            key = f"log:{incident}:{idx}"
            await store.write(
                namespace="logs",
                key=key,
                value={**entry, "id": key},
            )
            seeded += 1
        logger.info("log_seed: seeded %s", path.name)
    return seeded
