# Air-gapped example

```bash
# Build machine (online)
make setup-models && make package
bash deploy/package/verify-bundle.sh
make deploy HOST=10.0.0.5
# Target (offline)
ssh 10.0.0.5 "bash ~/…/dist/install.sh"
bash deploy/package/verify-installation.sh  # on target
curl -X POST http://localhost:8080/v1/documents/upload -F "file=@sample.pdf"
```
