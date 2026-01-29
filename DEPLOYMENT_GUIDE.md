# IA Text Orchestrator - Deployment Guide

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Environment variables configured (see `.env.example`)

### 1. Configure Environment Variables

```bash
# Copy the example file
cp .env.example .env

# Edit with your credentials
nano .env
```

Required variables:
- `RABBITMQ_USER` - RabbitMQ username
- `RABBITMQ_PASS` - RabbitMQ password
- `GRAFANA_ADMIN_USER` - Grafana admin username
- `GRAFANA_ADMIN_PASSWORD` - Grafana admin password

### 2. Start Services

```bash
cd deploy/docker
docker-compose up -d
```

### 3. Verify Deployment

```bash
# Check all services are running
docker-compose ps

# Test health endpoint
curl http://localhost:8080/health | jq

# Check metrics
curl http://localhost:8080/metrics | grep ia_text

# Access Grafana
open http://localhost:3000
# Login with credentials from .env

# Access Prometheus
curl http://localhost:9091/api/v1/query?query=up
```

## Service Endpoints

| Service | Port | Access |
|---------|------|--------|
| Orchestrator API | 8080 | Public |
| Grafana | 3000 | Public |
| Prometheus | 9091 | Localhost only |
| RabbitMQ Management | - | Internal only |
| Redis | - | Internal only |

## API Usage

### Create a Job

```bash
# With Base64 document
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{
    "document_base64": "SGVsbG8gV29ybGQ="
  }'

# With URL
curl -X POST http://localhost:8080/v1/documents/process \
  -H "Content-Type: application/json" \
  -d '{
    "document_url": "https://example.com/document.pdf"
  }'
```

Response:
```json
{
  "job_id": "1738123456789012345",
  "status": "pending",
  "status_url": "/v1/documents/1738123456789012345"
}
```

### Check Job Status

```bash
curl http://localhost:8080/v1/documents/1738123456789012345 | jq
```

Response:
```json
{
  "job_id": "1738123456789012345",
  "status": "completed",
  "results": {
    "text": "Document content...",
    "metadata": {...},
    "entities": [...],
    "embeddings": [...]
  }
}
```

### Delete Job

```bash
curl -X DELETE http://localhost:8080/v1/documents/1738123456789012345
```

## Monitoring

### Health Checks

```bash
curl http://localhost:8080/health | jq
```

Returns:
```json
{
  "status": "healthy",
  "timestamp": "2026-01-29T12:00:00Z",
  "service": "orchestrator",
  "version": "1.0.0",
  "checks": {
    "redis": {
      "status": "healthy",
      "latency_ms": 2
    },
    "rabbitmq": {
      "status": "healthy",
      "latency_ms": 5,
      "details": {
        "queues": {
          "embeddings": {
            "messages": 0,
            "consumers": 1
          },
          ...
        }
      }
    }
  }
}
```

### Key Metrics

```bash
# Job metrics
curl http://localhost:8080/metrics | grep ia_text_jobs

# Queue depth
curl http://localhost:8080/metrics | grep ia_text_queue_depth

# Runtime metrics
curl http://localhost:8080/metrics | grep -E "ia_text_(goroutine|memory)"

# Latency metrics
curl http://localhost:8080/metrics | grep ia_text_http_latency
```

## Alerting

Prometheus alerts are configured in `deploy/prometheus/alerts.yml`:

- **Critical Alerts**:
  - High job failure rate (>10%)
  - Jobs stuck (not completing)
  - Queue saturation (>500 messages)
  - High Redis/RabbitMQ error rates

- **Warning Alerts**:
  - Slow job processing (>5 minutes)
  - High HTTP error rate (>5%)
  - Slow Redis operations

## Troubleshooting

### Check Logs

```bash
# Orchestrator logs
docker-compose logs -f orchestrator

# Worker logs
docker-compose logs -f embeddings-worker entities-worker metadata-worker

# All logs
docker-compose logs -f
```

