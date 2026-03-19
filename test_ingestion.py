#!/usr/bin/env python3
"""
Ingestion Test Script - Measure end-to-end document processing
Processes sample documents and generates a performance report
"""

import base64
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from pydantic import BaseModel

# Configuration
ORCHESTRATOR_URL = "http://localhost:8080"
HEALTH_CHECK_TIMEOUT = 30
MAX_WAIT_TIME = 300  # 5 minutes max per document

# Test documents
TEST_DOCUMENTS = [
    {
        "name": "document1.txt",
        "path": "/tmp/test_documents/document1.txt",
        "description": "Simple technical document",
    },
    {
        "name": "document2.txt",
        "path": "/tmp/test_documents/document2.txt",
        "description": "Financial report",
    },
]


class ResultRecord(BaseModel):
    """Model for tracking processing results"""

    timestamp: str
    document_name: str
    job_id: str
    total_duration: float
    phase_durations: Dict[str, float]
    status: str
    chunks: Optional[int]
    entities_found: Optional[int]
    embeddings_dim: Optional[int]
    error: Optional[str]


class IngestionTester:
    """Handles document ingestion testing and result tracking"""

    def __init__(self, output_file: str = "/tmp/ingestion_results.json"):
        self.output_file = output_file
        self.results: List[ResultRecord] = []
        self.base_url = ORCHESTRATOR_URL
        self.failed_jobs = []

    def check_health(self) -> bool:
        """Check if orchestrator is healthy"""
        print("Checking orchestrator health...")
        start = time.time()
        while time.time() - start < HEALTH_CHECK_TIMEOUT:
            try:
                resp = requests.get(f"{self.base_url}/health", timeout=5)
                if resp.status_code == 200:
                    print("✓ Orchestrator is healthy")
                    return True
            except requests.RequestException:
                time.sleep(1)
        print("✗ Orchestrator health check failed")
        return False

    def submit_document(self, doc_name: str, doc_path: str) -> Optional[str]:
        """Submit a document for processing, return job_id"""
        print(f"\nSubmitting {doc_name}...")
        try:
            # Read file and encode as base64
            with open(doc_path, "rb") as f:
                content = f.read()
            doc_base64 = base64.b64encode(content).decode("utf-8")

            resp = requests.post(
                f"{self.base_url}/v1/documents/process",
                json={"document_base64": doc_base64},
                timeout=10,
            )
            if resp.status_code in (200, 202):  # 200 OK or 202 Accepted
                data = resp.json()
                job_id = data.get("job_id")
                print(f"✓ Job submitted: {job_id}")
                return job_id
            else:
                print(f"✗ Submit failed: {resp.status_code} - {resp.text[:200]}")
                return None
        except Exception as e:
            print(f"✗ Error submitting document: {e}")
            return None

    def wait_for_job(self, job_id: str, doc_name: str) -> Optional[ResultRecord]:
        """Poll job status until completion, measure timing"""
        print(f"Waiting for job {job_id} to complete...")
        start_time = time.time()
        phases_seen = {}
        last_status = None

        while time.time() - start_time < MAX_WAIT_TIME:
            try:
                resp = requests.get(
                    f"{self.base_url}/v1/documents/{job_id}/status",
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status")

                    # Track phase transitions
                    if status != last_status:
                        if status not in phases_seen:
                            phases_seen[status] = time.time() - start_time
                        print(f"  → {status}: {phases_seen[status]:.2f}s")
                        last_status = status

                    if status == "completed":
                        total_duration = time.time() - start_time
                        print(f"✓ Job completed in {total_duration:.2f}s")

                        # Extract metrics
                        chunks = data.get("chunks_count", 0)
                        entities = data.get("entities_count", 0)
                        embedding_dim = data.get("embedding_dimension", 0)

                        return ResultRecord(
                            timestamp=datetime.now().isoformat(),
                            document_name=doc_name,
                            job_id=job_id,
                            total_duration=total_duration,
                            phase_durations=phases_seen,
                            status="completed",
                            chunks=chunks,
                            entities_found=entities,
                            embeddings_dim=embedding_dim,
                            error=None,
                        )

                    elif status == "failed":
                        error = data.get("error", "Unknown error")
                        print(f"✗ Job failed: {error}")
                        self.failed_jobs.append(job_id)

                        return ResultRecord(
                            timestamp=datetime.now().isoformat(),
                            document_name=doc_name,
                            job_id=job_id,
                            total_duration=time.time() - start_time,
                            phase_durations=phases_seen,
                            status="failed",
                            chunks=None,
                            entities_found=None,
                            embeddings_dim=None,
                            error=error,
                        )

                time.sleep(2)

            except Exception as e:
                print(f"  Error checking status: {e}")
                time.sleep(2)

        print(f"✗ Job timeout after {MAX_WAIT_TIME}s")
        self.failed_jobs.append(job_id)
        return ResultRecord(
            timestamp=datetime.now().isoformat(),
            document_name=doc_name,
            job_id=job_id,
            total_duration=MAX_WAIT_TIME,
            phase_durations=phases_seen,
            status="timeout",
            chunks=None,
            entities_found=None,
            embeddings_dim=None,
            error="Processing timeout",
        )

    def run_tests(self):
        """Execute full test suite"""
        print("\n" + "=" * 70)
        print("IA TEXT ORCHESTRATOR - INGESTION TEST")
        print("=" * 70)
        print(f"Start time: {datetime.now().isoformat()}")

        if not self.check_health():
            print("✗ Cannot proceed: orchestrator not healthy")
            return False

        # Process each document
        for doc_config in TEST_DOCUMENTS:
            job_id = self.submit_document(doc_config["name"], doc_config["path"])
            if job_id:
                result = self.wait_for_job(job_id, doc_config["name"])
                if result:
                    self.results.append(result)

        return len(self.results) > 0

    def save_results(self):
        """Save results to disk as JSON"""
        output = {
            "test_metadata": {
                "timestamp": datetime.now().isoformat(),
                "orchestrator_url": self.base_url,
                "total_documents_processed": len(self.results),
                "successful_jobs": len(
                    [r for r in self.results if r.status == "completed"]
                ),
                "failed_jobs": len(
                    [r for r in self.results if r.status != "completed"]
                ),
                "failed_job_ids": self.failed_jobs,
            },
            "results": [r.model_dump() for r in self.results],
        }

        with open(self.output_file, "w") as f:
            json.dump(output, f, indent=2)

        print(f"\n✓ Results saved to {self.output_file}")
        return self.output_file

    def print_summary(self):
        """Print summary statistics"""
        if not self.results:
            print("No results to summarize")
            return

        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)

        successful = [r for r in self.results if r.status == "completed"]
        failed = [r for r in self.results if r.status != "completed"]

        print(f"Total documents: {len(self.results)}")
        print(f"Successful: {len(successful)}")
        print(f"Failed: {len(failed)}")

        if successful:
            durations = [r.total_duration for r in successful]
            print(f"\nTiming (completed jobs):")
            print(f"  Min: {min(durations):.2f}s")
            print(f"  Max: {max(durations):.2f}s")
            print(f"  Avg: {sum(durations) / len(durations):.2f}s")

            total_chunks = sum(r.chunks or 0 for r in successful)
            total_entities = sum(r.entities_found or 0 for r in successful)
            print(f"\nExtracted data:")
            print(f"  Total chunks: {total_chunks}")
            print(f"  Total entities: {total_entities}")

        if failed:
            print(f"\nFailed documents:")
            for r in failed:
                print(f"  - {r.document_name}: {r.error}")

        print("=" * 70)


if __name__ == "__main__":
    tester = IngestionTester()
    if tester.run_tests():
        tester.print_summary()
        output_file = tester.save_results()
        print(f"\nTo view full results: cat {output_file}")
    else:
        print("✗ Test suite failed")
        sys.exit(1)
