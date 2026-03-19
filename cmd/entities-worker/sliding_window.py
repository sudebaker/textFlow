"""
Sliding Window Processing for GLiNER Entity Extraction

GLiNER has a maximum input length of ~384 words (approximately 512 tokens).
When processing chunks of 512 tokens, this implementation uses a sliding window
approach to prevent information loss:

- Window 1: tokens 0-384
- Window 2: tokens 128-512 (overlap of 128 tokens)

This ensures complete coverage of the 512-token chunks while respecting
the model's limitations.
"""

import logging
from typing import List, Dict, Any, Tuple
from rapidfuzz import fuzz
from unidecode import unidecode

logger = logging.getLogger(__name__)

# Constants
GLINER_MAX_WORDS = 384  # GLiNER max length in words
WORDS_PER_TOKEN_RATIO = 1.33  # Approximately 384 words ≈ 512 tokens
GLINER_MAX_TOKENS = int(
    GLINER_MAX_WORDS / WORDS_PER_TOKEN_RATIO
)  # ~288 tokens as safe estimate

# For actual chunks (512 tokens), we use sliding window
CHUNK_TOKEN_SIZE = 512
WINDOW_TOKEN_SIZE = 384
OVERLAP_TOKENS = 128  # 512 - 384

# Deduplication parameters
FUZZY_SIMILARITY_THRESHOLD = 0.80  # 80% text similarity required for fuzzy match
OVERLAP_REGION_BUFFER = 20  # Characters to consider as "overlap region"


def estimate_tokens(text: str) -> int:
    """
    Estimate token count from text (rough approximation).
    Uses simple word-based estimation: 1 word ≈ 1.33 tokens (accounting for subwords).
    """
    words = len(text.split())
    return int(words * WORDS_PER_TOKEN_RATIO)


def split_with_sliding_window(
    text: str, window_size: int = WINDOW_TOKEN_SIZE, overlap: int = OVERLAP_TOKENS
) -> List[Tuple[str, int]]:
    """
    Split text into overlapping windows.

    Args:
        text: Input text to split
        window_size: Window size in characters (approximated from tokens)
        overlap: Overlap size in characters

    Returns:
        List of (window_text, global_offset) tuples
    """
    # Approximate character count: ~5 chars per word, 1.33 tokens per word
    # So: tokens = len(text) / 5 / 1.33, or len(text) ≈ tokens * 6.65
    chars_per_token = 6.65
    window_chars = int(window_size * chars_per_token)
    overlap_chars = int(overlap * chars_per_token)

    step = window_chars - overlap_chars
    windows = []

    for start in range(0, len(text), step):
        end = min(start + window_chars, len(text))

        # For the last window, ensure we get the remaining text
        if end - start < window_chars // 2 and start > 0:
            # If the last window is too small, merge with previous
            break

        window_text = text[start:end]
        windows.append((window_text, start))

        if end >= len(text):
            break

    logger.debug(
        f"Split text (len={len(text)}) into {len(windows)} windows "
        f"(window={window_chars}c, overlap={overlap_chars}c, step={step}c)"
    )

    return windows


def normalize_entity_text(text: str) -> str:
    """
    Normalize entity text for comparison.

    Args:
        text: Entity text to normalize

    Returns:
        Normalized text (lowercase, no accents, trimmed)
    """
    return unidecode(text).lower().strip()


def positions_overlap(
    entity1: Dict[str, Any],
    entity2: Dict[str, Any],
    buffer: int = OVERLAP_REGION_BUFFER,
) -> bool:
    """
    Check if two entities have overlapping positions.

    Args:
        entity1: First entity with 'start' and 'end' positions
        entity2: Second entity with 'start' and 'end' positions
        buffer: Number of characters to consider as "close enough" for overlap

    Returns:
        True if positions overlap or are very close
    """
    start1 = entity1.get("start", 0)
    end1 = entity1.get("end", 0)
    start2 = entity2.get("start", 0)
    end2 = entity2.get("end", 0)

    # Check for actual overlap
    if start1 <= end2 + buffer and start2 <= end1 + buffer:
        return True

    return False


