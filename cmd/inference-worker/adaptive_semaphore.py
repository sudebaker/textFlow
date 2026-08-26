#!/usr/bin/env python3
"""
Adaptive LLM Concurrency Semaphore

TCP-like AIMD (Additive Increase Multiplicative Decrease) congestion control
for LLM inference concurrency. Controls how many LLM calls can run in parallel,
dynamically adjusting based on call success/error (binary signal, like TCP Reno:
each successful call is an ACK that grows cwnd; each error/timeout halves it).

Features:
- Slow start → additive increase on success → multiplicative decrease on error
- Circuit breaker with cooldown after consecutive errors
- Thread-safe for use in multi-threaded workers
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AdaptiveSemaphore:
    """
    AIMD congestion control for LLM concurrency.

    cwnd (congestion window) starts at min_concurrency.
    On success: additive increase (+1), like TCP ACK.
    On error/timeout: multiplicative decrease (cwnd // decay_factor).
    After N consecutive errors: circuit breaker triggers cooldown.
    """

    min_concurrency: int = 1
    max_concurrency: int = 16
    decay_factor: int = 2
    cooldown_seconds: float = 30.0
    consecutive_errors_for_cooldown: int = 5

    # Internal state
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cwnd: int = field(default=0, init=False, repr=False)
    _in_flight: int = field(default=0, init=False, repr=False)
    _consecutive_errors: int = field(default=0, init=False, repr=False)
    _cooldown_until: float = field(default=0.0, init=False, repr=False)

    # Metrics
    _total_requests: int = field(default=0, init=False, repr=False)
    _total_timeouts: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        self._cwnd = self.min_concurrency

    def acquire(self, timeout: float = 30.0) -> bool:
        """Block until a token is available or timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._cooldown_until > time.monotonic():
                    # In cooldown, wait
                    pass
                elif self._in_flight < self._cwnd:
                    self._in_flight += 1
                    return True

            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)

    def release(self, is_error: bool = False):
        """Release token and update cwnd based on outcome (binary AIMD).

        A successful call (ACK) grows cwnd additively; an error/timeout
        halves it multiplicatively. No throughput metric is needed — the
        LLM timeout acts as the RTO (retransmission timeout) in TCP terms.
        """
        with self._lock:
            self._in_flight -= 1
            self._total_requests += 1

            if is_error:
                self._total_timeouts += 1
                self._consecutive_errors += 1
                # Multiplicative decrease
                old_cwnd = self._cwnd
                self._cwnd = max(self.min_concurrency, self._cwnd // self.decay_factor)
                logger.info(
                    f"cwnd decreased {old_cwnd} → {self._cwnd} "
                    f"(consecutive errors: {self._consecutive_errors})"
                )

                # Circuit breaker
                if self._consecutive_errors >= self.consecutive_errors_for_cooldown:
                    self._cooldown_until = (
                        time.monotonic() + self.cooldown_seconds
                    )
                    self._consecutive_errors = 0
                    logger.warning(
                        f"Circuit breaker triggered, cooling down "
                        f"for {self.cooldown_seconds}s"
                    )
            else:
                self._consecutive_errors = 0
                # Additive increase on success (TCP ACK semantics)
                old_cwnd = self._cwnd
                self._cwnd = min(self.max_concurrency, self._cwnd + 1)
                if old_cwnd != self._cwnd:
                    logger.info(
                        f"cwnd increased {old_cwnd} → {self._cwnd}"
                    )

    @property
    def cwnd(self) -> int:
        with self._lock:
            return self._cwnd

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def is_in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "cwnd": self._cwnd,
                "in_flight": self._in_flight,
                "total_requests": self._total_requests,
                "total_timeouts": self._total_timeouts,
                "is_in_cooldown": self.is_in_cooldown,
            }
