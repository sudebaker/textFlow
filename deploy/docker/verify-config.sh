#!/bin/bash
# IA Text Orchestrator - Configuration Verification Script
# Verifies air-gapped deployment readiness

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

ERRORS=0
WARNINGS=0

print_header() {
    echo -e "\n${BLUE}══════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}══════════════════════════════════════════════════════${NC}\n"
}

check_ok() {
    echo -e "${GREEN}✅ $1${NC}"
}

check_warn() {
    echo -e "${YELLOW}⚠️  $1${NC}"
    ((WARNINGS++))
}

check_error() {
    echo -e "${RED}❌ $1${NC}"
    ((ERRORS++))
}

# Check if we're in the right directory
if [ ! -f "deploy/docker/docker-compose.yml" ]; then
    echo -e "${RED}Error: Must run from project root (where docker-compose.yml exists)${NC}"
    exit 1
fi

print_header "🔍 IA Text Orchestrator - Configuration Check"

***REMOVED***================
# Check Model Files
***REMOVED***================
print_header "📦 MODEL FILES"

MODELS_REQUIRED=(
    "bge-m3"
    "deberta-v3-small"
    "gliner-small-v2.1"
)

for model in "${MODELS_REQUIRED[@]}"; do
    if [ -d "models/$model" ]; then
        file_count=$(find "models/$model" -type f 2>/dev/null | wc -l)
        if [ "$file_count" -gt 0 ]; then
            check_ok "Model '$model' found ($file_count files)"
        else
            check_error "Model '$model' exists but is empty"
        fi
    else
        check_error "Model '$model' NOT FOUND (required for air-gapped deployment)"
    fi
done

***REMOVED***================
# Check .env File
***REMOVED***================
print_header "⚙️  ENVIRONMENT CONFIGURATION"

if [ -f ".env" ]; then
    check_ok ".env file exists"
    
    # Check for critical variables
    if grep -q "HF_HUB_OFFLINE=1" .env; then
        check_ok "HF_HUB_OFFLINE=1 set"
    else
        check_warn "HF_HUB_OFFLINE not set or incorrect in .env"
    fi
    
    if grep -q "TRANSFORMERS_OFFLINE=1" .env; then
        check_ok "TRANSFORMERS_OFFLINE=1 set"
    else
        check_warn "TRANSFORMERS_OFFLINE not set or incorrect in .env"
    fi
    
    if grep -q "ALLOW_REMOTE_DOWNLOAD=false" .env; then
        check_ok "ALLOW_REMOTE_DOWNLOAD=false set"
    else
        check_warn "ALLOW_REMOTE_DOWNLOAD not disabled"
    fi
else
    check_error ".env file NOT FOUND"
    check_error "Copy from .env.example: cp .env.example .env"
fi

***REMOVED***================
# Check Dockerfile Configuration
***REMOVED***================
print_header "🐳 DOCKERFILE CONFIGURATION"

DOCKERFILES=(
    "cmd/entities-worker/Dockerfile"
    "cmd/embeddings-worker/Dockerfile"
)

for dockerfile in "${DOCKERFILES[@]}"; do
    if [ -f "$dockerfile" ]; then
        if grep -q "HF_HUB_OFFLINE=1" "$dockerfile"; then
            check_ok "$(basename $(dirname $dockerfile)): HF_HUB_OFFLINE=1"
        else
            check_warn "$(basename $(dirname $dockerfile)): Missing HF_HUB_OFFLINE"
        fi
    fi
done

***REMOVED***================
# Check Docker Compose Configuration
***REMOVED***================
print_header "🐳 DOCKER-COMPOSE CONFIGURATION"

if grep -q "HF_HUB_OFFLINE=1" deploy/docker/docker-compose.yml; then
    check_ok "docker-compose.yml: HF_HUB_OFFLINE=1 set"
else
    check_warn "docker-compose.yml: HF_HUB_OFFLINE may not be set"
fi

if grep -q "GLINER_MODEL_PATH=/models/gliner-small-v2.1" deploy/docker/docker-compose.yml; then
    check_ok "docker-compose.yml: GLiNER model path correct"
else
    check_error "docker-compose.yml: GLiNER model path incorrect"
fi

if grep -q "volumes:" deploy/docker/docker-compose.yml && grep -q "../../models:/models" deploy/docker/docker-compose.yml; then
    check_ok "docker-compose.yml: Model volumes mounted"
else
    check_error "docker-compose.yml: Model volumes NOT mounted correctly"
fi

***REMOVED***================
# Check Python Worker Configuration
***REMOVED***================
print_header "🐍 PYTHON WORKER CONFIGURATION"

if grep -q "local_files_only=True" cmd/entities-worker/worker.py; then
    check_ok "entities-worker: local_files_only=True"
else
    check_warn "entities-worker: local_files_only not enforced"
fi

if grep -q "HF_HUB_OFFLINE" cmd/embeddings-worker/worker.py; then
    check_ok "embeddings-worker: Offline mode set"
else
    check_warn "embeddings-worker: Offline mode check"
fi

***REMOVED***================
# Check FastAPI in Requirements
***REMOVED***================
print_header "📋 PYTHON DEPENDENCIES"

REQUIREMENTS_FILES=(
    "cmd/metadata-worker/requirements.txt"
    "cmd/extraction-worker/requirements.txt"
    "cmd/completion-worker/requirements.txt"
    "cmd/entities-worker/requirements.txt"
)

for req_file in "${REQUIREMENTS_FILES[@]}"; do
    if [ -f "$req_file" ]; then
        if grep -q "fastapi" "$req_file"; then
            check_ok "$(basename $(dirname $req_file)): fastapi included"
        else
            check_error "$(basename $(dirname $req_file)): fastapi MISSING"
        fi
    fi
done

***REMOVED***================
# Check Docker Installation
***REMOVED***================
print_header "🔧 SYSTEM REQUIREMENTS"

if command -v docker &> /dev/null; then
    docker_version=$(docker --version)
    check_ok "Docker installed: $docker_version"
else
    check_error "Docker NOT installed"
fi

if command -v docker-compose &> /dev/null || docker compose version &> /dev/null; then
    check_ok "Docker Compose available"
else
    check_error "Docker Compose NOT available"
fi

# Check disk space for models (~4GB)
disk_space=$(du -sh models/ 2>/dev/null | cut -f1)
if [ -n "$disk_space" ]; then
    check_ok "Models directory size: $disk_space"
else
    check_warn "Could not determine models directory size"
fi

***REMOVED***================
# Check Git Status
***REMOVED***================
print_header "📦 GIT STATUS"

if [ -d ".git" ]; then
    check_ok "Git repository initialized"
    
    if git rev-parse --git-dir > /dev/null 2>&1; then
        check_ok ".gitignore properly configured"
    fi
else
    check_warn "Not a git repository"
fi

***REMOVED***================
# Summary
***REMOVED***================
print_header "📊 VERIFICATION SUMMARY"

echo -e "Errors:   ${RED}$ERRORS${NC}"
echo -e "Warnings: ${YELLOW}$WARNINGS${NC}"

if [ $ERRORS -eq 0 ]; then
    echo -e "\n${GREEN}✅ All critical checks passed!${NC}"
    
    if [ $WARNINGS -eq 0 ]; then
        echo -e "${GREEN}✅ No warnings - system is ready for deployment${NC}"
        exit 0
    else
        echo -e "${YELLOW}⚠️  Please review warnings above${NC}"
        exit 0
    fi
else
    echo -e "\n${RED}❌ Please fix errors above before deployment${NC}"
    exit 1
fi
