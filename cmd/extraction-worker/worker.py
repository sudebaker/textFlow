#!/usr/bin/env python3
import os
import sys
import json
import base64
import hashlib
import subprocess
import tempfile
import logging
import pika
import redis
import requests
import magic
import langdetect
import textstat
from typing import Dict, Optional, List, Any
from urllib.parse import urlparse
from pathlib import Path
import tiktoken

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from pkg.events_python import EventBus
from pkg.worker_common.rabbitmq import parse_rabbitmq_url

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://localhost:5672/")
DOCLING_URL = os.getenv("DOCLING_URL", "http://docling:5001")
QUEUE_NAME = os.getenv("QUEUE_NAME", "extract_text")
PREFETCH_COUNT = int(os.getenv("PREFETCH_COUNT", "3"))

CHUNK_SIZE_TOKENS = int(os.getenv("CHUNK_SIZE_TOKENS", "512"))
CHUNK_OVERLAP_TOKENS = int(os.getenv("CHUNK_OVERLAP_TOKENS", "50"))
EXIFTOOL_PATH = os.getenv("EXIFTOOL_PATH", "/usr/bin/exiftool")

try:
    tokenizer = tiktoken.get_encoding("cl100k_base")
except Exception:
    logger.warning("tiktoken online failed, using simple tokenization")
    tokenizer = None


