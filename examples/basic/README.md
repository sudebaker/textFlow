# Basic example — PDF → chunks + entities + metadata

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@/path/to/file.pdf" | tee job.json
JOB=$(jq -r .job_id job.json)
watch -n 1 "curl -s http://localhost:8080/v1/documents/$JOB | jq .status,.current_step"
curl http://localhost:8080/v1/documents/$JOB/download | gunzip | jq .
```
