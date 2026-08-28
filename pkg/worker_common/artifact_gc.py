"""Reachability-based garbage collection for content-addressed ArtifactStore.

Scans Redis for live sha256:<hex> refs, compares against the filesystem artifact
store, and deletes orphaned artifacts older than a minimum age.

Usage:
    python -m pkg.worker_common.artifact_gc --dry-run --min-age 24h
    python -m pkg.worker_common.artifact_gc --min-age 24h  # actually delete

Environment:
    ARTIFACT_PATH — artifact store root (default: /app/data/artifacts)
    REDIS_URL     — Redis connection URL
"""

import argparse
import logging
import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from pkg.worker_common.artifact_store import FSStore, get_store

logger = logging.getLogger(__name__)

# Pattern to extract sha256:<64hex> from any Redis value
_ARTIFACT_REF_RE = re.compile(r"sha256:([0-9a-f]{64})")

# Default keys to scan for live refs (artifact store refs)
_SCANNABLE_KEY_PATTERNS = [
    "orchestrator:job:*:text",
    "orchestrator:job:*:chunks",
    "orchestrator:job:*:embeddings",
    "orchestrator:job:*:inference_embeddings",
    "orchestrator:job:*:results",
]

# How old an orphan must be before deletion (prevents race with newly-written job)
DEFAULT_MIN_AGE_SECONDS = 86400  # 24h


def _parse_min_age(value: str) -> int:
    """Parse human duration like '24h', '7d', '3600' into seconds."""
    value = value.strip().lower()
    if value.isdigit():
        return int(value)
    m = re.match(r"^(\d+)([smhd])$", value)
    if not m:
        raise ValueError(f"Invalid min-age: {value} (use 24h, 7d, 3600)")
    num, unit = m.groups()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(num) * multipliers[unit]


def collect_live_refs(redis_client, key_patterns: Optional[List[str]] = None) -> Set[str]:
    """Collect all sha256:<hex> artifact refs currently reachable from Redis.

    Scans all keys matching the scannable patterns and extracts refs from
    their values. Uses SCAN for memory efficiency on large Redis.

    Returns:
        Set of hex digests (without prefix) that are still live.
    """
    if key_patterns is None:
        key_patterns = _SCANNABLE_KEY_PATTERNS

    live: Set[str] = set()
    for pattern in key_patterns:
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor, match=pattern, count=500)
            if keys:
                # Decode bytes keys if needed
                str_keys = [
                    k.decode() if isinstance(k, bytes) else k for k in keys
                ]
                # Pipeline GET for all keys in this batch
                pipe = redis_client.pipeline()
                for k in str_keys:
                    pipe.get(k)
                values = pipe.execute()
                for v in values:
                    if v is None:
                        continue
                    text = v.decode() if isinstance(v, bytes) else str(v)
                    for m in _ARTIFACT_REF_RE.finditer(text):
                        live.add(m.group(1))
            if cursor == 0:
                break
    return live


def scan_store(store: FSStore) -> List[Tuple[str, str, float, int]]:
    """Scan the artifact store filesystem.

    Returns:
        List of (digest, path, mtime, size_bytes) for every artifact file.
    """
    artifacts: List[Tuple[str, str, float, int]] = []
    root = store.root
    if not os.path.isdir(root):
        return artifacts
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if not fname.endswith(".bin"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                st = os.stat(fpath)
                digest = fname[: -len(".bin")]
                # Validate hex digest
                if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                    artifacts.append((digest, fpath, st.st_mtime, st.st_size))
            except OSError as e:
                logger.warning("Stat failed for %s: %s", fpath, e)
    return artifacts


def gc(
    store: FSStore,
    redis_client,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    dry_run: bool = True,
    key_patterns: Optional[List[str]] = None,
) -> Dict:
    """Run reachability-based artifact GC.

    Args:
        store: The artifact store to clean.
        redis_client: Redis client.
        min_age_seconds: Orphans younger than this are spared.
        dry_run: If True, only report (no deletes).
        key_patterns: Optional Redis key patterns to scan for live refs.

    Returns:
        Dict with keys: scanned, live, orphan_total, orphan_eligible,
        deleted, bytes_reclaimed, errors.
    """
    now = time.time()
    cutoff = now - min_age_seconds

    live = collect_live_refs(redis_client, key_patterns)
    all_artifacts = scan_store(store)

    scanned = len(all_artifacts)
    orphan_total = 0
    orphan_eligible: List[Tuple[str, str, float, int]] = []
    for digest, path, mtime, size in all_artifacts:
        if digest not in live:
            orphan_total += 1
            if mtime < cutoff:
                orphan_eligible.append((digest, path, mtime, size))

    deleted = 0
    bytes_reclaimed = 0
    errors = 0

    if not dry_run:
        for digest, path, mtime, size in orphan_eligible:
            try:
                os.remove(path)
                deleted += 1
                bytes_reclaimed += size
                logger.info("GC deleted orphan %s (age %.0fh, %d bytes)",
                            digest, (now - mtime) / 3600, size)
            except OSError as e:
                logger.warning("GC delete failed for %s: %s", path, e)
                errors += 1

    result = {
        "scanned": scanned,
        "live": len(live),
        "orphan_total": orphan_total,
        "orphan_eligible": len(orphan_eligible),
        "deleted": deleted,
        "bytes_reclaimed": bytes_reclaimed,
        "errors": errors,
        "dry_run": dry_run,
    }
    logger.info(
        "GC: scanned=%d live=%d orphans=%d eligible=%d deleted=%d bytes=%d dry_run=%s",
        scanned, len(live), orphan_total, len(orphan_eligible),
        deleted, bytes_reclaimed, dry_run,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="Artifact Store GC")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only report, don't delete")
    parser.add_argument("--min-age", default="24h",
                        help="Minimum orphan age before deletion (e.g. 24h, 7d, 3600)")
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://redis:6379/6379"),
                        help="Redis URL")
    parser.add_argument("--artifact-path", default=None,
                        help="Artifact store path (default: ARTIFACT_PATH env or /app/data/artifacts)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    min_age = _parse_min_age(args.min_age)

    import redis as redis_lib
    r = redis_lib.from_url(args.redis_url, decode_responses=False)
    if args.artifact_path:
        store = FSStore(args.artifact_path)
    else:
        store = get_store()

    result = gc(store, r, min_age_seconds=min_age, dry_run=args.dry_run)
    print(result)
    # Exit code 0 even if orphans exist; caller can check result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
