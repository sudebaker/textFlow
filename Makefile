# Makefile for textFlow

.PHONY: help run run-orchestrator run-resource run-workers run-all build test lint format clean docker-build docker-push docker-logs setup-models docker-build-offline docker-build-models package package-skip-build deploy install-remote

# Variables
GO_VERSION?=1.22
PYTHON_VERSION?=3.11
COMPOSE_FILE := deploy/docker/docker-compose.yml

# Colors
RED=\033[0;31m
GREEN=\033[0;32m
YELLOW=\033[1;33m
NC=\033[0m

help: ## Show this help message
	@echo -e "\n${GREEN}textFlow - Available commands:${NC}\n"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

***REMOVED***=================
# Development
***REMOVED***=================

run-orchestrator: ## Run orchestrator locally
	@echo -e "${YELLOW}Running orchestrator on port 8080...${NC}"
	cd cmd/orchestrator && go run main.go

run-resource: ## Run resource manager locally
	@echo -e "${YELLOW}Running resource manager on port 9090...${NC}"
	cd cmd/resource-manager && go run main.go

run-embeddings-worker: ## Run embeddings worker locally
	@echo -e "${YELLOW}Running embeddings worker...${NC}"
	cd cmd/embeddings-worker && python embeddings_worker.py

run-entities-worker: ## Run entities worker locally
	@echo -e "${YELLOW}Running entities worker...${NC}"
	cd cmd/entities-worker && python entities_worker.py

run-audio-worker: ## Run audio worker locally
	@echo -e "${YELLOW}Running audio worker...${NC}"
	cd cmd/audio-worker && python worker.py

run-image-worker: ## Run image worker locally
	@echo -e "${YELLOW}Running image worker...${NC}"
	cd cmd/image-worker && python worker.py

run-workers: ## Run all workers locally
	@echo -e "${YELLOW}Running all workers...${NC}"
	@$(MAKE) run-embeddings-worker &
	@$(MAKE) run-entities-worker &
	@$(MAKE) run-audio-worker &
	@$(MAKE) run-image-worker &

run-all: ## Run all services locally (requires docker-compose infrastructure)
	@echo -e "${YELLOW}Running all services...${NC}"
	@$(MAKE) run-orchestrator &
	@$(MAKE) run-resource &
	@$(MAKE) run-workers

***REMOVED***=================
# Docker
***REMOVED***=================

docker-build: ## Build all Docker images
	@echo -e "${YELLOW}Building all Docker images...${NC}"
	docker compose -f $(COMPOSE_FILE) build

docker-push: ## Push all Docker images to registry
	@echo -e "${YELLOW}Pushing all Docker images...${NC}"
	docker compose -f $(COMPOSE_FILE) push

docker-logs: ## Show logs from all services
	@echo -e "${YELLOW}Showing logs from all services...${NC}"
	docker compose -f $(COMPOSE_FILE) logs -f

docker-up: ## Start all services with docker-compose
	@echo -e "${YELLOW}Starting all services...${NC}"
	docker compose -f $(COMPOSE_FILE) up -d

docker-down: ## Stop all services
	@echo -e "${YELLOW}Stopping all services...${NC}"
	docker compose -f $(COMPOSE_FILE) down

***REMOVED***=================
# Testing
***REMOVED***=================

test: ## Run all Go tests
	@echo -e "${YELLOW}Running Go tests...${NC}"
	go test -v ./...

test-coverage: ## Run tests with coverage
	@echo -e "${YELLOW}Running tests with coverage...${NC}"
	go test -v -coverprofile=coverage.out ./...
	go tool cover -html=coverage.out -o coverage.html

test-python: ## Run all Python tests
	@echo -e "${YELLOW}Running Python tests...${NC}"
	pytest cmd/*/tests -v

***REMOVED***=================
# Quality
***REMOVED***=================

lint: ## Run Go linter (requires golangci-lint)
	@echo -e "${YELLOW}Running Go linter...${NC}"
	golangci-lint run ./...

lint-fix: ## Fix linter issues
	@echo -e "${YELLOW}Fixing linter issues...${NC}"
	golangci-lint run ./... --fix

format: ## Format Go and Python code
	@echo -e "${YELLOW}Formatting Go code...${NC}"
	go fmt ./...
	@echo -e "${YELLOW}Formatting Python code...${NC}"
	black cmd/*/ --line-length 120
	isort cmd/*/ --profile black

***REMOVED***=================
# Build
***REMOVED***=================

build: ## Build all binaries
	@echo -e "${YELLOW}Building all binaries...${NC}"
	@mkdir -p bin
	go build -o bin/orchestrator ./cmd/orchestrator
	go build -o bin/resource-manager ./cmd/resource-manager
	cd tools/client && go build -o ../../bin/client .

build-orchestrator: ## Build orchestrator binary
	@echo -e "${YELLOW}Building orchestrator...${NC}"
	@mkdir -p bin
	go build -o bin/orchestrator ./cmd/orchestrator

build-resource-manager: ## Build resource-manager binary
	@echo -e "${YELLOW}Building resource-manager...${NC}"
	@mkdir -p bin
	go build -o bin/resource-manager ./cmd/resource-manager

build-client: ## Build tools/client binary
	@echo -e "${YELLOW}Building client...${NC}"
	@mkdir -p bin
	cd tools/client && go build -o ../../bin/client .

***REMOVED***=================
# Dependencies
***REMOVED***=================

deps: ## Install Go dependencies
	@echo -e "${YELLOW}Installing Go dependencies...${NC}"
	go mod download
	go mod tidy

deps-python: ## Install Python dependencies
	@echo -e "${YELLOW}Installing Python dependencies...${NC}"
	pip install -r cmd/embeddings-worker/requirements.txt
	pip install -r cmd/entities-worker/requirements.txt

***REMOVED***=================
# Clean
***REMOVED***=================

clean: ## Clean build artifacts
	@echo -e "${YELLOW}Cleaning build artifacts...${NC}"
	rm -f bin/*
	rm -f coverage.out coverage.html
	go clean -cache

***REMOVED***=================
# Docker Infrastructure
***REMOVED***=================

infra-up: ## Start infrastructure (RabbitMQ, Redis, Docling)
	@echo -e "${YELLOW}Starting infrastructure...${NC}"
	docker compose -f $(COMPOSE_FILE) up -d rabbitmq redis docling

infra-down: ## Stop infrastructure
	@echo -e "${YELLOW}Stopping infrastructure...${NC}"
	docker compose -f $(COMPOSE_FILE) down rabbitmq redis docling

***REMOVED***=================
# Air-Gapped Deployment
***REMOVED***=================

setup-models: ## Download ML models for air-gapped deployment (~2GB)
	@echo -e "${YELLOW}Downloading ML models (one-time setup)...${NC}"
	@echo "This may take several minutes depending on your internet connection."
	cd deploy/docker && python download-models.py
	@echo -e "${GREEN}✅ Models ready for air-gapped deployment!${NC}"

docker-build-models: ## Build images using local models (100% offline after setup)
	@echo -e "${YELLOW}Building Docker images with local models...${NC}"
	docker compose -f $(COMPOSE_FILE) build
	@echo -e "${GREEN}✅ All images built successfully!${NC}"

docker-build-offline: setup-models docker-build-models
	@echo -e "${GREEN}🎉 Build complete! You can now deploy without internet.${NC}"

***REMOVED***=================
# Air-Gapped Packaging & Deployment
***REMOVED***=================

package: ## Build images and create air-gapped deployment bundle in dist/
	@echo -e "${YELLOW}Packaging deployment bundle...${NC}"
	@bash deploy/package/package.sh

package-skip-build: ## Create deployment bundle without rebuilding images
	@echo -e "${YELLOW}Packaging deployment bundle (skip image build)...${NC}"
	@bash deploy/package/package.sh --skip-build

deploy: ## Transfer bundle to target via rsync (requires HOST=<ip>)
	@test -n "$(HOST)" || (echo "ERROR: HOST is required. Usage: make deploy HOST=10.0.0.5"; exit 1)
	@bash deploy/package/deploy.sh $(HOST)

install-remote: ## Run install.sh on target machine (requires HOST=<ip>)
	@test -n "$(HOST)" || (echo "ERROR: HOST is required. Usage: make install-remote HOST=10.0.0.5"; exit 1)
	@ssh $(HOST) "bash ~/ia-text-deployment/install.sh"

***REMOVED***=================
# Documentation
***REMOVED***=================

docs: ## Generate documentation
	@echo -e "${YELLOW}Generating documentation...${NC}"
	go doc -all ./... > DOCS.md