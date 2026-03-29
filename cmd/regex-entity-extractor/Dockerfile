# --- STAGE 1: Build ---
FROM golang:1.22-alpine AS builder

WORKDIR /app

# Copy project files for dependency resolution
COPY go.mod go.sum ./

# Download dependencies
RUN go mod download

# Copy source code for regex-entity-extractor
COPY cmd/regex-entity-extractor ./cmd/regex-entity-extractor

# Add missing dependency for regex-entity-extractor
RUN go get github.com/neurosnap/sentences

# Build the application
RUN CGO_ENABLED=0 GOOS=linux go build -o /app/main ./cmd/regex-entity-extractor


# --- STAGE 2: Production ---
FROM alpine:3.20

WORKDIR /app

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# Install curl for healthcheck
RUN apk add --no-cache wget curl

# Copy configuration and compiled binary
COPY --from=builder /app/main .
COPY cmd/regex-entity-extractor/regex_patterns.json .

EXPOSE 8081

RUN addgroup -S appgroup && adduser -S appuser -G appgroup
USER appuser

# Healthcheck: GET /health endpoint
HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=5s \
  CMD curl -sf http://localhost:8081/health || exit 1

ENTRYPOINT ["/app/main"]
