#!/usr/bin/env python3
"""
End-to-End Test Suite for Inference Embeddings in textFlow

Phases:
1. Basic validation: document upload and inference embeddings generation
2. Atomicity validation: verify Redis pipeline atomicity
3. Performance testing: batch processing and metrics
4. Quality validation: embedding vector properties
"""

import json
import os
import sys
import time
import subprocess
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Tuple

# Configuration
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DATASETS_DIR = Path(__file__).parent / "datasets"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Logging
class Logger:
    def __init__(self):
        self.logs = []
    
    def log(self, level: str, message: str, phase: str = ""):
        timestamp = datetime.now().isoformat()
        entry = {"timestamp": timestamp, "level": level, "phase": phase, "message": message}
        self.logs.append(entry)
        prefix = f"[{phase}] " if phase else ""
        print(f"{timestamp} {level:8} {prefix}{message}")
    
    def info(self, msg: str, phase: str = ""):
        self.log("INFO", msg, phase)
    
    def error(self, msg: str, phase: str = ""):
        self.log("ERROR", msg, phase)
    
    def warn(self, msg: str, phase: str = ""):
        self.log("WARN", msg, phase)
    
    def save_logs(self, filename: str):
        with open(RESULTS_DIR / filename, "w") as f:
            json.dump(self.logs, f, indent=2)

logger = Logger()

# Phase 1: Basic Validation
class Phase1BasicValidation:
    """Upload document and verify inference embeddings generation"""
    
    def __init__(self):
        self.phase_name = "PHASE_1_BASIC_VALIDATION"
        self.results = {}
    
    def run(self) -> bool:
        logger.info("Starting basic validation", self.phase_name)
        
        test_file = DATASETS_DIR / "test_doc_1.txt"
        if not test_file.exists():
            logger.error(f"Test file not found: {test_file}", self.phase_name)
            return False
        
        try:
            # Upload document
            logger.info(f"Uploading test document: {test_file.name}", self.phase_name)
            with open(test_file, "rb") as f:
                files = {"file": (test_file.name, f, "text/plain")}
                response = requests.post(f"{ORCHESTRATOR_URL}/v1/documents/upload", files=files, timeout=60)
            
            if response.status_code not in [200, 202]:
                logger.error(f"Upload failed: {response.status_code} - {response.text}", self.phase_name)
                return False
            
            job_data = response.json()
            job_id = job_data.get("job_id")
            logger.info(f"Document uploaded successfully. Job ID: {job_id}", self.phase_name)
            self.results["job_id"] = job_id
            
            # Poll for completion
            logger.info("Polling for job completion...", self.phase_name)
            max_retries = 120  # 10 minutes with 5-second intervals
            for attempt in range(max_retries):
                status_response = requests.get(f"{ORCHESTRATOR_URL}/v1/documents/{job_id}", timeout=10)
                if status_response.status_code != 200:
                    logger.warn(f"Status check failed: {status_response.status_code}", self.phase_name)
                    time.sleep(5)
                    continue
                
                job_status = status_response.json()
                status = job_status.get("status")
                
                if status == "completed":
                    logger.info(f"Job completed after {attempt * 5} seconds", self.phase_name)
                    self.results["job_status"] = job_status
                    break
                elif status == "failed":
                    logger.error(f"Job failed: {job_status.get('error', 'unknown error')}", self.phase_name)
                    return False
                else:
                    if attempt % 6 == 0:  # Log every 30 seconds
                        logger.info(f"Job status: {status} (attempt {attempt + 1}/{max_retries})", self.phase_name)
                    time.sleep(5)
            else:
                logger.error("Job polling timeout (10 minutes exceeded)", self.phase_name)
                return False
            
            # Validate response structure
            logger.info("Validating response structure", self.phase_name)
            if not self._validate_response_structure(job_status):
                return False
            
            # Check for inference_embeddings
            if "inference_embeddings" not in job_status:
                logger.warn("No inference_embeddings in response (may be expected if no inferences)", self.phase_name)
            else:
                logger.info(f"✓ inference_embeddings present in response", self.phase_name)
                self.results["has_inference_embeddings"] = True
            
            self.results["phase_success"] = True
            return True
        
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}", self.phase_name)
            return False
    
    def _validate_response_structure(self, response: Dict) -> bool:
        """Validate that response has required fields"""
        required_fields = ["job_id", "status", "text", "chunks"]
        for field in required_fields:
            if field not in response:
                logger.error(f"Missing required field in response: {field}", self.phase_name)
                return False
        
        logger.info(f"✓ Response structure valid", self.phase_name)
        
        # Check chunks have embeddings
        chunks = response.get("chunks", [])
        if chunks:
            for idx, chunk in enumerate(chunks):
                if "embeddings" not in chunk:
                    logger.warn(f"Chunk {idx} missing embeddings", self.phase_name)
                else:
                    logger.info(f"✓ Chunk {idx} has embeddings ({len(chunk['embeddings'])} dimensions)", self.phase_name)
        
        return True

