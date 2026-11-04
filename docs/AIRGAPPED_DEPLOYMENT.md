# Air-Gapped Deployment (canónica: docs/deployment-guide.md)

Esta guía se consolidó en **`docs/deployment-guide.md`** (fuente única;
contiene despliegue air-gapped + verificación + upload/webhook).

Los fixes de staging/post-upstream (assets vendidos, `download_models.py`
con catálogo elegido por el usuario, doctor/smoke) están:
- staging y exclusión de endpoint-resolved: `docs/MODELS.md`
- validación en la máquina target: `make doctor` + `make smoke-test`
  (la instalación del bundle los usa; ver `deploy/package/install.sh`)
