from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

_logger = logging.getLogger("selfheal_api")

_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_VALID_PRIMITIVE_VALUES: frozenset[str] = frozenset(
    {
        "<null>",
        "<bool>",
        "<int>",
        "<float>",
        "<str>",
        "<int_str>",
        "<float_str>",
        "<bool_str>",
        "<iso_datetime>",
        "<empty_list>",
        "<empty_object>",
    }
)


def is_safe_key_segment(segment: str) -> bool:
    return bool(_SAFE_KEY_RE.match(segment))


def sanitize_candidate_paths(
    candidates: list[tuple[tuple[str | int, ...], str]],
) -> dict[str, tuple[tuple[str | int, ...], str]]:
    sanitized: dict[str, tuple[tuple[str | int, ...], str]] = {}
    for path, kind in candidates:
        if not path:
            continue
        if any(isinstance(seg, str) and not is_safe_key_segment(seg) for seg in path):
            continue
        if not is_valid_redacted_kind(kind):
            raise ValueError(f"Unsafe non-redacted kind value: {kind!r}")
        dot_key = ".".join(str(seg) for seg in path)
        sanitized[dot_key] = (path, kind)
    return sanitized


def is_valid_redacted_kind(value: str) -> bool:
    if value in _VALID_PRIMITIVE_VALUES:
        return True
    if value.startswith("list[") and value.endswith("]"):
        inner = value[5:-1]
        if not inner:
            return False
        parts = inner.split("|")
        return all(part in _VALID_PRIMITIVE_VALUES for part in parts)
    return False


@runtime_checkable
class SchemaMappingProvider(Protocol):
    def resolve_opaque_mappings(
        self,
        model_name: str,
        missing_fields: dict[str, str],
        extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]: ...


class DeterministicMappingProvider:
    def __init__(
        self,
        mappings: dict[str, tuple[str | int, ...]]
        | Callable[[str, dict[str, str], dict[str, str]], dict[str, tuple[str | int, ...]]],
    ) -> None:
        self._mappings = mappings

    def resolve_opaque_mappings(
        self,
        model_name: str,
        missing_fields: dict[str, str],
        extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        for key, kind_val in extra_observed_kinds.items():
            if not is_valid_redacted_kind(kind_val):
                raise ValueError(f"Unredacted value detected at {key!r}: {kind_val!r}")
        if callable(self._mappings):
            return self._mappings(model_name, missing_fields, extra_observed_kinds)
        result: dict[str, tuple[str | int, ...]] = {}
        for missing_key in missing_fields:
            if missing_key in self._mappings:
                result[missing_key] = self._mappings[missing_key]
        return result


class AnthropicSchemaMappingProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-3-5-haiku-latest",
        timeout_s: float = 5.0,
        endpoint: str = "https://api.anthropic.com/v1/messages",
        max_calls: int = 50,
    ) -> None:
        if max_calls < 0:
            raise ValueError("max_calls must be >= 0")
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model
        self._timeout_s = timeout_s
        self._endpoint = endpoint
        self._max_calls = max_calls
        self._calls_made = 0
        self._lock = threading.Lock()

    def resolve_opaque_mappings(
        self,
        model_name: str,
        missing_fields: dict[str, str],
        extra_observed_kinds: dict[str, str],
    ) -> dict[str, tuple[str | int, ...]]:
        for key, kind_val in extra_observed_kinds.items():
            if not is_valid_redacted_kind(kind_val):
                raise ValueError(f"Unredacted value detected at {key!r}: {kind_val!r}")
        if not self._api_key or not missing_fields or not extra_observed_kinds:
            return {}
        with self._lock:
            if self._calls_made >= self._max_calls:
                _logger.warning(
                    "AnthropicSchemaMappingProvider call cap (%d) reached; failing closed",
                    self._max_calls,
                )
                return {}
            self._calls_made += 1

        prompt = (
            "Map missing Pydantic model fields to candidate extra JSON paths using ONLY "
            "the structural types provided. Return ONLY a JSON object mapping missing_field_key "
            "to extra_path_key.\n"
            f"Model: {model_name}\n"
            f"Missing fields: {json.dumps(missing_fields)}\n"
            f"Extra structural kinds: {json.dumps(extra_observed_kinds)}\n"
        )
        body = json.dumps(
            {
                "model": self._model,
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                raw_resp = resp.read().decode("utf-8")
            payload: Any = json.loads(raw_resp)
            content_list = payload.get("content", [])
            if not isinstance(content_list, list) or not content_list:
                return {}
            text = content_list[0].get("text", "")
            if not isinstance(text, str) or not text.strip():
                return {}
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
                cleaned = re.sub(r"\s*```$", "", cleaned)
            mapping_raw: Any = json.loads(cleaned)
            if not isinstance(mapping_raw, dict):
                return {}
            resolved: dict[str, tuple[str | int, ...]] = {}
            for m_key, e_key in mapping_raw.items():
                if (
                    isinstance(m_key, str)
                    and isinstance(e_key, str)
                    and m_key in missing_fields
                    and e_key in extra_observed_kinds
                ):
                    resolved[m_key] = tuple(
                        int(part) if part.isdigit() else part for part in e_key.split(".")
                    )
            return resolved
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            _logger.warning(
                "AnthropicSchemaMappingProvider fallback failed for model=%s missing=%d: %s",
                model_name,
                len(missing_fields),
                exc,
            )
            return {}
