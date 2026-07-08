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