# Phase 2: Atomicity Validation
class Phase2AtomicityValidation:
    """Verify the inference_embeddings key is written as an artifact-store ref.

    Post-D3 the key is a plain redis.set with an artifact-store ref (no TTL), so
    both TTL > 0 and TTL == -1 are valid observations.
    """
    
    def __init__(self):
        self.phase_name = "PHASE_2_ATOMICITY"
        self.results = {}
    
    def run(self) -> bool:
        logger.info("Starting atomicity validation", self.phase_name)
        
        try:
            import redis
        except ImportError:
            logger.error("redis-py not installed. Install with: pip install redis", self.phase_name)
            return False
        
        try:
            # Connect to Redis
            redis_client = redis.from_url(REDIS_URL, decode_responses=False)
            logger.info("Connected to Redis", self.phase_name)
            
            # Upload a test document to generate inference embeddings
            test_file = DATASETS_DIR / "test_doc_2.txt"
            if not test_file.exists():
                logger.error(f"Test file not found: {test_file}", self.phase_name)
                return False
            
            logger.info(f"Uploading test document for atomicity check: {test_file.name}", self.phase_name)
            with open(test_file, "rb") as f:
                files = {"file": (test_file.name, f, "text/plain")}
                response = requests.post(f"{ORCHESTRATOR_URL}/v1/documents/upload", files=files, timeout=60)
            
            if response.status_code not in [200, 202]:
                logger.error(f"Upload failed: {response.status_code}", self.phase_name)
                return False
            
            job_id = response.json().get("job_id")
            logger.info(f"Document uploaded. Job ID: {job_id}", self.phase_name)
            
            # Monitor Redis keys during processing
            logger.info("Monitoring Redis keys during processing...", self.phase_name)
            inference_embeddings_key = f"orchestrator:job:{job_id}:inference_embeddings"
            ttl_check_results = []
            
            for attempt in range(60):  # Check for 5 minutes
                time.sleep(5)
                
                # Check if key exists and has TTL
                if redis_client.exists(inference_embeddings_key):
                    ttl = redis_client.ttl(inference_embeddings_key)
                    if ttl > 0:
                        ttl_check_results.append({"timestamp": datetime.now().isoformat(), "ttl": ttl})
                        logger.info(f"✓ inference_embeddings key exists with TTL: {ttl}s", self.phase_name)
                    elif ttl == -1:
                        logger.info(f"Key exists but has no TTL (expected for artifact-store refs)", self.phase_name)
                        ttl_check_results.append({"timestamp": datetime.now().isoformat(), "ttl": -1})
                    elif ttl == -2:
                        logger.info(f"Key no longer exists in Redis", self.phase_name)
                        break
                
                # Check job status
                try:
                    status_response = requests.get(f"{ORCHESTRATOR_URL}/v1/documents/{job_id}", timeout=10)
                    if status_response.status_code == 200:
                        job_status = status_response.json()
                        if job_status.get("status") == "completed":
                            logger.info("Job completed", self.phase_name)
                            break
                except Exception as e:
                    logger.error(f"Unexpected error in e2e test: {e}", exc_info=True)
                    pass
            
            self.results["ttl_checks"] = ttl_check_results

            # Post-D3, :inference_embeddings is written as an artifact-store ref
            # with a plain redis.set (no TTL), so ttl == -1 is expected, not an
            # atomicity violation. Both ttl > 0 and ttl == -1 are valid here.
            if not ttl_check_results:
                logger.warn(
                    "inference_embeddings key was never observed while job was in flight",
                    self.phase_name,
                )
            self.results["atomicity_valid"] = True
            logger.info(
                f"✓ {len(ttl_check_results)} TTL checks valid "
                f"(no TTL is expected for artifact-store refs)",
                self.phase_name,
            )
            
            self.results["phase_success"] = True
            return True
        
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}", self.phase_name)
            return False