### Common Issues

#### 1. Services won't start

```bash
# Check Docker resources
docker stats

# Verify environment variables
docker-compose config

# Check network connectivity
docker network ls
docker network inspect ia-text-datastore
```

#### 2. Jobs failing

```bash
# Check queue status
docker-compose exec rabbitmq rabbitmqctl list_queues

# Check Redis keys
docker-compose exec redis redis-cli KEYS "*"

# Check dead letter queue
docker-compose exec rabbitmq rabbitmqctl list_queues name messages | grep dead
```

#### 3. High memory usage

```bash
# Check Redis memory
docker-compose exec redis redis-cli INFO memory

# Check Redis eviction policy
docker-compose exec redis redis-cli CONFIG GET maxmemory-policy
# Should return: "noeviction"
```

#### 4. Network issues

```bash
# Verify network isolation
docker network inspect ia-text-backend | jq '.[0].Internal'
# Should return: true

# Check port bindings
docker-compose ps
netstat -tlnp | grep -E "(8080|9091|3000)"
```

## Security Checklist

- [ ] Environment variables configured (no defaults)
- [ ] Grafana admin password changed
- [ ] RabbitMQ credentials changed
- [ ] Prometheus bound to localhost only
- [ ] Redis/RabbitMQ not exposed externally
- [ ] Network isolation verified

## Performance Tuning

### Resource Limits

Adjust in `deploy/docker/docker-compose.yml`:

```yaml
services:
  orchestrator:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 1G
```

### Worker Scaling

```bash
# Scale workers horizontally
docker-compose up -d --scale embeddings-worker=3

# Check worker count
docker-compose ps | grep worker
```

### Prefetch Count

Adjust for throughput vs. memory tradeoff:

```yaml
environment:
  - PREFETCH_COUNT=10  # Higher = more throughput, more memory
```

## Backup and Recovery

### Backup Redis Data

```bash
# Create backup
docker-compose exec redis redis-cli SAVE
docker cp ia-text-redis:/data/dump.rdb ./backup/redis-$(date +%Y%m%d).rdb

# Restore from backup
docker cp ./backup/redis-20260129.rdb ia-text-redis:/data/dump.rdb
docker-compose restart redis
```

### Backup Prometheus Data

```bash
# Prometheus data is in volume
docker volume inspect ia-text_prometheus-data

# Create snapshot
docker run --rm -v ia-text_prometheus-data:/data -v $(pwd)/backup:/backup \
  alpine tar czf /backup/prometheus-$(date +%Y%m%d).tar.gz -C /data .
```

## Graceful Shutdown

```bash
# Graceful shutdown (waits for jobs to complete)
docker-compose stop

# Force shutdown
docker-compose down

# Shutdown and remove volumes
docker-compose down -v
```

## Upgrade Procedure

1. **Backup data** (Redis, Prometheus)
2. **Stop services**: `docker-compose stop`
3. **Pull new images**: `docker-compose pull`
4. **Update configuration** if needed
5. **Start services**: `docker-compose up -d`
6. **Verify health**: `curl http://localhost:8080/health`
7. **Check logs**: `docker-compose logs -f`

## Production Checklist

Before deploying to production:

- [ ] All environment variables configured
- [ ] Resource limits appropriate for workload
- [ ] Monitoring and alerting configured
- [ ] Backup strategy in place
- [ ] Log aggregation configured
- [ ] SSL/TLS certificates configured (if public)
- [ ] Firewall rules configured
- [ ] Network isolation verified
- [ ] Security scan completed
- [ ] Load testing completed
- [ ] Disaster recovery plan documented

## Support

For issues or questions:
1. Check logs: `docker-compose logs -f`
2. Check metrics: `curl http://localhost:8080/metrics`
3. Check health: `curl http://localhost:8080/health`
4. Review `IMPLEMENTATION_STATUS.md` for system details
