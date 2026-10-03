"""Tests for vision.config: env-driven VisionSettings (spec extraccion-visual §22)."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dataclasses  # noqa: E402
import pytest  # noqa: E402

from vision.config import SETTINGS, VisionSettings  # noqa: E402


class TestDefaults:
    def test_load_defaults(self):
        s = VisionSettings.load()
        assert s.ocr_enabled is False
        assert s.gate_enabled is True
        assert s.ocr_url == "http://vision-ocr:8080"
        assert s.ocr_timeout == 120.0
        assert s.max_pages_per_document == 50
        assert s.max_seconds_per_document == 900.0
        assert s.max_concurrency == 4
        assert s.image_dpi == 200
        assert s.min_chars_per_page == 100
        assert s.image_placeholder_ratio_max == 0.3
        assert s.garbage_ratio_max == 0.2


class TestFlag:
    def test_flag_parsing(self):
        from vision.config import _flag

        with patch.dict(os.environ, {"X": "1"}):
            assert _flag("X") is True

    @pytest.mark.parametrize("value", ["0", "garbage", "", "false", "no", "TRUE "])
    def test_flag_falsy_values(self, value):
        from vision.config import _flag

        with patch.dict(os.environ, {"X": value}):
            assert _flag("X") is False

    def test_flag_default_false(self):
        # A flag with no default reads as false when unset.
        from vision.config import _flag

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X", None)
            assert _flag("X") is False

    def test_flag_true_values(self):
        from vision.config import _flag

        for value in ("1", "true", "yes"):
            with patch.dict(os.environ, {"X": value}):
                assert _flag("X", "false") is True


class TestEnvOverride:
    def test_env_override_creates_custom_settings(self):
        env = {
            "VISION_OCR_ENABLED": "1",
            "VISION_GATE_ENABLED": "false",
            "VISION_OCR_URL": "http://localhost:9999",
            "VISION_OCR_TIMEOUT": "5",
            "VISION_OCR_MAX_PAGES_PER_DOCUMENT": "3",
            "VISION_OCR_MAX_SECONDS_PER_DOCUMENT": "30",
            "VISION_OCR_MAX_CONCURRENCY": "2",
            "VISION_OCR_IMAGE_DPI": "150",
            "VISION_MIN_CHARS_PER_PAGE": "10",
            "VISION_IMAGE_PLACEHOLDER_RATIO_MAX": "0.5",
            "VISION_GARBAGE_RATIO_MAX": "0.4",
        }
        with patch.dict(os.environ, env):
            s = VisionSettings.load()
        assert s.ocr_enabled is True
        assert s.gate_enabled is False
        assert s.ocr_url == "http://localhost:9999"
        assert s.ocr_timeout == 5.0
        assert s.max_pages_per_document == 3
        assert s.max_seconds_per_document == 30.0
        assert s.max_concurrency == 2
        assert s.image_dpi == 150
        assert s.min_chars_per_page == 10
        assert s.image_placeholder_ratio_max == 0.5
        assert s.garbage_ratio_max == 0.4


class TestFrozen:
    def test_frozen_dataclass(self):
        s = VisionSettings.load()
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.ocr_enabled = True


class TestSingleton:
    def test_settings_is_a_vision_settings(self):
        assert isinstance(SETTINGS, VisionSettings)