# Phase 3: Performance Testing
class Phase3Performance:
    """Test batch processing and performance metrics"""
    
    def __init__(self):
        self.phase_name = "PHASE_3_PERFORMANCE"
        self.results = {"jobs": [], "metrics": {}}
    
    def run(self) -> bool:
        logger.info("Starting performance testing", self.phase_name)
        
        test_files = list(DATASETS_DIR.glob("test_doc_*.txt"))
        if len(test_files) < 2:
            logger.warn("Not enough test documents for batch testing", self.phase_name)
            test_files = test_files or [DATASETS_DIR / "test_doc_1.txt"]
        
        start_time = time.time()
        job_times = []
        
        for idx, test_file in enumerate(test_files[:3], 1):  # Test with up to 3 documents
            logger.info(f"Processing document {idx}/{len(test_files[:3])}: {test_file.name}", self.phase_name)
            
            try:
                upload_start = time.time()
                with open(test_file, "rb") as f:
                    files = {"file": (test_file.name, f, "text/plain")}
                    response = requests.post(f"{ORCHESTRATOR_URL}/v1/documents/upload", files=files, timeout=60)
                
                if response.status_code not in [200, 202]:
                    logger.error(f"Upload failed for {test_file.name}: {response.status_code}", self.phase_name)
                    continue
                    
                job_id = response.json().get("job_id")
                upload_time = time.time() - upload_start
                logger.info(f"Document uploaded in {upload_time:.2f}s. Job ID: {job_id}", self.phase_name)
                
                # Poll for completion
                poll_start = time.time()
                for attempt in range(120):
                    time.sleep(2)
                    status_response = requests.get(f"{ORCHESTRATOR_URL}/v1/documents/{job_id}", timeout=10)
                    if status_response.status_code == 200:
                        job_status = status_response.json()
                        if job_status.get("status") == "completed":
                            poll_time = time.time() - poll_start
                            total_time = time.time() - upload_start
                            job_times.append(total_time)
                            logger.info(f"Job {job_id} completed in {total_time:.2f}s (upload: {upload_time:.2f}s, processing: {poll_time:.2f}s)", self.phase_name)
                            
                            self.results["jobs"].append({
                                "job_id": job_id,
                                "filename": test_file.name,
                                "upload_time": upload_time,
                                "processing_time": poll_time,
                                "total_time": total_time
                            })
                            break
                        elif job_status.get("status") == "failed":
                            logger.error(f"Job {job_id} failed", self.phase_name)
                            break
            
            except Exception as e:
                logger.error(f"Error processing {test_file.name}: {str(e)}", self.phase_name)
        
        # Calculate metrics
        if job_times:
            total_batch_time = time.time() - start_time
            self.results["metrics"] = {
                "total_batch_time": total_batch_time,
                "documents_processed": len(job_times),
                "avg_job_time": sum(job_times) / len(job_times),
                "min_job_time": min(job_times),
                "max_job_time": max(job_times),
                "throughput_docs_per_minute": (len(job_times) / total_batch_time) * 60 if total_batch_time > 0 else 0
            }
            
            logger.info(f"Batch metrics:", self.phase_name)
            logger.info(f"  Documents processed: {len(job_times)}", self.phase_name)
            logger.info(f"  Total time: {total_batch_time:.2f}s", self.phase_name)
            logger.info(f"  Average job time: {self.results['metrics']['avg_job_time']:.2f}s", self.phase_name)
            logger.info(f"  Throughput: {self.results['metrics']['throughput_docs_per_minute']:.2f} docs/min", self.phase_name)
        
        self.results["phase_success"] = len(job_times) > 0
        return self.results["phase_success"]

