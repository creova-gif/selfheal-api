from __future__ import annotations

import copy
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from sentinel.detector import DriftKind
from synthesizer.patcher import PatchPlan

_YAML_BOOL_OR_NULL = frozenset(
    {"true", "false", "null", "~", "yes", "no", "on", "off", "y", "n"}
)
_YAML_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_YAML_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.$][A-Za-z0-9_.$-]*$")


class OpenAPIPatchSpec(BaseModel):
    """OpenAPI 3.1.0 schema patch specification generated from a PatchPlan."""

    model_config = ConfigDict(frozen=True)

    model_name: str
    schema_version: str = "3.1.0"
    json_schema: dict[str, Any]
    aliases: dict[str, list[str]]
    coercions: dict[str, str]
    yaml_snippet: str


def _needs_yaml_quoting(value: str) -> bool:
    if value == "" or value != value.strip():
        return True
    if value.lower() in _YAML_BOOL_OR_NULL:
        return True
    if _YAML_NUMBER_RE.match(value):
        return True
    special_chars = set(":#{}[],&*?|-<>=!%@`\"'\\\n\r\t")
    return any(ch in special_chars for ch in value)


def _format_yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if _needs_yaml_quoting(value):
            return json.dumps(value, ensure_ascii=False)
        return value
    return json.dumps(str(value), ensure_ascii=False)


def _format_yaml_key(key: str) -> str:
    if _YAML_SAFE_KEY_RE.match(key) and key.lower() not in _YAML_BOOL_OR_NULL:
        return key
    return json.dumps(key, ensure_ascii=False)


def _render_yaml_lines(value: Any, indent: int = 0) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{{}}"]
        lines: list[str] = []
        for k, v in value.items():
            key_str = _format_yaml_key(str(k))
            if isinstance(v, dict) and v:
                lines.append(f"{pad}{key_str}:")
                lines.extend(_render_yaml_lines(v, indent + 2))
            elif isinstance(v, list) and v:
                lines.append(f"{pad}{key_str}:")
                lines.extend(_render_yaml_lines(v, indent + 2))
            elif isinstance(v, dict):
                lines.append(f"{pad}{key_str}: {{}}")
            elif isinstance(v, list):
                lines.append(f"{pad}{key_str}: []")
            else:
                lines.append(f"{pad}{key_str}: {_format_yaml_scalar(v)}")
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{pad}[]"]
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                item_lines = _render_yaml_lines(item, indent + 2)
                lines.append(f"{pad}- {item_lines[0].lstrip()}")
                lines.extend(item_lines[1:])
            elif isinstance(item, list) and item:
                lines.append(f"{pad}-")
                lines.extend(_render_yaml_lines(item, indent + 2))
            elif isinstance(item, dict):
                lines.append(f"{pad}- {{}}")
            elif isinstance(item, list):
                lines.append(f"{pad}- []")
            else:
                lines.append(f"{pad}- {_format_yaml_scalar(item)}")
        return lines

    return [f"{pad}{_format_yaml_scalar(value)}"]


def _should_record_coercion(
    drift_kind: DriftKind,
    expected_type: str,
    observed_kind: str,
    has_source_path: bool,
) -> bool:
    if drift_kind != DriftKind.MISSING_FIELD:
        return True
    if not has_source_path or not observed_kind or observed_kind == "<missing>":
        return False
    return observed_kind.lower() != f"<{expected_type.lower()}>"


def emit_openapi_schema_patch(
    model_cls: type[BaseModel], plan: PatchPlan
) -> OpenAPIPatchSpec:
    """Emit an OpenAPI 3.1.0 schema patch spec from a Pydantic model and PatchPlan."""
    json_schema: dict[str, Any] = copy.deepcopy(model_cls.model_json_schema())
    aliases: dict[str, list[str]] = {}
    coercions: dict[str, str] = {}

    for action in plan.actions:
        if action.source_path is not None and len(action.source_path) > 0:
            dot_path = ".".join(str(seg) for seg in action.source_path)
            existing = aliases.setdefault(action.target_field, [])
            if dot_path not in existing:
                existing.append(dot_path)

        if _should_record_coercion(
            drift_kind=action.drift_kind,
            expected_type=action.expected_type,
            observed_kind=action.observed_kind,
            has_source_path=action.source_path is not None,
        ):
            coercions[action.target_field] = action.expected_type

    json_schema["x-selfheal-aliases"] = copy.deepcopy(aliases)
    json_schema["x-selfheal-coercions"] = dict(coercions)

    model_name = model_cls.__name__
    yaml_lines = _render_yaml_lines({model_name: json_schema})
    yaml_snippet = "\n".join(yaml_lines) + "\n"

    return OpenAPIPatchSpec(
        model_name=model_name,
        schema_version="3.1.0",
        json_schema=json_schema,
        aliases=aliases,
        coercions=coercions,
        yaml_snippet=yaml_snippet,
    )


def emit_openapi_document(
    specs: list[OpenAPIPatchSpec],
    *,
    title: str = "SelfHealAPI Schemas",
    version: str = "0.2.0",
) -> dict[str, Any]:
    """Assemble a valid OpenAPI 3.1.0 document from a list of OpenAPIPatchSpec."""
    schemas: dict[str, Any] = {
        spec.model_name: copy.deepcopy(spec.json_schema) for spec in specs
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": version,
        },
        "components": {
            "schemas": schemas,
        },
    }
