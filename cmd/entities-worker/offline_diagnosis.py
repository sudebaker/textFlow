#!/usr/bin/env python3
import os
import sys

# Forzar modo offline
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"

from gliner import GLiNER
import transformers
from transformers import AutoConfig, AutoTokenizer

# Ruta del modelo
MODEL_PATH = "/models/gliner-small-v2.1"


def diagnose_model_loading():
    print("🔍 Diagnóstico de carga de modelo GLiNER en modo offline")

    # Verificar archivos necesarios
    required_files = [
        "config.json",
        "pytorch_model.bin",
        "gliner_config.json",
        "tokenizer_config.json",
    ]

    print("\n📋 Archivos necesarios:")
    for file in required_files:
        file_path = os.path.join(MODEL_PATH, file)
        exists = os.path.exists(file_path)
        print(f"  {file}: {'✅ Existe' if exists else '❌ No existe'}")

    # Intentar cargar configuración
    try:
        print("\n🧩 Cargando AutoConfig:")
        config = AutoConfig.from_pretrained(MODEL_PATH, local_files_only=True)
        print("  ✅ Configuración cargada exitosamente")
        print(f"  Tipo de modelo: {config.model_type}")
    except Exception as e:
        print(f"  ❌ Error cargando configuración: {e}")

    # Intentar cargar tokenizer
    try:
        print("\n🔤 Cargando Tokenizer:")
        tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
        print("  ✅ Tokenizer cargado exitosamente")
    except Exception as e:
        print(f"  ❌ Error cargando tokenizer: {e}")

    # Intentar cargar modelo GLiNER
    try:
        print("\n🚀 Cargando modelo GLiNER:")
        model = GLiNER.from_pretrained(MODEL_PATH, local_files_only=True)
        print("  ✅ Modelo GLiNER cargado exitosamente")

        # Prueba de predicción
        print("\n🧪 Prueba de predicción:")
        text = "Cristiano Ronaldo plays for Al Nassr in Saudi Arabia."
        labels = ["person", "team", "country"]

        entities = model.predict_entities(text, labels, threshold=0.01)
        print(f"  Entidades detectadas: {len(entities)}")
        for e in entities:
            print(f"    {e['text']} => {e['label']} (score: {e['score']:.3f})")

    except Exception as e:
        print(f"  ❌ Error cargando modelo GLiNER: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    diagnose_model_loading()
