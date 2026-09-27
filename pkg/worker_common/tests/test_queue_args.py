"""Tests for pkg/worker_common/queue_args (shared declaration table builder)."""

# Standard library

# Third-party
import pytest

# Local
from pkg.worker_common.queue_args import build_queue_arguments, get_queue_max_length


def test_get_queue_max_length_defaults_to_1000(monkeypatch):
    monkeypatch.delenv("QUEUE_MAX_LENGTH", raising=False)
    assert get_queue_max_length() == 1000


def test_get_queue_max_length_reads_env(monkeypatch):
    monkeypatch.setenv("QUEUE_MAX_LENGTH", "42")
    assert get_queue_max_length() == 42


def test_get_queue_max_length_zero_is_allowed(monkeypatch):
    monkeypatch.setenv("QUEUE_MAX_LENGTH", "0")
    assert get_queue_max_length() == 0


def test_get_queue_max_length_invalid_value_falls_back(monkeypatch):
    monkeypatch.setenv("QUEUE_MAX_LENGTH", "not-a-number")
    assert get_queue_max_length() == 1000


def test_build_queue_arguments_default_table(monkeypatch):
    monkeypatch.delenv("QUEUE_MAX_LENGTH", raising=False)
    arguments = build_queue_arguments("entities_text")
    assert arguments == {
        "x-dead-letter-exchange": "document_processor_dlx",
        "x-dead-letter-routing-key": "entities_text_failed",
        "x-max-length": 1000,
        "x-overflow": "reject-publish",
    }


def test_build_queue_arguments_custom_max_length(monkeypatch):
    monkeypatch.setenv("QUEUE_MAX_LENGTH", "42")
    assert build_queue_arguments("audio")["x-max-length"] == 42


def test_build_queue_arguments_zero_omits_limit_args(monkeypatch):
    monkeypatch.setenv("QUEUE_MAX_LENGTH", "0")
    arguments = build_queue_arguments("image")
    assert arguments == {
        "x-dead-letter-exchange": "document_processor_dlx",
        "x-dead-letter-routing-key": "image_failed",
    }
