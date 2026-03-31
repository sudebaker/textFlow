FROM golang:1.22-alpine AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download && go mod verify

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o orchestrator ./cmd/orchestrator

FROM alpine:3.20

RUN apk --no-cache add ca-certificates

WORKDIR /app

COPY --from=builder /app/orchestrator .

# GC tuning: reduce GC frequency to lower CPU overhead from frequent collection.
# GOGC=150 allows heap to grow 50% more before GC triggers (default=100).
# GOMEMLIMIT acts as a soft cap, triggering GC before OOM.
ENV GOGC=150
ENV GOMEMLIMIT=800MiB

EXPOSE 8080

RUN addgroup -g 101 -S appgroup && adduser -u 100 -S appuser -G appgroup && \
    mkdir -p /app/data/uploads /app/data/results && \
    chown -R appuser:appgroup /app
USER appuser

CMD ["./orchestrator"]