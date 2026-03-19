#!/usr/bin/env python3
"""
End-to-End Test for IA Text Orchestrator
Tests complete document processing pipeline from upload to result retrieval
"""

import json
import time
import sys
import base64
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import requests
except ImportError:
    print("❌ ERROR: requests library not installed")
    print("   Install with: pip install requests")
    sys.exit(1)

# Configuration
ORCHESTRATOR_URL = "http://localhost:8080"
TEST_DOCUMENT_PATH = "/path/to/textflow/data/input/sample-document.pdf"
MAX_WAIT_TIME = 1800  # 30 minutes — CPU mode is slow for large docs
POLL_INTERVAL = 5  # Check status every 5 seconds


def print_section(title: str, char: str = "="):
    """Print a formatted section header."""
    print(f"\n{char * 80}")
    print(f"{title}")
    print(f"{char * 80}\n")


def check_orchestrator_health() -> bool:
    """Check if orchestrator is running and healthy."""
    try:
        response = requests.get(f"{ORCHESTRATOR_URL}/health", timeout=5)
        if response.status_code == 200:
            health = response.json()
            print(f"✅ Orchestrator is healthy")
            print(f"   Service: {health.get('service')}")
            print(f"   Version: {health.get('version')}")
            print(f"   Uptime: {health.get('uptime')}")
            return True
        else:
            print(f"⚠️  Orchestrator returned status {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print(f"❌ Cannot connect to orchestrator at {ORCHESTRATOR_URL}")
        print(f"   Make sure orchestrator is running: docker ps | grep orchestrator")
        return False
    except Exception as e:
        print(f"❌ Error checking orchestrator health: {e}")
        return False


def upload_document(file_path: str) -> Optional[str]:
    """
    Upload document to orchestrator and get job_id.

    Args:
        file_path: Path to PDF file

    Returns:
        job_id if successful, None otherwise
    """
    print(f"📤 Uploading document: {file_path}")

    # Check if file exists
    if not Path(file_path).exists():
        print(f"❌ File not found: {file_path}")
        return None

    # Get file size
    file_size = Path(file_path).stat().st_size
    print(f"   File size: {file_size / 1024:.2f} KB")

    # Read and encode file
    with open(file_path, "rb") as f:
        file_content = f.read()

    document_base64 = base64.b64encode(file_content).decode("utf-8")

    # Prepare request
    payload = {"document_base64": document_base64, "filename": Path(file_path).name}

    try:
        print(f"   Sending request to {ORCHESTRATOR_URL}/v1/documents/process")
        response = requests.post(
            f"{ORCHESTRATOR_URL}/v1/documents/process", json=payload, timeout=30
        )

        # 200 OK, 201 Created, or 202 Accepted are all valid
        if response.status_code in [200, 201, 202]:
            result = response.json()
            job_id = result.get("job_id")
            print(f"✅ Document uploaded successfully")
            print(f"   Job ID: {job_id}")
            print(f"   Status: {result.get('status')}")
            return job_id
        else:
            print(f"❌ Upload failed with status {response.status_code}")
            print(f"   Response: {response.text}")
            return None

    except requests.exceptions.Timeout:
        print(f"❌ Request timed out after 30 seconds")
        return None
    except Exception as e:
        print(f"❌ Error uploading document: {e}")
        return None


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Get current status of a job.

    Args:
        job_id: Job identifier

    Returns:
        Job status dict or None if error
    """
    try:
        response = requests.get(f"{ORCHESTRATOR_URL}/v1/documents/{job_id}", timeout=10)

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            print(f"⚠️  Job not found: {job_id}")
            return None
        else:
            print(f"⚠️  Status check returned {response.status_code}")
            return None

    except Exception as e:
        print(f"⚠️  Error checking status: {e}")
        return None


def wait_for_completion(job_id: str) -> bool:
    """
    Wait for job to complete, polling status periodically.

    Args:
        job_id: Job identifier

    Returns:
        True if completed successfully, False otherwise
    """
    print_section(f"⏳ Waiting for job {job_id} to complete", "-")

    start_time = time.time()
    last_status = None

    while True:
        elapsed = time.time() - start_time

        # Check timeout
        if elapsed > MAX_WAIT_TIME:
            print(f"\n❌ Timeout: Job did not complete within {MAX_WAIT_TIME} seconds")
            return False

        # Get current status
        status = get_job_status(job_id)

        if not status:
            print(f"⚠️  Could not retrieve status, retrying...")
            time.sleep(POLL_INTERVAL)
            continue

        # Extract fields
        current_status = status.get("status", "unknown")
        progress = status.get("progress", 0)
        steps = status.get("steps", {})

        # Print status if changed
        if current_status != last_status:
            print(f"\n[{elapsed:.1f}s] Status: {current_status} ({progress}%)")

            if steps:
                print("   Steps:")
                for step_name, step_status in steps.items():
                    icon = (
                        "✅"
                        if step_status == "completed"
                        else "⏳"
                        if step_status == "pending"
                        else "❌"
                    )
                    print(f"      {icon} {step_name}: {step_status}")

            last_status = current_status
        else:
            # Same status, just print progress
            print(f".", end="", flush=True)

        # Check if completed
        if current_status == "completed":
            print(f"\n\n✅ Job completed successfully in {elapsed:.1f} seconds")
            return True

        # Check if failed
        if current_status == "failed":
            error = status.get("error", "Unknown error")
            print(f"\n\n❌ Job failed: {error}")
            return False

        # Wait before next poll
        time.sleep(POLL_INTERVAL)


def download_results(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Download final results for completed job.

    Args:
        job_id: Job identifier

    Returns:
        Results dict or None if error
    """
    print_section(f"📥 Downloading results for job {job_id}", "-")

    try:
        # Get full job details
        response = requests.get(f"{ORCHESTRATOR_URL}/v1/documents/{job_id}", timeout=10)

        if response.status_code != 200:
            print(f"❌ Could not retrieve results: HTTP {response.status_code}")
            return None

        results = response.json()

        # ✅ ASSERTION 1: Status must be "completed"
        status = results.get("status")
        if status != "completed":
            print(
                f"❌ ASSERTION FAILED: Job status is '{status}', expected 'completed'"
            )
            return None
        print(f"✅ Status assertion passed: {status}")

        # Display summary
        print(f"✅ Results retrieved successfully")
        print(f"\n📊 Summary:")
        print(f"   Status: {results.get('status')}")
        print(f"   Progress: {results.get('progress')}%")

        # Display extracted text length
        if "text" in results:
            text_length = len(results["text"])
            print(f"   Extracted text: {text_length} characters")
            print(f"   First 100 chars: {results['text'][:100]}...")

        # Display entities with assertions
        if "entities" in results:
            entities = results["entities"]

            # ✅ ASSERTION 2: Entities must be a list with at least 1 entity
            if not isinstance(entities, list):
                print(
                    f"❌ ASSERTION FAILED: Entities is not a list, got {type(entities)}"
                )
                return None
            if len(entities) == 0:
                print(f"❌ ASSERTION FAILED: No entities found")
                return None
            print(f"✅ Entities assertion passed: {len(entities)} entities found")

            # ✅ ASSERTION 3: Each entity must have required fields
            required_entity_fields = {"text", "label", "score", "start", "end"}
            for i, entity in enumerate(entities):
                missing_fields = required_entity_fields - set(entity.keys())
                if missing_fields:
                    print(
                        f"❌ ASSERTION FAILED: Entity {i} missing fields: {missing_fields}"
                    )
                    return None
            print(
                f"✅ Entity structure assertion passed: All entities have required fields"
            )

            print(f"\n🏷️  Entities: {len(entities)} found")

            # Count by type
            entity_types = {}
            for entity in entities:
                entity_type = entity.get("label", "unknown")
                entity_types[entity_type] = entity_types.get(entity_type, 0) + 1

            for entity_type, count in sorted(entity_types.items()):
                print(f"      {entity_type}: {count}")

            # Show sample entities
            print(f"\n   Sample entities:")
            for entity in entities[:5]:
                print(
                    f"      • {entity.get('text')} ({entity.get('label')}) - confidence: {entity.get('confidence', 0):.2f}"
                )

        # Display embeddings with assertions
        if "embeddings" in results:
            embeddings = results["embeddings"]

            # ✅ ASSERTION 4: Embeddings must be present
            if embeddings is None:
                print(f"❌ ASSERTION FAILED: Embeddings are None")
                return None

            # Check if embeddings is in expected format (dict with model, dimension, and embedding data)
            if isinstance(embeddings, dict):
                dimension = embeddings.get("dimension", 0)
                model = embeddings.get("model", "unknown")

                # ✅ ASSERTION 5: Dimension must be 1024 for BAAI/bge-m3
                if dimension != 1024:
                    print(
                        f"❌ ASSERTION FAILED: Embedding dimension is {dimension}, expected 1024"
                    )
                    return None
                print(f"✅ Embedding dimension assertion passed: {dimension}")

                # ✅ ASSERTION 6: Must have embedding data (count chunks with embeddings)
                embedding_count = 0
                for key in embeddings:
                    if key not in ["model", "dimension"] and isinstance(
                        embeddings[key], (list, dict)
                    ):
                        embedding_count += 1

                if embedding_count == 0:
                    print(f"❌ ASSERTION FAILED: No embedding data found")
                    return None
                print(
                    f"✅ Embedding data assertion passed: Found embeddings for {embedding_count} chunks"
                )
            elif isinstance(embeddings, list):
                print(f"🔢 Embeddings: {len(embeddings)} dimensions")
                print(f"   Sample: {embeddings[:5]}...")

        # Display chunks with assertion
        if "chunks" in results:
            chunks = results["chunks"]

            # ✅ ASSERTION 7: Chunks must be a list with at least 1 chunk
            if not isinstance(chunks, list):
                print(f"❌ ASSERTION FAILED: Chunks is not a list, got {type(chunks)}")
                return None
            if len(chunks) == 0:
                print(f"❌ ASSERTION FAILED: No chunks found")
                return None
            print(f"✅ Chunks assertion passed: {len(chunks)} chunks found")

        # Display metadata
        if "metadata" in results:
            metadata = results["metadata"]
            print(f"\n📄 Metadata:")
            for key, value in metadata.items():
                if isinstance(value, (str, int, float)):
                    print(f"      {key}: {value}")

        return results

    except Exception as e:
        print(f"❌ Error downloading results: {e}")
        return None


def save_results_to_file(job_id: str, results: Dict[str, Any]) -> bool:
    """
    Save results to JSON file.

    Args:
        job_id: Job identifier
        results: Results dictionary

    Returns:
        True if saved successfully
    """
    output_dir = Path("/path/to/textflow/data/output")
    output_dir.mkdir(exist_ok=True, parents=True)

    output_file = output_dir / f"result_{job_id}.json"

    try:
        with open(output_file, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        print(f"\n💾 Results saved to: {output_file}")
        print(f"   File size: {output_file.stat().st_size / 1024:.2f} KB")
        return True

    except Exception as e:
        print(f"❌ Error saving results: {e}")
        return False


def main():
    """Main test execution."""
    print_section("🧪 IA Text Orchestrator - End-to-End Test")

    print("Test configuration:")
    print(f"   Orchestrator: {ORCHESTRATOR_URL}")
    print(f"   Test document: {TEST_DOCUMENT_PATH}")
    print(f"   Max wait time: {MAX_WAIT_TIME} seconds")
    print(f"   Poll interval: {POLL_INTERVAL} seconds")

    # Step 1: Check orchestrator health
    print_section("Step 1: Check Orchestrator Health")
    if not check_orchestrator_health():
        print("\n❌ Test aborted: Orchestrator is not healthy")
        return 1

    # Step 2: Upload document
    print_section("Step 2: Upload Document")
    job_id = upload_document(TEST_DOCUMENT_PATH)

    if not job_id:
        print("\n❌ Test aborted: Document upload failed")
        return 1

    # Step 3: Wait for completion
    print_section("Step 3: Monitor Job Progress")
    success = wait_for_completion(job_id)

    if not success:
        print("\n❌ Test failed: Job did not complete successfully")
        return 1

    # Step 4: Download results
    print_section("Step 4: Download Results")
    results = download_results(job_id)

    if not results:
        print("\n❌ Test failed: Could not download results or assertions failed")
        return 1

    # Step 5: Save results to file
    print_section("Step 5: Save Results to File")
    if not save_results_to_file(job_id, results):
        print("\n⚠️  Warning: Could not save results to file")

    # Final summary
    print_section("✅ Test Completed Successfully", "=")
    print(f"Job ID: {job_id}")
    print(f"Status: {results.get('status')}")
    print(f"Entities extracted: {len(results.get('entities', []))}")
    print(f"Text length: {len(results.get('text', ''))} characters")
    print(f"\n🎉 End-to-end test passed!")

    return 0


if __name__ == "__main__":
    sys.exit(main())
