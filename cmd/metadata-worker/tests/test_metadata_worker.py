import pytest
import json
import hashlib
from unittest.mock import Mock, patch, MagicMock

from worker import MetadataWorker


class TestMetadataWorker:
    @pytest.fixture
    def worker(self):
        with patch("redis.from_url"):
            return MetadataWorker()

    @pytest.fixture
    def mock_worker(self, worker):
        worker._redis_client = Mock()
        worker._event_bus = Mock()
        return worker


class TestExtractMetadata(TestMetadataWorker):
    def test_basic_metadata(self, worker):
        text = "Hello world. This is a test document."
        metadata = worker._extract_metadata(text)

        assert metadata["text_length"] == len(text)
        assert metadata["word_count"] == len(text.split())
        assert metadata["line_count"] == 1
        assert metadata["char_count"] == len(text)
        assert metadata["content_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert "extracted_at" in metadata

    def test_multiline_text(self, worker):
        text = "Line one.\nLine two.\nLine three."
        metadata = worker._extract_metadata(text)

        assert metadata["line_count"] == 3

    def test_language_detection_spanish(self, worker):
        text = " el documento de la empresa que fue enviado en el año pasado "
        metadata = worker._extract_metadata(text)
        assert metadata["language"] == "es"

    def test_language_detection_english(self, worker):
        text = " the document of the company that was sent in the year "
        metadata = worker._extract_metadata(text)
        assert metadata["language"] == "en"

    def test_language_detection_unknown(self, worker):
        text = "xyz abcdef ghi jkl"
        metadata = worker._extract_metadata(text)
        assert metadata["language"] == "unknown"

    def test_has_urls_true(self, worker):
        text = "Visit https://example.com for more info."
        metadata = worker._extract_metadata(text)
        assert metadata["has_urls"] is True

    def test_has_urls_false(self, worker):
        text = "No links here."
        metadata = worker._extract_metadata(text)
        assert metadata["has_urls"] is False

    def test_has_emails_true(self, worker):
        text = "Contact us at info@example.com"
        metadata = worker._extract_metadata(text)
        assert metadata["has_emails"] is True

    def test_has_emails_false(self, worker):
        text = "No email here."
        metadata = worker._extract_metadata(text)
        assert metadata["has_emails"] is False

    def test_has_numbers_true(self, worker):
        text = "The value is 42."
        metadata = worker._extract_metadata(text)
        assert metadata["has_numbers"] is True

    def test_has_numbers_false(self, worker):
        text = "No numbers here."
        metadata = worker._extract_metadata(text)
        assert metadata["has_numbers"] is False

    def test_sentence_length_calculation(self, worker):
        text = "Word word word. Word word. Word."
        metadata = worker._extract_metadata(text)
        # 6 words, 3 sentences → 6/3 = 2.0
        assert metadata["avg_sentence_length"] == 2.0

    def test_no_sentences(self, worker):
        text = "No punctuation here"
        metadata = worker._extract_metadata(text)
        assert metadata["avg_sentence_length"] == 0

    def test_document_url_sets_source_and_mime(self, worker):
        text = "Some content"
        metadata = worker._extract_metadata(text, document_url="https://example.com/doc.pdf")
        assert metadata["source_url"] == "https://example.com/doc.pdf"
        assert metadata["mime_type"] == "application/pdf"

    def test_document_url_no_mime(self, worker):
        text = "Some content"
        metadata = worker._extract_metadata(text, document_url="https://example.com/custom")
        assert metadata["source_url"] == "https://example.com/custom"
        assert "mime_type" not in metadata

    def test_content_hash_deterministic(self, worker):
        text = "Same text"
        m1 = worker._extract_metadata(text)
        m2 = worker._extract_metadata(text)
        assert m1["content_hash"] == m2["content_hash"]

    def test_content_hash_differs(self, worker):
        m1 = worker._extract_metadata("Text A")
        m2 = worker._extract_metadata("Text B")
        assert m1["content_hash"] != m2["content_hash"]


class TestDetectLanguage(TestMetadataWorker):
    def test_spanish(self, worker):
        assert worker._detect_language(" el documento de la empresa ") == "es"

    def test_english(self, worker):
        assert worker._detect_language(" the document of the company ") == "en"

    def test_unknown(self, worker):
        assert worker._detect_language("xyz abcdef") == "unknown"

    def test_mixed_favors_majority(self, worker):
        # More Spanish words than English
        text = " el la de que y a en un ser se the "
        assert worker._detect_language(text) == "es"


class TestProcessMessage(TestMetadataWorker):
    def test_process_message_success(self, mock_worker):
        mock_worker.redis_client.get.return_value = "Hello world. This is a test."

        message = {"job_id": "job-123", "document_url": "https://example.com/doc.pdf"}
        result = mock_worker.process_message(message)

        assert result["word_count"] == 6
        mock_worker.redis_client.set.assert_called_once()
        mock_worker.redis_client.hset.assert_called_once_with(
            "orchestrator:job:job-123:steps", "metadata", "completed"
        )
        mock_worker._event_bus.publish_job_progress.assert_called_once_with(
            "job-123", 100, "metadata"
        )

    def test_process_message_stores_correct_key(self, mock_worker):
        mock_worker.redis_client.get.return_value = "Some text"

        message = {"job_id": "job-456"}
        mock_worker.process_message(message)

        call_args = mock_worker.redis_client.set.call_args
        assert call_args[0][0] == "orchestrator:job:job-456:metadata"

    def test_process_message_stored_metadata_is_json(self, mock_worker):
        mock_worker.redis_client.get.return_value = "Some text"

        message = {"job_id": "job-789"}
        mock_worker.process_message(message)

        stored_json = mock_worker.redis_client.set.call_args[0][1]
        parsed = json.loads(stored_json)
        assert "word_count" in parsed
        assert "content_hash" in parsed

    def test_process_message_no_text_raises(self, mock_worker):
        mock_worker.redis_client.get.return_value = None

        message = {"job_id": "job-no-text"}
        with pytest.raises(ValueError, match="No text found"):
            mock_worker.process_message(message)

    def test_process_message_empty_text_raises(self, mock_worker):
        mock_worker.redis_client.get.return_value = ""

        message = {"job_id": "job-empty"}
        with pytest.raises(ValueError, match="No text found"):
            mock_worker.process_message(message)

    def test_process_message_uses_document_url(self, mock_worker):
        mock_worker.redis_client.get.return_value = "Content"

        message = {"job_id": "job-url", "document_url": "https://example.com/report.docx"}
        result = mock_worker.process_message(message)

        assert result["source_url"] == "https://example.com/report.docx"
        assert result["mime_type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
