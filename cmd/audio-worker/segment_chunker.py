from typing import List

from pkg.audio_client.models import AudioSegment, TranscriptionResult

CHARS_PER_TOKEN = 4


def _simple_chunk(text: str, max_chars: int) -> List[dict]:
    """Split text into character-based chunks mirroring chunk_text() format."""
    chunks = []
    start = 0
    chunk_num = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append({
            "chunk_id": f"chunk_{chunk_num:03d}",
            "text": text[start:end],
            "start_offset": start,
            "end_offset": end,
            "token_count": (end - start) // CHARS_PER_TOKEN,
        })
        if end >= len(text):
            break
        start = end
        chunk_num += 1
    return chunks


class SegmentChunker:
    """Groups diarized segments by speaker turn into text chunks.
    
    Output format matches chunk_text() in extraction-worker so downstream
    workers receive identical chunk structures regardless of content type.
    """

    def __init__(self, max_chars: int = 1500):
        self.max_chars = max_chars

    def chunk(self, result: TranscriptionResult) -> List[dict]:
        if not result.segments:
            return _simple_chunk(result.text, self.max_chars)
        return self._chunk_by_speaker(result.segments)

    def _chunk_by_speaker(self, segments: List[AudioSegment]) -> List[dict]:
        """Merge consecutive same-speaker segments; split on max_chars."""
        chunks = []
        chunk_num = 0
        buffer_speaker: str | None = None
        buffer_texts: List[str] = []
        buffer_start = 0

        def _flush(end_offset: int) -> None:
            nonlocal chunk_num
            if not buffer_texts:
                return
            combined = " ".join(buffer_texts)
            prefix = f"[{buffer_speaker}]: " if buffer_speaker else ""
            full_text = f"{prefix}{combined}"
            chunks.append({
                "chunk_id": f"chunk_{chunk_num:03d}",
                "text": full_text,
                "start_offset": buffer_start,
                "end_offset": end_offset,
                "token_count": len(full_text) // CHARS_PER_TOKEN,
            })
            chunk_num += 1

        for i, seg in enumerate(segments):
            if seg.speaker != buffer_speaker or (
                sum(len(t) for t in buffer_texts) + len(seg.text) > self.max_chars
            ):
                _flush(i)
                buffer_speaker = seg.speaker
                buffer_texts = [seg.text]
                buffer_start = i
            else:
                buffer_texts.append(seg.text)

        _flush(len(segments))
        return chunks