def entities_are_duplicate(
    entity1: Dict[str, Any],
    entity2: Dict[str, Any],
    similarity_threshold: float = FUZZY_SIMILARITY_THRESHOLD,
) -> bool:
    """
    Check if two entities are duplicates using fuzzy matching.

    Uses token_set_ratio for better matching of variations and subsets
    (e.g., "New York" vs "New York City" will match).

    Args:
        entity1: First entity
        entity2: Second entity
        similarity_threshold: Minimum similarity ratio (0-1)

    Returns:
        True if entities are considered duplicates
    """
    # Must have same label
    if entity1.get("label", "") != entity2.get("label", ""):
        return False

    # Must have overlapping positions
    if not positions_overlap(entity1, entity2):
        return False

    # Check text similarity using fuzzy matching
    text1 = normalize_entity_text(entity1.get("text", ""))
    text2 = normalize_entity_text(entity2.get("text", ""))

    if not text1 or not text2:
        return False

    # Use token_set_ratio for better matching:
    # - Handles word reordering
    # - Handles subsets ("New York" vs "New York City")
    # - More robust than token_sort_ratio
    similarity = fuzz.token_set_ratio(text1, text2) / 100.0

    is_duplicate = similarity >= similarity_threshold

    if is_duplicate:
        logger.debug(
            f"Duplicate detected: '{text1}' vs '{text2}' "
            f"(similarity={similarity:.2%}, label={entity1.get('label')})"
        )

    return is_duplicate


def merge_entities(
    entities_list: List[List[Dict[str, Any]]], offsets: List[int], text_length: int
) -> List[Dict[str, Any]]:
    """
    Merge entities from multiple windows, handling duplicates from overlap regions.

    Uses fuzzy matching to detect and eliminate duplicate entities that appear
    in overlapping regions between consecutive windows.

    Args:
        entities_list: List of entity lists from each window
        offsets: List of global offsets for each window
        text_length: Total length of the original text

    Returns:
        Merged and deduplicated entities with corrected global positions
    """
    if not entities_list:
        return []

    # Step 1: Flatten and adjust positions to global coordinates
    all_entities = []
    for window_idx, (entities, offset) in enumerate(zip(entities_list, offsets)):
        for entity in entities:
            entity_copy = dict(entity)
            if "start" in entity_copy and "end" in entity_copy:
                entity_copy["start"] = entity_copy.get("start", 0) + offset
                entity_copy["end"] = entity_copy.get("end", 0) + offset
            entity_copy["_window"] = window_idx  # Track source window for debugging
            all_entities.append(entity_copy)

    if not all_entities:
        return []

    logger.debug(
        f"Deduplicating {len(all_entities)} entities from {len(entities_list)} windows"
    )

    # Step 2: Deduplicate using fuzzy matching
    merged = {}
    duplicates_removed = 0

    for entity in all_entities:
        found_duplicate = False

        # Check against all existing merged entities
        for merged_key, merged_entity in list(merged.items()):
            if entities_are_duplicate(entity, merged_entity):
                # Duplicate found - keep the one with higher confidence
                entity_score = entity.get("score", entity.get("confidence", 0))
                merged_score = merged_entity.get(
                    "score", merged_entity.get("confidence", 0)
                )

                if entity_score > merged_score:
                    logger.debug(
                        f"Replacing lower-confidence duplicate: "
                        f"'{merged_entity.get('text')}' (score={merged_score:.3f}) → "
                        f"'{entity.get('text')}' (score={entity_score:.3f})"
                    )
                    merged[merged_key] = entity
                else:
                    logger.debug(
                        f"Keeping existing entity over duplicate: "
                        f"'{merged_entity.get('text')}' (score={merged_score:.3f}) > "
                        f"'{entity.get('text')}' (score={entity_score:.3f})"
                    )

                found_duplicate = True
                duplicates_removed += 1
                break

        # If not a duplicate, add as new entity
        if not found_duplicate:
            # Create a unique key for the merged dict
            key = (
                entity.get("label", ""),
                entity.get("start", 0),
                entity.get("end", 0),
                normalize_entity_text(entity.get("text", "")),
            )
            merged[key] = entity

    # Step 3: Clean up tracking fields and return results
    result = []
    for entity in merged.values():
        entity_clean = {k: v for k, v in entity.items() if not k.startswith("_")}
        result.append(entity_clean)

    logger.info(
        f"Merging complete: {len(all_entities)} raw entities → "
        f"{len(result)} deduplicated entities "
        f"(removed {duplicates_removed} duplicates)"
    )

    return result


