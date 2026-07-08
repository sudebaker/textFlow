#!/usr/bin/env python3
"""Tests for AdaptiveSemaphore."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from adaptive_semaphore import AdaptiveSemaphore


class TestAdaptiveSemaphoreAcquireRelease:
    def test_acquire_returns_true_when_available(self):
        sem = AdaptiveSemaphore(min_concurrency=2, max_concurrency=8)
        assert sem.acquire(timeout=1) is True
        assert sem.in_flight == 1

    def test_acquire_blocks_until_timeout(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=1)
        assert sem.acquire(timeout=0.1) is True
        # Second acquire should timeout
        assert sem.acquire(timeout=0.1) is False

    def test_release_decrements_in_flight(self):
        sem = AdaptiveSemaphore(min_concurrency=2, max_concurrency=8)
        sem.acquire(timeout=1)
        assert sem.in_flight == 1
        sem.release(latency_ms=100, tokens_per_sec=20.0, is_error=False)
        assert sem.in_flight == 0


class TestAdditiveIncrease:
    def test_cwnd_grows_on_success(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=8, target_tokens_per_sec=10.0)
        assert sem.cwnd == 1
        sem.acquire(timeout=1)
        sem.release(latency_ms=100, tokens_per_sec=20.0, is_error=False)
        assert sem.cwnd == 2

    def test_cwnd_grows_multiple_successes(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=8, target_tokens_per_sec=10.0)
        for _ in range(5):
            sem.acquire(timeout=1)
            sem.release(latency_ms=100, tokens_per_sec=20.0, is_error=False)
        assert sem.cwnd == 6

    def test_cwnd_never_exceeds_max(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=4, target_tokens_per_sec=10.0)
        for _ in range(10):
            sem.acquire(timeout=1)
            sem.release(latency_ms=100, tokens_per_sec=20.0, is_error=False)
        assert sem.cwnd == 4

    def test_no_increase_when_tokens_per_sec_below_target(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=8, target_tokens_per_sec=10.0)
        sem.acquire(timeout=1)
        sem.release(latency_ms=100, tokens_per_sec=5.0, is_error=False)
        assert sem.cwnd == 1


class TestMultiplicativeDecrease:
    def test_cwnd_halves_on_error(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=16, decay_factor=2)
        sem._cwnd = 8
        sem.acquire(timeout=1)
        sem.release(latency_ms=10000, tokens_per_sec=0, is_error=True)
        assert sem.cwnd == 4

    def test_cwnd_never_goes_below_min(self):
        sem = AdaptiveSemaphore(min_concurrency=2, max_concurrency=16, decay_factor=2)
        sem._cwnd = 2
        sem.acquire(timeout=1)
        sem.release(latency_ms=10000, tokens_per_sec=0, is_error=True)
        assert sem.cwnd >= 2

    def test_consecutive_errors_increment(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=16)
        for _ in range(3):
            sem.acquire(timeout=1)
            sem.release(latency_ms=10000, is_error=True)
        stats = sem.get_stats()
        assert stats["total_timeouts"] == 3


class TestCircuitBreaker:
    def test_cooldown_triggers_after_consecutive_errors(self):
        sem = AdaptiveSemaphore(
            min_concurrency=1,
            max_concurrency=16,
            consecutive_errors_for_cooldown=3,
            cooldown_seconds=10,
        )
        for _ in range(3):
            sem.acquire(timeout=1)
            sem.release(latency_ms=5000, is_error=True)
        assert sem.is_in_cooldown is True

    def test_cooldown_resets_consecutive_errors(self):
        sem = AdaptiveSemaphore(
            min_concurrency=1,
            max_concurrency=16,
            consecutive_errors_for_cooldown=3,
            cooldown_seconds=0.1,
        )
        for _ in range(3):
            sem.acquire(timeout=1)
            sem.release(latency_ms=5000, is_error=True)
        assert sem.is_in_cooldown is True
        time.sleep(0.2)
        assert sem.is_in_cooldown is False

    def test_acquire_blocks_during_cooldown(self):
        sem = AdaptiveSemaphore(
            min_concurrency=1,
            max_concurrency=16,
            consecutive_errors_for_cooldown=3,
            cooldown_seconds=0.2,
        )
        for _ in range(3):
            sem.acquire(timeout=1)
            sem.release(latency_ms=5000, is_error=True)
        assert sem.acquire(timeout=0.1) is False


class TestSuccessResetsConsecutiveErrors:
    def test_success_resets_error_counter(self):
        sem = AdaptiveSemaphore(
            min_concurrency=1,
            max_concurrency=16,
            consecutive_errors_for_cooldown=3,
        )
        # 2 errors
        for _ in range(2):
            sem.acquire(timeout=1)
            sem.release(latency_ms=5000, is_error=True)
        # 1 success resets
        sem.acquire(timeout=1)
        sem.release(latency_ms=100, tokens_per_sec=20.0, is_error=False)
        # 2 more errors should NOT trigger cooldown
        for _ in range(2):
            sem.acquire(timeout=1)
            sem.release(latency_ms=5000, is_error=True)
        assert sem.is_in_cooldown is False


class TestGetStats:
    def test_stats_initial(self):
        sem = AdaptiveSemaphore(min_concurrency=2, max_concurrency=8)
        stats = sem.get_stats()
        assert stats["cwnd"] == 2
        assert stats["in_flight"] == 0
        assert stats["total_requests"] == 0
        assert stats["total_timeouts"] == 0
        assert stats["avg_tokens_per_sec"] == 0
        assert stats["is_in_cooldown"] is False

    def test_stats_after_requests(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=8, target_tokens_per_sec=10.0)
        sem.acquire(timeout=1)
        sem.release(latency_ms=100, tokens_per_sec=20.0, is_error=False)
        sem.acquire(timeout=1)
        sem.release(latency_ms=100, tokens_per_sec=0, is_error=True)
        stats = sem.get_stats()
        assert stats["total_requests"] == 2
        assert stats["total_timeouts"] == 1
        assert stats["avg_tokens_per_sec"] > 0


class TestConcurrentAccess:
    def test_thread_safety(self):
        sem = AdaptiveSemaphore(min_concurrency=1, max_concurrency=8, target_tokens_per_sec=10.0)
        errors = []

        def worker():
            try:
                for _ in range(20):
                    if sem.acquire(timeout=1):
                        time.sleep(0.01)
                        sem.release(latency_ms=50, tokens_per_sec=15.0, is_error=False)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        stats = sem.get_stats()
        assert stats["total_requests"] == 80
        assert stats["in_flight"] == 0


import threading
