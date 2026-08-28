# Multimodal — image and audio

Image (OCR → chunks):
```bash
curl -X POST http://localhost:8080/v1/documents/upload -F "file=@photo.png"
```
Audio (Whisper transcription → chunks):
```bash
curl -X POST http://localhost:8080/v1/documents/upload -F "file=@clip.mp3"
```