def requires_sliding_window(
    text: str, threshold_tokens: int = GLINER_MAX_TOKENS
) -> bool:
    """
    Check if text exceeds GLiNER's safe processing length and requires sliding window.

    Uses GLINER_MAX_TOKENS (not WINDOW_TOKEN_SIZE) as the default threshold to provide
    a safety margin. The word-to-token ratio estimate (1.33) can undercount actual
    DeBERTa subword tokens, especially for Spanish legal text (~1.37-1.50 ratio).
    Using GLINER_MAX_TOKENS=288 as threshold ensures chunks stay well under the
    model's hard 384-token limit.

    Args:
        text: Input text
        threshold_tokens: Estimated-token threshold above which sliding window is used

    Returns:
        True if sliding window is needed
    """
    estimated_tokens = estimate_tokens(text)
    return estimated_tokens > threshold_tokens


def process_with_sliding_window(
    text: str,
    predict_fn,
    entity_types: List[str],
    threshold: float = 0.1,
) -> List[Dict[str, Any]]:
    """
    Process text with GLiNER using sliding window if needed.

    Args:
        text: Input text to process
        predict_fn: GLiNER's predict_entities function
        entity_types: Entity types to extract
        threshold: Confidence threshold

    Returns:
        List of extracted entities with global positions
    """
    estimated_tokens = estimate_tokens(text)

    # If text is small enough, process directly
    if estimated_tokens <= WINDOW_TOKEN_SIZE:
        logger.debug(
            f"Text has {estimated_tokens} estimated tokens (≤{WINDOW_TOKEN_SIZE}), "
            f"processing without sliding window"
        )
        try:
            entities = predict_fn(text, entity_types, threshold=threshold)
            return entities if entities else []
        except Exception as e:
            logger.error(f"Error processing text: {e}")
            return []

    # Use sliding window for larger texts
    logger.info(
        f"Text has {estimated_tokens} estimated tokens (>{WINDOW_TOKEN_SIZE}), "
        f"using sliding window approach (overlap={OVERLAP_TOKENS}t)"
    )

    windows = split_with_sliding_window(text)
    logger.debug(f"Processing {len(windows)} windows")

    entities_list = []
    offsets = []

    for window_idx, (window_text, offset) in enumerate(windows):
        try:
            logger.debug(
                f"  Window {window_idx + 1}/{len(windows)}: "
                f"offset={offset}, len={len(window_text)}"
            )

            entities = predict_fn(window_text, entity_types, threshold=threshold)
            if not entities:
                entities = []

            entities_list.append(entities)
            offsets.append(offset)
        except Exception as e:
            logger.warning(f"Error processing window {window_idx}: {e}")
            entities_list.append([])
            offsets.append(offset)

    # Merge entities from all windows
    merged_entities = merge_entities(entities_list, offsets, len(text))

    logger.info(
        f"Sliding window processing complete: "
        f"{sum(len(e) for e in entities_list)} raw entities → "
        f"{len(merged_entities)} deduplicated entities"
    )

    return merged_entities
