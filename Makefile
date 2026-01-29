# Makefile for ia-text-orchestrator

.PHONY: help run run-orchestrator run-resource run-workers run-all build test lint format clean docker-build docker-push docker-logs

# Variables
GO_VERSION?=1.22
PYTHON_VERSION?=3.11

# Colors
RED=\033[0;31m
GREEN=\033[0;32m
YELLOW=\033[1;33m
NC=\033[0m

help: ## Show this help message
	@echo -e "\n${GREEN}ia-text-orchestrator - Available commands:${NC}\n"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

# =============================================================================
# Development
# =============================================================================

run-orchestrator: ## Run orchestrator locally
	@echo -e "${YELLOW}Running orchestrator on port 8080...${NC}"
	cd cmd/orchestrator && go run main.go

run-resource: ## Run resource manager locally
	@echo -e "${YELLOW}Running resource manager on port 9090...${NC}"
	cd cmd/resource-manager && go run main.go

run-embeddings-worker: ## Run embeddings worker locally
	@echo -e "${YELLOW}Running embeddings worker...${NC}"
	cd cmd/embeddings-worker && python worker.py

run-entities-worker: ## Run entities worker locally
	@echo -e "${YELLOW}Running entities worker...${NC}"
	cd cmd/entities-worker && python worker.py

run-workers: ## Run all workers locally
	@echo -e "${YELLOW}Running all workers...${NC}"
	@$(MAKE) run-embeddings-worker &
	@$(MAKE) run-entities-worker &

run-all: ## Run all services locally (requires docker-compose infrastructure)
	@echo -e "${YELLOW}Running all services...${NC}"
	@$(MAKE) run-orchestrator &
	@$(MAKE) run-resource &
	@$(MAKE) run-workers

# =============================================================================
# Docker
# =============================================================================

docker-build: ## Build all Docker images
	@echo -e "${YELLOW}Building all Docker images...${NC}"
	docker compose build

docker-push: ## Push all Docker images to registry
	@echo -e "${YELLOW}Pushing all Docker images...${NC}"
	docker compose push

docker-logs: ## Show logs from all services
	@echo -e "${YELLOW}Showing logs from all services...${NC}"
	docker compose logs -f

docker-up: ## Start all services with docker-compose
	@echo -e "${YELLOW}Starting all services...${NC}"
	docker compose up -d

docker-down: ## Stop all services
	@echo -e "${YELLOW}Stopping all services...${NC}"
	docker compose down

# =============================================================================
# Testing
# =============================================================================

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

# =============================================================================
# Quality
# =============================================================================

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

# =============================================================================
# Build
# =============================================================================

build: ## Build all binaries
	@echo -e "${YELLOW}Building all binaries...${NC}"
	go build -o bin/orchestrator ./cmd/orchestrator
	go build -o bin/resource-manager ./cmd/resource-manager

build-orchestrator: ## Build orchestrator binary
	@echo -e "${YELLOW}Building orchestrator...${NC}"
	go build -o bin/orchestrator ./cmd/orchestrator

build-resource-manager: ## Build resource-manager binary
	@echo -e "${YELLOW}Building resource-manager...${NC}"
	go build -o bin/resource-manager ./cmd/resource-manager

# =============================================================================
# Dependencies
# =============================================================================

deps: ## Install Go dependencies
	@echo -e "${YELLOW}Installing Go dependencies...${NC}"
	go mod download
	go mod tidy

deps-python: ## Install Python dependencies
	@echo -e "${YELLOW}Installing Python dependencies...${NC}"
	pip install -r cmd/embeddings-worker/requirements.txt
	pip install -r cmd/entities-worker/requirements.txt

# =============================================================================
# Clean
# =============================================================================

clean: ## Clean build artifacts
	@echo -e "${YELLOW}Cleaning build artifacts...${NC}"
	rm -f bin/*
	rm -f coverage.out coverage.html
	go clean -cache

# =============================================================================
# Docker Infrastructure
# =============================================================================

infra-up: ## Start infrastructure (RabbitMQ, Redis, Unstructured)
	@echo -e "${YELLOW}Starting infrastructure...${NC}"
	docker compose up -d rabbitmq redis unstructured

infra-down: ## Stop infrastructure
	@echo -e "${YELLOW}Stopping infrastructure...${NC}"
	docker compose down rabbitmq redis unstructured

# =============================================================================
# Documentation
# =============================================================================

docs: ## Generate documentation
	@echo -e "${YELLOW}Generating documentation...${NC}"
	go doc -all ./... > DOCS.md