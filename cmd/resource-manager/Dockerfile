FROM golang:1.23-alpine AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download && go mod verify

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o resource-manager ./cmd/resource-manager

# Debian (glibc) runtime so the host's nvidia-smi binary (a glibc ELF) can be
# bind-mounted in for GPU metrics. The Go binary itself is static.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/resource-manager .

EXPOSE 9090

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app
USER appuser

CMD ["./resource-manager"]