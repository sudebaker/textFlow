import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

# Import from the worker module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from worker import SourceClassifier


class TestSourceClassifier:
    def test_notariado_classification(self):
        text = "ESCRITURA NOTARIAL de compraventa. El notario fedatario certifica..."
        result = SourceClassifier.classify(text)
        assert result is not None
        assert result["document_type"] == "notariado"
        assert result["confidence"] >= 0.5

    def test_catastro_classification(self):
        text = "Datos catastrales. Referencia catastral: 12345678. Plano catastral adjunto."
        result = SourceClassifier.classify(text)
        assert result is not None
        assert result["document_type"] == "catastro"

    def test_bancario_classification(self):
        text = "ESTADO DE CUENTA. Banco XYZ. Extracto bancario del período. Movimientos registrados."
        result = SourceClassifier.classify(text)
        assert result is not None
        assert result["document_type"] == "bancario"

    def test_unknown_document(self):
        text = "This is just a random text with no identifiable document markers."
        result = SourceClassifier.classify(text)
        # Should return None or lowest confidence match
        assert result is None or result["confidence"] < 0.5

    def test_empty_text(self):
        result = SourceClassifier.classify("")
        assert result is None
