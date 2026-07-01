#!/usr/bin/env bash
# scripts/test_broker.sh
# Levanta un RabbitMQ temporal en un contenedor Docker para tests de integración del broker Go.
# Uso: RABBITMQ_URL="amqp://test:test@localhost:5673/" bash scripts/test_broker.sh

set -e

CONTAINER_NAME="textflow-test-rabbitmq-$$"
RABBITMQ_USER="${RABBITMQ_USER:-test}"
RABBITMQ_PASS="${RABBITMQ_PASS:-test}"
RABBITMQ_PORT="${RABBITMQ_PORT:-5673}"

cleanup() {
    echo "Stopping test RabbitMQ container..."
    docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Starting RabbitMQ container..."
docker run -d \
    --name "$CONTAINER_NAME" \
    -p "${RABBITMQ_PORT}:5672" \
    -e RABBITMQ_DEFAULT_USER="$RABBITMQ_USER" \
    -e RABBITMQ_DEFAULT_PASS="$RABBITMQ_PASS" \
    rabbitmq:3.13-management >/dev/null

echo "Waiting for RabbitMQ to be ready..."
for i in $(seq 1 30); do
    if docker exec "$CONTAINER_NAME" rabbitmq-diagnostics -q ping >/dev/null 2>&1; then
        echo "RabbitMQ ready on port ${RABBITMQ_PORT}"
        break
    fi
    sleep 1
done

export RABBITMQ_URL="amqp://${RABBITMQ_USER}:${RABBITMQ_PASS}@localhost:${RABBITMQ_PORT}/"

echo "Running broker tests with RABBITMQ_URL=$RABBITMQ_URL"
cd /path/to/textflow

go test -v -race ./internal/broker/...
