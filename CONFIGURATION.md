# Configuration Guide

## Environment Variables

Este proyecto utiliza variables de entorno para configuración segura. **NUNCA** comitees credenciales directamente en el código.

### Setup Inicial

1. Copia el archivo de ejemplo:
```bash
cp .env.example .env
```

2. Edita `.env` con tus credenciales reales:
```bash
# RabbitMQ Configuration
RABBITMQ_USER=tu_usuario
RABBITMQ_PASS=tu_contraseña_segura

# Grafana Admin Credentials
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=contraseña_segura_diferente_a_admin
```

3. El archivo `.env` está en `.gitignore` y **NO** se subirá a Git.

### Variables Requeridas

| Variable | Descripción | Default | Requerido |
|----------|-------------|---------|-----------|
| `RABBITMQ_USER` | Usuario de RabbitMQ | guest | No (dev only) |
| `RABBITMQ_PASS` | Password de RabbitMQ | guest | No (dev only) |
| `RABBITMQ_URL` | URL completa de RabbitMQ | - | **Sí** |
| `REDIS_URL` | URL de Redis | redis://localhost:6379 | No |
| `GRAFANA_ADMIN_USER` | Usuario admin de Grafana | admin | No |
| `GRAFANA_ADMIN_PASSWORD` | Password admin de Grafana | admin | No |

### Seguridad

⚠️ **IMPORTANTE**:
- En **desarrollo**: Los defaults (guest/admin) están OK
- En **producción**: DEBES cambiar TODAS las credenciales
- Usa contraseñas fuertes (min 16 caracteres, mixtos)
- Rota credenciales periódicamente (cada 90 días)

### Docker Compose

El archivo `docker-compose.yml` usa estas variables automáticamente:

```bash
# Levantar servicios (lee .env automáticamente)
docker-compose up -d

# O especificar archivo .env manualmente
docker-compose --env-file .env.production up -d
```

### Verificación

Verifica que NO haya credenciales hardcoded:

```bash
# Este comando NO debe retornar resultados
git grep -i "guest:guest"
git grep -i "password.*admin"
```

### Troubleshooting

**Error: "RABBITMQ_URL is required"**
- Asegúrate de que `.env` existe y tiene `RABBITMQ_URL` definida
- O pasa la variable en línea: `RABBITMQ_URL=amqp://user:pass@host:5672/ docker-compose up`

**Servicios no pueden conectar**
- Verifica que las credenciales en `.env` coincidan con las del servicio
- Revisa logs: `docker-compose logs rabbitmq redis`
