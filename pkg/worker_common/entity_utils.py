import hashlib
import re
from typing import Dict, List, Set

from rapidfuzz import fuzz
from unidecode import unidecode

SCHEMA_VERSION = "1.1.0"

_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_entity_text(text: str) -> str:
    if not text:
        return ""
    text = unidecode(text)
    text = _PUNCT_RE.sub("", text)
    return text.lower().strip()


def fuzzy_match_score(a: str, b: str) -> float:
    return fuzz.ratio(a, b)


def entity_id(label: str, text: str) -> str:
    """Return a stable 12-char hex ID for a (label, text) pair.

    Uses normalize_entity_text (unidecode + remove punct + lower + strip) so
    accented variants ("María" / "Maria") and case differ only in normalization.
    """
    key = f"{label}:{normalize_entity_text(text)}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def deduplicate_entities(entities: list, threshold: float = 0.85) -> dict:
    """Deduplicate entities using fuzzy text matching, keeping highest confidence.

    Two entities merge when they share the same label AND their normalized texts
    are similar enough (fuzzy_match_score / 100 >= threshold). Normalization uses
    normalize_entity_text (unidecode + remove punct + lower + strip) so accented
    variants ("Educación" / "Educacion") are treated as identical.

    Args:
        entities: List of entity dicts, each expected to have:
            - entity_id (optional): stable 12-char hex ID
            - label, text, confidence

    Returns:
        Dict keyed by entity_id → {label, text, confidence, start_offset,
        end_offset, chunk_id}. Per-chunk fields (chunk_id, start, end) are
        preserved as start_offset, end_offset, chunk_id in the merged entity.
        Falls back to entity_id(label, text) if the field is missing.
    """
    if not entities:
        return {}

    result: dict = {}
    norm_index: dict = {}

    for ent in entities:
        label = ent.get("label", "")
        text = ent.get("text", "")
        confidence = ent.get("confidence", 0.0)
        norm_text = normalize_entity_text(text)

        matched_id = None
        for existing_id, existing_norm in norm_index.items():
            if result[existing_id]["label"] != label:
                continue
            similarity = fuzzy_match_score(norm_text, existing_norm) / 100.0
            if similarity >= threshold:
                matched_id = existing_id
                break

        if matched_id:
            if confidence > result[matched_id].get("confidence", 0):
                result[matched_id] = {
                    "label": label,
                    "text": text,
                    "confidence": confidence,
                    "start_offset": ent.get("start", 0),
                    "end_offset": ent.get("end", 0),
                    "chunk_id": ent.get("chunk_id", ""),
                }
                norm_index[matched_id] = norm_text
        else:
            eid = ent.get("entity_id") or entity_id(label, text)
            result[eid] = {
                "label": label,
                "text": text,
                "confidence": confidence,
                "start_offset": ent.get("start", 0),
                "end_offset": ent.get("end", 0),
                "chunk_id": ent.get("chunk_id", ""),
            }
            norm_index[eid] = norm_text

    return result


def resolve_entity_refs(
    entity_refs: List[str],
    entities_dict: Dict[str, dict],
    fuzzy_threshold: float = 0.85,
) -> List[str]:
    resolved: Set[str] = set()
    threshold = fuzzy_threshold * 100.0
    for ref in entity_refs:
        normalized_ref = normalize_entity_text(ref)
        if not normalized_ref:
            continue

        matched = False
        for ent_id, ent in entities_dict.items():
            if normalize_entity_text(ent.get("text", "")) == normalized_ref:
                resolved.add(ent_id)
                matched = True
                break

        if not matched:
            best_score = 0.0
            best_id = None
            for ent_id, ent in entities_dict.items():
                score = fuzzy_match_score(
                    normalized_ref, normalize_entity_text(ent.get("text", ""))
                )
                if score >= threshold and score > best_score:
                    best_score = score
                    best_id = ent_id
            if best_id:
                resolved.add(best_id)

    return sorted(resolved)
