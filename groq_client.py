"""Centralized Groq HTTP client with API-key failover and cooldowns."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests


GROQ_API_URL = os.environ.get(
    "GROQ_API_URL",
    "https://api.groq.com/openai/v1/chat/completions",
).strip()

GROQ_API_TIMEOUT_SECONDS = max(
    10,
    int(os.environ.get("GROQ_API_TIMEOUT_SECONDS", "60")),
)


def _load_keys() -> list[str]:
    """Load the five configured Groq API keys in order."""

    keys: list[str] = []

    for index in range(1, 6):
        value = os.environ.get(f"GROQ_API_KEY_{index}", "").strip()

        if value and value not in keys:
            keys.append(value)

    if not keys:
        raise ValueError(
            "No Groq API keys configured. "
            "Set GROQ_API_KEY_1 ... GROQ_API_KEY_5."
        )

    return keys


@dataclass
class _KeyState:
    cooldown_until: float = 0.0
    failures: int = 0


_lock = threading.Lock()
_states: dict[str, _KeyState] = {}
_next_index = 0


def _key_label(key: str) -> str:
    """Return a safe masked representation for logs."""

    if len(key) <= 8:
        return "***"

    return f"{key[:4]}...{key[-4:]}"


def _retry_after_seconds(response: requests.Response | None) -> float:
    """Read Retry-After from the provider response."""

    if response is None:
        return 0.0

    raw = response.headers.get("retry-after", "").strip()

    if not raw:
        return 0.0

    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _mark_key_failure(
    key: str,
    cooldown_seconds: float = 0.0,
) -> None:
    """Record a failed key and optionally put it on cooldown."""

    with _lock:
        state = _states.setdefault(key, _KeyState())

        state.failures += 1

        if cooldown_seconds > 0:
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + cooldown_seconds,
            )


def _mark_key_success(key: str) -> None:
    """Reset failure/cooldown state after a successful request."""

    with _lock:
        state = _states.setdefault(key, _KeyState())

        state.failures = 0
        state.cooldown_until = 0.0


def _select_key(
    keys: list[str],
    excluded: set[str],
) -> str | None:
    """
    Select the next healthy key using round-robin scheduling.

    Keys that are already attempted for the current request or are on
    cooldown are skipped.
    """

    global _next_index

    now = time.monotonic()

    with _lock:
        for offset in range(len(keys)):
            index = (_next_index + offset) % len(keys)
            key = keys[index]

            if key in excluded:
                continue

            state = _states.setdefault(key, _KeyState())

            if state.cooldown_until > now:
                continue

            # Advance the round-robin pointer immediately so successful
            # traffic is distributed across the available keys.
            _next_index = (index + 1) % len(keys)

            return key

    return None


class GroqAPIError(Exception):
    """Application-level Groq API error."""

    def __init__(
        self,
        message: str,
        status_code: int = 502,
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)

        self.message = message
        self.status_code = status_code
        self.retryable = retryable


def call_groq_http(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Send one Groq request with automatic API-key failover.

    Normal traffic:
        Key 1 -> Key 2 -> Key 3 -> Key 4 -> Key 5 -> repeat

    Immediate failover occurs for:
        - 401 Unauthorized
        - 429 Rate Limited
        - 5xx Server Errors
        - request timeouts
        - network/request exceptions

    A failed key is temporarily cooled down so later requests can avoid it.
    """

    keys = _load_keys()

    attempted: set[str] = set()
    errors: list[str] = []

    for _ in range(len(keys)):
        key = _select_key(
            keys,
            attempted,
        )

        if key is None:
            break

        attempted.add(key)

        try:
            response = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=GROQ_API_TIMEOUT_SECONDS,
            )

        except requests.exceptions.Timeout:
            _mark_key_failure(
                key,
                cooldown_seconds=2.0,
            )

            errors.append("timeout")

            print(
                f"[GROQ] key {_key_label(key)} timed out; "
                "failing over.",
                flush=True,
            )

            continue

        except requests.exceptions.RequestException:
            _mark_key_failure(
                key,
                cooldown_seconds=2.0,
            )

            errors.append("network_error")

            print(
                f"[GROQ] key {_key_label(key)} request failed; "
                "failing over.",
                flush=True,
            )

            continue

        status = response.status_code

        # ---------------------------------------------------------
        # SUCCESS
        # ---------------------------------------------------------
        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError:
                # The HTTP request succeeded, but the provider returned
                # malformed JSON. Treat this request as failed so the next
                # configured key can be tried immediately.
                _mark_key_failure(
                    key,
                    cooldown_seconds=2.0,
                )

                errors.append("invalid_json")

                print(
                    f"[GROQ] key {_key_label(key)} returned invalid JSON; "
                    "failing over.",
                    flush=True,
                )

                continue

            _mark_key_success(key)

            return data

        retry_after = _retry_after_seconds(response)

        # ---------------------------------------------------------
        # UNAUTHORIZED
        # ---------------------------------------------------------
        if status == 401:
            _mark_key_failure(
                key,
                cooldown_seconds=60.0,
            )

            errors.append("401")

            print(
                f"[GROQ] key {_key_label(key)} unauthorized; "
                "failing over.",
                flush=True,
            )

            continue

        # ---------------------------------------------------------
        # RATE LIMITED
        # ---------------------------------------------------------
        if status == 429:
            cooldown = max(
                1.0,
                retry_after,
            )

            _mark_key_failure(
                key,
                cooldown_seconds=cooldown,
            )

            errors.append("429")

            print(
                f"[GROQ] key {_key_label(key)} rate-limited; "
                f"failing over (cooldown={cooldown:g}s).",
                flush=True,
            )

            continue

        # ---------------------------------------------------------
        # SERVER / TRANSIENT ERROR
        # ---------------------------------------------------------
        if 500 <= status <= 599:
            _mark_key_failure(
                key,
                cooldown_seconds=2.0,
            )

            errors.append(str(status))

            print(
                f"[GROQ] key {_key_label(key)} got HTTP {status}; "
                "failing over.",
                flush=True,
            )

            continue

        # ---------------------------------------------------------
        # FORBIDDEN
        # ---------------------------------------------------------
        # A 403 is normally a project/model/permission/configuration
        # problem rather than a temporary API-key failure. Do not hide
        # that problem by silently rotating through other keys.
        if status == 403:
            raise GroqAPIError(
                "AI request was forbidden. "
                "Check Groq model/project permissions.",
                status_code=403,
                retryable=False,
            )

        # ---------------------------------------------------------
        # OTHER PROVIDER ERRORS
        # ---------------------------------------------------------
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = {}

        message = "AI service request failed."

        if isinstance(error_payload, dict):
            raw_error = error_payload.get("error")

            if (
                isinstance(raw_error, dict)
                and raw_error.get("message")
            ):
                message = str(raw_error["message"])

            elif isinstance(raw_error, str):
                message = raw_error

            elif error_payload.get("message"):
                message = str(
                    error_payload["message"]
                )

        raise GroqAPIError(
            message,
            status_code=status,
            retryable=False,
        )

    # -------------------------------------------------------------
    # ALL KEYS FAILED
    # -------------------------------------------------------------
    if attempted:
        joined = (
            ", ".join(errors)
            if errors
            else "all configured keys unavailable"
        )

        all_rate_limited = (
            bool(errors)
            and all(item == "429" for item in errors)
        )

        raise GroqAPIError(
            (
                "All available Groq API keys failed or are "
                f"rate-limited ({joined}). Please try again shortly."
            ),
            status_code=429 if all_rate_limited else 503,
            retryable=False,
        )

    # No key could be selected because all configured keys are
    # currently on cooldown.
    raise GroqAPIError(
        "All configured Groq API keys are temporarily unavailable. "
        "Please try again shortly.",
        status_code=503,
        retryable=False,
    )