def compute_file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def extract_pdf_metadata(file_path: str, filename: str) -> Dict[str, Any]:
    metadata = {
        "filename": filename,
        "file_size_bytes": os.path.getsize(file_path)
        if os.path.exists(file_path)
        else 0,
        "sha256": "",
        "author": None,
        "title": None,
        "subject": None,
        "creator": None,
        "producer": None,
        "creation_date": None,
        "modification_date": None,
        "page_count": None,
        "encrypted": False,
        "mime_type": None,
        "exif_data": {},
    }

    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            file_bytes = f.read()
            metadata["sha256"] = compute_file_hash(file_bytes)

        metadata["mime_type"] = magic.from_file(file_path, mime=True)

        try:
            result = subprocess.run(
                [EXIFTOOL_PATH, "-j", file_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                exif_data = json.loads(result.stdout)
                if exif_data:
                    exif = exif_data[0]

                    metadata["author"] = exif.get("Author") or exif.get("Creator")
                    metadata["title"] = exif.get("Title") or exif.get("DocumentTitle")
                    metadata["subject"] = exif.get("Subject")
                    metadata["creator"] = exif.get("Creator") or exif.get("Software")
                    metadata["producer"] = exif.get("Producer")

                    if "CreateDate" in exif:
                        metadata["creation_date"] = exif["CreateDate"]
                    elif "CreationDate" in exif:
                        metadata["creation_date"] = exif["CreationDate"]

                    if "ModifyDate" in exif:
                        metadata["modification_date"] = exif["ModifyDate"]

                    if "PageCount" in exif:
                        metadata["page_count"] = int(exif["PageCount"])

                    metadata["encrypted"] = exif.get("Encrypted", False)

                    metadata["exif_data"] = {
                        k: v
                        for k, v in exif.items()
                        if k not in ["SourceFile", "File:FileSize", "File:MIMEType"]
                    }
        except Exception as e:
            logger.warning(f"exiftool extraction failed: {e}")

    return metadata


def chunk_text(
    text: str, chunk_size: int = CHUNK_SIZE_TOKENS, overlap: int = CHUNK_OVERLAP_TOKENS
) -> List[Dict[str, Any]]:
    chunks = []

    if tokenizer is not None:
        tokens = tokenizer.encode(text)
        start = 0
        chunk_num = 0

        while start < len(tokens):
            end = min(start + chunk_size, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text = tokenizer.decode(chunk_tokens)

            chunks.append(
                {
                    "chunk_id": f"chunk_{chunk_num:03d}",
                    "text": chunk_text,
                    "start_offset": start,
                    "end_offset": end,
                    "token_count": len(chunk_tokens),
                }
            )

            if end >= len(tokens):
                break

            start = end - overlap
            chunk_num += 1
    else:
        chars = list(text)
        char_count = len(chars)
        chars_per_token = 4
        effective_chunk_size = chunk_size * chars_per_token
        effective_overlap = overlap * chars_per_token

        start = 0
        chunk_num = 0

        while start < char_count:
            end = min(start + effective_chunk_size, char_count)
            chunk_chars = chars[start:end]
            chunk_text = "".join(chunk_chars)

            chunks.append(
                {
                    "chunk_id": f"chunk_{chunk_num:03d}",
                    "text": chunk_text,
                    "start_offset": start,
                    "end_offset": end,
                    "token_count": (end - start) // chars_per_token,
                }
            )

            if end >= char_count:
                break

            start = end - effective_overlap
            chunk_num += 1

    logger.info(f"Created {len(chunks)} chunks")
    return chunks


def analyze_text(text: str) -> Dict[str, Any]:
    analysis = {
        "char_count": len(text),
        "word_count": len(text.split()),
        "line_count": len(text.split("\n")),
        "language": "unknown",
        "has_urls": False,
        "has_emails": False,
        "has_numbers": False,
        "encoding": "utf-8",
    }

    try:
        analysis["language"] = langdetect.detect(text)
    except Exception as e:
        logger.warning(f"Language detection failed: {e}")
        analysis["language"] = "unknown"

    analysis["has_urls"] = "http://" in text or "https://" in text
    analysis["has_emails"] = "@" in text and "." in text.split("@")[-1]

    import re

    numbers = re.findall(r"\d+", text)
    analysis["has_numbers"] = len(numbers) > 0

    try:
        analysis["readability_score"] = textstat.flesch_reading_ease(text)
    except Exception as e:
        logger.warning(f"Readability calculation failed: {e}")

    return analysis


class ExtractionWorker:
    def __init__(self):
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.event_bus = EventBus(self.redis_client)
        self.temp_dir = tempfile.mkdtemp()

    def __del__(self):
        import shutil

        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except:
            pass

    def extract_text_from_base64(
        self, document_base64: str, filename: str = "document"
    ) -> Dict[str, Any]:
        try:
            document_bytes = base64.b64decode(document_base64)
            logger.info(f"Decoded {len(document_bytes)} bytes, filename: {filename}")

            # Detectar si es PDF por los magic bytes
            if document_bytes[:4] == b"%PDF":
                if not filename.lower().endswith(".pdf"):
                    filename = filename + ".pdf"
                    logger.info(f"Adjusted filename to: {filename}")

            response = requests.post(
                f"{DOCLING_URL}/convert",
                files={"files": (filename, document_bytes)},
                timeout=300,
            )
            logger.info(f"Docling response status: {response.status_code}")
            response.raise_for_status()

            result = response.json()
            text = result.get("markdown", "") or result.get("text", "")

            metadata = {
                "docling_pages": result.get("document", {}).get("pages", [])
                and len(result.get("document", {}).get("pages", [])),
                "extraction_method": "base64",
            }

            return {"text": text, "metadata": metadata}

        except Exception as e:
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            logger.error(f"Failed to extract text from base64: {e}")
            raise

    def extract_text_from_file(
        self, file_path: str, filename: str = "document"
    ) -> Dict[str, Any]:
        try:
            with open(file_path, "rb") as f:
                document_bytes = f.read()
            logger.info(f"Read {len(document_bytes)} bytes from {file_path}")

            response = requests.post(
                f"{DOCLING_URL}/convert",
                files={"files": (filename, document_bytes)},
                timeout=300,
            )
            logger.info(f"Docling response status: {response.status_code}")
            response.raise_for_status()

            result = response.json()
            # Docling returns markdown text
            text = result.get("markdown", "") or result.get("text", "")

            metadata = {
                "docling_pages": result.get("document", {}).get("pages", [])
                and len(result.get("document", {}).get("pages", [])),
                "extraction_method": "file",
            }

            return {"text": text, "metadata": metadata}

        except Exception as e:
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            logger.error(f"Failed to extract text from file: {e}")
            raise

    def extract_text_from_url(self, document_url: str) -> Dict[str, Any]:
        try:
            if document_url.startswith("http://docling:") or document_url.startswith(
                "http://docling/"
            ):
                if "://docling:" in document_url:
                    url_path = document_url.split("/data/uploads/")[-1]
                    local_path = Path("/app/data/uploads") / url_path
                elif "/app/" in document_url:
                    url_path = document_url.split("/app/")[-1]
                    local_path = Path("/app") / url_path
                else:
                    url_path = document_url.split("/data/uploads/")[-1]
                    local_path = Path("/app/data/uploads") / url_path

                # Validate that resolved path stays within allowed directory
                try:
                    resolved_path = local_path.resolve()
                    allowed_base = Path("/app").resolve()
                    resolved_path.relative_to(allowed_base)
                except ValueError:
                    raise ValueError(f"Path traversal attempt detected: {local_path}")

                with open(resolved_path, "rb") as f:
                    document_bytes = f.read()

                filename = resolved_path.name
            else:
                response = requests.get(document_url, timeout=30)
                response.raise_for_status()
                document_bytes = response.content

                filename = document_url.split("/")[-1]
                if "." not in filename:
                    filename = "document.pdf"

            response = requests.post(
                f"{DOCLING_URL}/convert",
                files={"files": (filename, document_bytes)},
                timeout=300,
            )
            response.raise_for_status()

            result = response.json()
            # Docling returns markdown text
            text = result.get("markdown", "") or result.get("text", "")

            metadata = {
                "docling_pages": result.get("document", {}).get("pages", [])
                and len(result.get("document", {}).get("pages", [])),
                "extraction_method": "url",
            }

            return {"text": text, "metadata": metadata}

        except Exception as e:
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")
            logger.error(f"Failed to extract text from URL: {e}")
            raise

    def process_message(self, ch, method, properties, body):
        job_id = None
        temp_file_path = None

        try:
            message = json.loads(body)
            job_id = message.get("job_id")

            logger.info(f"Processing text extraction for job: {job_id}")

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "extracting"
            )

            if message.get("document_path"):
                result = self.extract_text_from_file(
                    message["document_path"], os.path.basename(message["document_path"])
                )
                text = result["text"]
            elif message.get("document_base64"):
                result = self.extract_text_from_base64(message["document_base64"])
                text = result["text"]
            elif message.get("document_url"):
                result = self.extract_text_from_url(message["document_url"])
                text = result["text"]
            else:
                raise ValueError("No document provided")

            # For metadata extraction, use original file if available
            if message.get("document_path"):
                temp_file_path = message["document_path"]
            else:
                temp_fd, temp_file_path = tempfile.mkstemp(suffix=".pdf")
                try:
                    os.write(
                        temp_fd, base64.b64decode(message.get("document_base64", ""))
                    )
                finally:
                    os.close(temp_fd)

            document_metadata = extract_pdf_metadata(
                temp_file_path,
                os.path.basename(message.get("document_path", "document.pdf")),
            )

            text_metadata = analyze_text(text)

            chunks = chunk_text(text)

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:status", "status", "processing"
            )

            self.redis_client.set(f"orchestrator:job:{job_id}:text", text)
            self.redis_client.set(
                f"orchestrator:job:{job_id}:chunks", json.dumps(chunks)
            )
            self.redis_client.set(
                f"orchestrator:job:{job_id}:metadata:document",
                json.dumps(document_metadata),
            )
            self.redis_client.set(
                f"orchestrator:job:{job_id}:metadata:text", json.dumps(text_metadata)
            )

            self.redis_client.hset(
                f"orchestrator:job:{job_id}:steps", "extraction", "completed"
            )

            logger.info(
                f"Stored for job {job_id}: text={len(text)} chars, chunks={len(chunks)}, doc_metadata keys={list(document_metadata.keys())}"
            )

            params = parse_rabbitmq_url(RABBITMQ_URL)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()

            job_message = {
                "job_id": job_id,
                "chunks": chunks,
                "document_metadata": document_metadata,
            }

            if message.get("entity_types"):
                job_message["entity_types"] = message["entity_types"]

            job_message_json = json.dumps(job_message)
            for queue in ["embeddings", "entities", "metadata"]:
                channel.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=job_message_json,
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type="application/json",
                    ),
                )
                logger.info(f"Published job {job_id} to queue: {queue}")

            connection.close()

            self.event_bus.publish_job_progress(job_id, 25, "processing")

            ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.info(f"Text extraction completed for job: {job_id}")

        except Exception as e:
            logger.error(f"Error processing extraction: {e}")
            if job_id:
                self.redis_client.hset(
                    f"orchestrator:job:{job_id}:status", "status", "failed"
                )
                self.redis_client.set(f"orchestrator:job:{job_id}:error", str(e))
                self.event_bus.publish_job_failed(job_id, str(e))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except:
                    pass


def main():
    worker = ExtractionWorker()

    params = parse_rabbitmq_url(RABBITMQ_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.basic_qos(prefetch_count=PREFETCH_COUNT)
    channel.basic_consume(
        queue=QUEUE_NAME, on_message_callback=worker.process_message, auto_ack=False
    )

    logger.info(f"Extraction worker started. Consuming from queue: {QUEUE_NAME}")
    channel.start_consuming()


if __name__ == "__main__":
    main()
