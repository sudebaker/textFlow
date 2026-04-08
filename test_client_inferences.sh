#!/bin/bash
#
# Test script: Verify client + orchestrator support for inferences with images/audio
#
# Prerequisites:
#   - make infra-up (RabbitMQ, Redis, Docling)
#   - make run-orchestrator (port 8080)
#   - Optional: make run-embeddings-worker, run-entities-worker, run-inference-worker, run-completion-worker
#
# Usage:
#   bash test_client_inferences.sh [test_name]
#
# Available tests:
#   - test_image_inferences
#   - test_audio_inferences
#   - test_image_without_inferences
#   - test_redis_features_storage
#   - test_full_e2e_image
#

set -e

# Configuration constants
API_URL="http://localhost:8080"
CLIENT_BIN="./bin/client"
REDIS_CLI="redis-cli"
TEMP_DIR="/tmp/ia-text-orchestrator-tests-$$"
AUDIO_SAMPLE_RATE=16000
AUDIO_DURATION_SEC=1
AUDIO_BITS_PER_SAMPLE=16
TEST_TIMEOUT=30s

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Cleanup function: removes temporary files on exit
cleanup() {
    if [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
}
trap cleanup EXIT

# Create temporary directory for test files
mkdir -p "$TEMP_DIR"

# Color output functions
print_info() {
    echo -e "${YELLOW}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1"
}

# Create minimal PNG (1x1 transparent PNG)
# Deduplicated implementation: single function replacing the duplicated code
create_test_png() {
    local output_file="$1"
    python3 -c "
import base64
# Minimal valid 1x1 transparent PNG (base64 encoded)
png_data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
with open('$output_file', 'wb') as f:
    f.write(png_data)
"
}

# Create minimal WAV file (1 second of silence, 16kHz mono)
create_test_wav() {
    local output_file="$1"
    python3 -c "
import struct

channels = 1
sample_rate = $AUDIO_SAMPLE_RATE
bits_per_sample = $AUDIO_BITS_PER_SAMPLE

# Audio data (silence)
samples = bytes(sample_rate * channels * bits_per_sample // 8)
riff_size = 36 + len(samples)

# WAV header
wav_header = struct.pack(
    '<4sI4s4sIHHIIHH4sI',
    b'RIFF', riff_size, b'WAVE',
    b'fmt ', 16,           # subchunk1size
    1,                      # audio format (PCM)
    channels,
    sample_rate,
    sample_rate * channels * bits_per_sample // 8,  # byte rate
    channels * bits_per_sample // 8,                 # block align
    bits_per_sample,
    b'data', len(samples)
)

with open('$output_file', 'wb') as f:
    f.write(wav_header + samples)
"
}

# Extract job ID from output (more robust UUID pattern matching)
# Matches standard UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
extract_job_id() {
    local output="$1"
    # First try: look for "Job created: <uuid>" or "Job ID: <uuid>"
    local job_id=$(echo "$output" | grep -oE 'Job (created|ID): [a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}' | awk -F': ' '{print $NF}' | head -1)
    
    # Fallback: search for UUID anywhere in output
    if [ -z "$job_id" ]; then
        job_id=$(echo "$output" | grep -oE '[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}' | head -1)
    fi
    
    echo "$job_id"
}

# Test 1: Image with inferences flag
test_image_inferences() {
    print_info "Test 1: Image upload with inferences flag (-f)"
    
    # Create test image
    create_test_png $TEMP_DIR/test_image.png
    
    # Upload with -f flag
    print_info "Uploading image with -f flag..."
    OUTPUT=$($CLIENT_BIN -i $TEMP_DIR/test_image.png -o $TEMP_DIR/test_image_results.json -u "$API_URL" -f --timeout 30s 2>&1 || true)
    
    if echo "$OUTPUT" | grep -q "Job created\|Job ID"; then
        print_success "Job created successfully"
        
        # Extract job ID
        JOB_ID=$(extract_job_id "$OUTPUT")
        if [ -z "$JOB_ID" ]; then
            print_error "✗ Could not extract job ID from output"
            return 1
        fi
        
        print_info "Job ID: $JOB_ID"
        
        # Verify features are stored in Redis
        FEATURES=$($REDIS_CLI GET "orchestrator:job:$JOB_ID:features" 2>/dev/null || echo "")
        if [ -n "$FEATURES" ]; then
            print_success "Features stored in Redis: $FEATURES"
            if echo "$FEATURES" | grep -q "inferences"; then
                print_success "✓ Test 1 PASSED: inferences feature detected"
                return 0
            else
                print_error "✗ Test 1 FAILED: inferences feature not in stored features"
                return 1
            fi
        else
            print_error "✗ Features not found in Redis for job $JOB_ID"
            return 1
        fi
    else
        print_error "✗ Test 1 FAILED: Could not create job"
        echo "Output: $OUTPUT"
        return 1
    fi
}

# Test 2: Audio with inferences flag
test_audio_inferences() {
    print_info "Test 2: Audio upload with inferences flag (-f)"
    
    # Create test WAV
    create_test_wav $TEMP_DIR/test_audio.wav
    
    # Upload with -f flag
    print_info "Uploading audio with -f flag..."
    OUTPUT=$($CLIENT_BIN -i $TEMP_DIR/test_audio.wav -o $TEMP_DIR/test_audio_results.json -u "$API_URL" -f --timeout 30s 2>&1 || true)
    
    if echo "$OUTPUT" | grep -q "Job created\|Job ID"; then
        print_success "Job created successfully"
        
        JOB_ID=$(extract_job_id "$OUTPUT")
        if [ -z "$JOB_ID" ]; then
            print_error "✗ Could not extract job ID"
            return 1
        fi
        
        print_info "Job ID: $JOB_ID"
        
        # Verify features are stored
        FEATURES=$($REDIS_CLI GET "orchestrator:job:$JOB_ID:features" 2>/dev/null || echo "")
        if [ -n "$FEATURES" ] && echo "$FEATURES" | grep -q "inferences"; then
            print_success "✓ Test 2 PASSED: Audio with inferences feature detected"
            return 0
        else
            print_error "✗ Test 2 FAILED: Features not properly stored"
            return 1
        fi
    else
        print_error "✗ Test 2 FAILED: Could not create job"
        return 1
    fi
}

# Test 3: Image without inferences flag
test_image_without_inferences() {
    print_info "Test 3: Image upload WITHOUT inferences flag (negative test)"
    
    # Create test image
    create_test_png $TEMP_DIR/test_image_no_inf.png
    
    # Upload WITHOUT -f flag
    print_info "Uploading image WITHOUT -f flag..."
    OUTPUT=$($CLIENT_BIN -i $TEMP_DIR/test_image_no_inf.png -o $TEMP_DIR/test_no_inf_results.json -u "$API_URL" --timeout 30s 2>&1 || true)
    
    if echo "$OUTPUT" | grep -q "Job created\|Job ID"; then
        print_success "Job created successfully"
        
        JOB_ID=$(extract_job_id "$OUTPUT")
        if [ -z "$JOB_ID" ]; then
            print_error "✗ Could not extract job ID"
            return 1
        fi
        
        print_info "Job ID: $JOB_ID"
        
        # Verify NO features are stored or inferences is not in features
        FEATURES=$($REDIS_CLI GET "orchestrator:job:$JOB_ID:features" 2>/dev/null || echo "")
        if [ -z "$FEATURES" ] || ! echo "$FEATURES" | grep -q "inferences"; then
            print_success "✓ Test 3 PASSED: No inferences feature (as expected)"
            return 0
        else
            print_error "✗ Test 3 FAILED: Inferences feature should NOT be set"
            return 1
        fi
    else
        print_error "✗ Test 3 FAILED: Could not create job"
        return 1
    fi
}

# Test 4: Verify Redis storage directly
test_redis_features_storage() {
    print_info "Test 4: Redis features storage verification"
    
    # First, upload a file with features
    create_test_png $TEMP_DIR/test_redis.png
    
    OUTPUT=$($CLIENT_BIN -i $TEMP_DIR/test_redis.png -o $TEMP_DIR/test_redis_results.json -u "$API_URL" -f --timeout 30s 2>&1 || true)
    
    JOB_ID=$(extract_job_id "$OUTPUT")
    if [ -z "$JOB_ID" ]; then
        print_error "✗ Test 4 FAILED: Could not extract job ID"
        return 1
    fi
    
    print_info "Job ID: $JOB_ID"
    
    # Check Redis key exists
    REDIS_KEY="orchestrator:job:$JOB_ID:features"
    EXISTS=$($REDIS_CLI EXISTS "$REDIS_KEY" 2>/dev/null || echo "0")
    
    if [ "$EXISTS" = "1" ]; then
        FEATURES=$($REDIS_CLI GET "$REDIS_KEY")
        print_success "Redis key exists with value: $FEATURES"
        
        # Verify it's valid JSON
        if echo "$FEATURES" | grep -q '^\[.*\]$'; then
            print_success "✓ Test 4 PASSED: Features stored as valid JSON array"
            return 0
        else
            print_error "✗ Test 4 FAILED: Features not in JSON array format"
            return 1
        fi
    else
        print_error "✗ Test 4 FAILED: Redis key does not exist: $REDIS_KEY"
        return 1
    fi
}

# Main
main() {
    TEST_NAME="${1:-all}"
    
    print_info "Starting client + inferences integration tests"
    print_info "API URL: $API_URL"
    print_info ""
    
    # Check prerequisites
    if ! command -v $CLIENT_BIN &> /dev/null; then
        print_error "Client binary not found: $CLIENT_BIN"
        print_info "Run: make build-client"
        exit 1
    fi
    
    if ! command -v $REDIS_CLI &> /dev/null; then
        print_error "redis-cli not found"
        exit 1
    fi
    
    # Check API is running
    if ! curl -s "$API_URL/health" > /dev/null 2>&1; then
        print_error "API not responding at $API_URL"
        print_info "Run: make run-orchestrator"
        exit 1
    fi
    print_success "API is responsive"
    print_info ""
    
    # Run tests
    FAILED=0
    PASSED=0
    
    if [ "$TEST_NAME" = "all" ] || [ "$TEST_NAME" = "test_image_inferences" ]; then
        test_image_inferences && ((PASSED++)) || ((FAILED++))
    fi
    
    if [ "$TEST_NAME" = "all" ] || [ "$TEST_NAME" = "test_audio_inferences" ]; then
        test_audio_inferences && ((PASSED++)) || ((FAILED++))
    fi
    
    if [ "$TEST_NAME" = "all" ] || [ "$TEST_NAME" = "test_image_without_inferences" ]; then
        test_image_without_inferences && ((PASSED++)) || ((FAILED++))
    fi
    
    if [ "$TEST_NAME" = "all" ] || [ "$TEST_NAME" = "test_redis_features_storage" ]; then
        test_redis_features_storage && ((PASSED++)) || ((FAILED++))
    fi
    
    # Summary
    echo ""
    print_info "Test Summary:"
    print_success "Passed: $PASSED"
    [ $FAILED -gt 0 ] && print_error "Failed: $FAILED" || print_success "Failed: 0"
    
    if [ $FAILED -eq 0 ]; then
        print_success "All tests passed!"
        return 0
    else
        print_error "Some tests failed"
        return 1
    fi
}

main "$@"
