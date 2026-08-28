# GPU example

```bash
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.gpu.yml up -d
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.8.0-base nvidia-smi
# Then same API as examples/basic, with DOCLING_DEVICE=cuda in the override.
```
