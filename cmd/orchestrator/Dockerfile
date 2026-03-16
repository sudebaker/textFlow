FROM golang:1.22.5-alpine3.21 AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download && go mod verify

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o orchestrator ./cmd/orchestrator

FROM alpine:3.19

RUN apk --no-cache add ca-certificates

WORKDIR /app

COPY --from=builder /app/orchestrator .

EXPOSE 8080

CMD ["./orchestrator"]