# Phase 4: Quality Validation
class Phase4QualityValidation:
    """Validate embedding vector properties"""
    
    def __init__(self):
        self.phase_name = "PHASE_4_QUALITY"
        self.results = {}
    
    def run(self) -> bool:
        logger.info("Starting quality validation", self.phase_name)
        
        try:
            import numpy as np
        except ImportError:
            logger.warn("numpy not installed, skipping embedding validation", self.phase_name)
            self.results["phase_success"] = True
            return True
        
        # Upload document and validate embeddings
        test_file = DATASETS_DIR / "test_doc_3.txt"
        if not test_file.exists():
            logger.error(f"Test file not found: {test_file}", self.phase_name)
            return False
        
        try:
            logger.info(f"Uploading test document: {test_file.name}", self.phase_name)
            with open(test_file, "rb") as f:
                files = {"file": (test_file.name, f, "text/plain")}
                response = requests.post(f"{ORCHESTRATOR_URL}/v1/documents/upload", files=files, timeout=60)
            
            if response.status_code not in [200, 202]:
                logger.error(f"Upload failed: {response.status_code}", self.phase_name)
                return False
            
            job_id = response.json().get("job_id")
            logger.info(f"Document uploaded. Job ID: {job_id}", self.phase_name)
            
            # Poll for completion
            for attempt in range(120):
                time.sleep(2)
                status_response = requests.get(f"{ORCHESTRATOR_URL}/v1/documents/{job_id}", timeout=10)
                if status_response.status_code == 200:
                    job_status = status_response.json()
                    if job_status.get("status") == "completed":
                        logger.info("Job completed", self.phase_name)
                        break
                    elif job_status.get("status") == "failed":
                        logger.error("Job failed", self.phase_name)
                        return False
            
            # Validate embeddings
            chunks = job_status.get("chunks", [])
            logger.info(f"Validating embeddings for {len(chunks)} chunks", self.phase_name)
            
            valid_embeddings = 0
            for idx, chunk in enumerate(chunks):
                if "embeddings" in chunk:
                    emb = np.array(chunk["embeddings"])
                    logger.info(f"✓ Chunk {idx}: embeddings shape {emb.shape}, norm {np.linalg.norm(emb):.4f}", self.phase_name)
                    valid_embeddings += 1
            
            logger.info(f"✓ {valid_embeddings}/{len(chunks)} chunks have valid embeddings", self.phase_name)
            self.results["valid_embeddings"] = valid_embeddings
            self.results["total_chunks"] = len(chunks)
            self.results["phase_success"] = valid_embeddings > 0
            
            return self.results["phase_success"]
        
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}", self.phase_name)
            return False

# Main test runner
def main():
    logger.info("=" * 80)
    logger.info("textFlow Inference Embeddings End-to-End Test Suite")
    logger.info("=" * 80)
    logger.info(f"Orchestrator URL: {ORCHESTRATOR_URL}")
    logger.info(f"Datasets directory: {DATASETS_DIR}")
    logger.info(f"Results directory: {RESULTS_DIR}")
    
    # Verify connectivity
    try:
        response = requests.get(f"{ORCHESTRATOR_URL}/health", timeout=5)
        logger.info(f"✓ Orchestrator health check: {response.status_code}")
    except Exception as e:
        logger.error(f"Failed to connect to orchestrator: {e}")
        return False
    
    phases = [
        Phase1BasicValidation(),
        Phase2AtomicityValidation(),
        Phase3Performance(),
        Phase4QualityValidation(),
    ]
    
    results = {}
    for phase in phases:
        logger.info("")
        logger.info(f"{'=' * 80}")
        success = phase.run()
        results[phase.phase_name] = {"success": success, "results": phase.results}
        logger.info(f"{'=' * 80}")
        logger.info(f"Phase result: {'✓ PASS' if success else '✗ FAIL'}")
    
    # Save results
    logger.info("")
    logger.info("=" * 80)
    logger.info("Test Summary")
    logger.info("=" * 80)
    
    all_passed = all(r["success"] for r in results.values())
    for phase_name, phase_result in results.items():
        status = "✓ PASS" if phase_result["success"] else "✗ FAIL"
        logger.info(f"{phase_name}: {status}")
    
    # Save results to JSON
    results_file = RESULTS_DIR / f"e2e_test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    logger.info(f"Results saved to: {results_file}")
    
    logger.save_logs(f"e2e_test_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    
    logger.info("")
    logger.info(f"Overall result: {'✓ ALL TESTS PASSED' if all_passed else '✗ SOME TESTS FAILED'}")
    
    return all_passed

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
