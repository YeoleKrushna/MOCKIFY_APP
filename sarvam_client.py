"""Centralized Sarvam HTTP client with API-key failover and cooldowns."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests


SARVAM_API_URL = os.environ.get(
    "SARVAM_API_URL",
    "https://api.sarvam.ai/v1/chat/completions",
).strip()

SARVAM_API_TIMEOUT_SECONDS = max(
    10,
    int(os.environ.get("SARVAM_API_TIMEOUT_SECONDS", "90")),
)


def _load_keys() -> list[str]:
    """Load SARVAM_API_KEY1 ... SARVAM_API_KEY5 in order."""
    keys: list[str] = []

    for index in range(1, 6):
        value = os.environ.get(f"SARVAM_API_KEY{index}", "").strip()
        if value and value not in keys:
            keys.append(value)

    if not keys:
        raise ValueError(
            "No Sarvam API keys configured. "
            "Set SARVAM_API_KEY1 ... SARVAM_API_KEY5."
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
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def _retry_after_seconds(response: requests.Response | None) -> float:
    if response is None:
        return 0.0

    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return 0.0

    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _mark_key_failure(key: str, cooldown_seconds: float = 0.0) -> None:
    with _lock:
        state = _states.setdefault(key, _KeyState())
        state.failures += 1
        if cooldown_seconds > 0:
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + cooldown_seconds,
            )


def _mark_key_success(key: str) -> None:
    with _lock:
        state = _states.setdefault(key, _KeyState())
        state.failures = 0
        state.cooldown_until = 0.0


def _select_key(keys: list[str], excluded: set[str]) -> str | None:
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

            _next_index = (index + 1) % len(keys)
            return key

    return None


class SarvamAPIError(Exception):
    """Application-level Sarvam API error."""

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


def _extract_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "Sarvam AI request failed."

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        if payload.get("message"):
            return str(payload["message"])

    return "Sarvam AI request failed."


def call_sarvam_http(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send one Sarvam request with automatic API-key failover.

    Immediate failover occurs for 401, 429, 5xx, timeout/network errors,
    and malformed successful JSON responses.  400/403/422 errors are returned
    immediately because changing API keys will not normally fix a bad request.
    """
    keys = _load_keys()
    attempted: set[str] = set()
    errors: list[str] = []

    for _ in range(len(keys)):
        key = _select_key(keys, attempted)
        if key is None:
            break

        attempted.add(key)

        try:
            response = requests.post(
                SARVAM_API_URL,
                headers={
                    "api-subscription-key": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=SARVAM_API_TIMEOUT_SECONDS,
            )
        except requests.exceptions.Timeout:
            _mark_key_failure(key, cooldown_seconds=2.0)
            errors.append("timeout")
            print(
                f"[SARVAM] key {_key_label(key)} timed out; failing over.",
                flush=True,
            )
            continue
        except requests.exceptions.RequestException:
            _mark_key_failure(key, cooldown_seconds=2.0)
            errors.append("network_error")
            print(
                f"[SARVAM] key {_key_label(key)} request failed; failing over.",
                flush=True,
            )
            continue

        status = response.status_code

        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError:
                _mark_key_failure(key, cooldown_seconds=2.0)
                errors.append("invalid_json")
                print(
                    f"[SARVAM] key {_key_label(key)} returned invalid JSON; failing over.",
                    flush=True,
                )
                continue

            _mark_key_success(key)
            return data

        retry_after = _retry_after_seconds(response)

        if status == 401:
            _mark_key_failure(key, cooldown_seconds=60.0)
            errors.append("401")
            print(
                f"[SARVAM] key {_key_label(key)} unauthorized; failing over.",
                flush=True,
            )
            continue

        if status == 429:
            cooldown = max(1.0, retry_after)
            _mark_key_failure(key, cooldown_seconds=cooldown)
            errors.append("429")
            print(
                f"[SARVAM] key {_key_label(key)} rate-limited; failing over "
                f"(cooldown={cooldown:g}s).",
                flush=True,
            )
            continue

        if 500 <= status <= 599:
            _mark_key_failure(key, cooldown_seconds=2.0)
            errors.append(str(status))
            print(
                f"[SARVAM] key {_key_label(key)} got HTTP {status}; failing over.",
                flush=True,
            )
            continue

        # For a request/configuration error, another key normally cannot help.
        message = _extract_error_message(response)
        raise SarvamAPIError(
            message,
            status_code=status,
            retryable=False,
        )

    if attempted:
        joined = ", ".join(errors) if errors else "all configured keys unavailable"
        all_rate_limited = bool(errors) and all(item == "429" for item in errors)
        raise SarvamAPIError(
            "All available Sarvam API keys failed or are rate-limited "
            f"({joined}). Please try again shortly.",
            status_code=429 if all_rate_limited else 503,
            retryable=False,
        )

    raise SarvamAPIError(
        "All configured Sarvam API keys are temporarily unavailable. Please try again shortly.",
        status_code=503,
        retryable=False,
    )
