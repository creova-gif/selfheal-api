from __future__ import annotations

import json
import re
import time
from enum import StrEnum
from types import UnionType
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from sentinel.llm_fallback import SchemaMappingProvider


class PrimitiveKind(StrEnum):
    NULL = "<null>"
    BOOL = "<bool>"
    INT = "<int>"
    FLOAT = "<float>"
    STR = "<str>"
    INT_STR = "<int_str>"
    FLOAT_STR = "<float_str>"
    BOOL_STR = "<bool_str>"
    ISO_DATETIME = "<iso_datetime>"
    EMPTY_LIST = "<empty_list>"
    EMPTY_OBJECT = "<empty_object>"


type RedactedValue = (
    PrimitiveKind | dict[str, RedactedValue] | list[RedactedValue]
)


class DriftKind(StrEnum):
    MISSING_FIELD = "missing_field"
    EXTRA_FIELD = "extra_field"
    TYPE_MISMATCH = "type_mismatch"
    NULLABILITY_DRIFT = "nullability_drift"
    ENUM_DRIFT = "enum_drift"
    INVALID_JSON = "invalid_json"


class DriftIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: DriftKind
    path: tuple[str | int, ...]
    expected_type: str
    observed_kind: str
    candidate_source_path: tuple[str | int, ...] | None = None


class DriftReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_name: str
    has_drift: bool
    issues: tuple[DriftIssue, ...]
    redacted_snapshot: RedactedValue
    flat_observed_kinds: dict[str, PrimitiveKind]
    elapsed_ms: float


_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d+|\d+\.|\.\d+)(?:[eE][+-]?\d+)?$")
_ISO_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


def classify_scalar(value: Any) -> PrimitiveKind:
    if value is None:
        return PrimitiveKind.NULL
    if isinstance(value, bool):
        return PrimitiveKind.BOOL
    if isinstance(value, int):
        return PrimitiveKind.INT
    if isinstance(value, float):
        return PrimitiveKind.FLOAT
    if isinstance(value, str):
        stripped = value.strip()
        lower = stripped.lower()
        if lower in ("true", "false"):
            return PrimitiveKind.BOOL_STR
        if _INT_RE.match(stripped):
            return PrimitiveKind.INT_STR
        if _FLOAT_RE.match(stripped):
            return PrimitiveKind.FLOAT_STR
        if _ISO_DT_RE.match(stripped):
            return PrimitiveKind.ISO_DATETIME
        return PrimitiveKind.STR
    return PrimitiveKind.STR


def redact_payload(payload: Any) -> RedactedValue:
    if isinstance(payload, dict):
        if not payload:
            return PrimitiveKind.EMPTY_OBJECT
        return {str(k): redact_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        if not payload:
            return PrimitiveKind.EMPTY_LIST
        return [redact_payload(item) for item in payload]
    return classify_scalar(payload)


def _flatten_redacted(
    node: RedactedValue, prefix: str = ""
) -> dict[str, PrimitiveKind]:
    flat: dict[str, PrimitiveKind] = {}
    if isinstance(node, PrimitiveKind):
        if prefix:
            flat[prefix] = node
        return flat
    if isinstance(node, dict):
        for key, child in node.items():
            next_prefix = f"{prefix}.{key}" if prefix else key
            flat.update(_flatten_redacted(child, next_prefix))
        return flat
    if isinstance(node, list):
        for idx, child in enumerate(node):
            next_prefix = f"{prefix}[{idx}]"
            flat.update(_flatten_redacted(child, next_prefix))
        return flat
    return flat


def _lookup_path(payload: Any, path: tuple[str | int, ...]) -> tuple[bool, Any]:
    curr = payload
    for segment in path:
        if isinstance(segment, int):
            if isinstance(curr, list) and 0 <= segment < len(curr):
                curr = curr[segment]
            else:
                return False, None
        elif isinstance(curr, dict) and segment in curr:
            curr = curr[segment]
        else:
            return False, None
    return True, curr


def _describe_annotation(annotation: Any) -> str:
    if annotation is None or annotation is type(None):
        return "None"
    origin = get_origin(annotation)
    if origin is Literal:
        lit_args = ", ".join(repr(a) for a in get_args(annotation))
        return f"Literal[{lit_args}]"
    if origin in (Union, UnionType):
        parts = [_describe_annotation(arg) for arg in get_args(annotation)]
        return " | ".join(parts)
    if origin is list:
        type_args = get_args(annotation)
        inner = _describe_annotation(type_args[0]) if type_args else "Any"
        return f"list[{inner}]"
    if origin is dict:
        type_args = get_args(annotation)
        if len(type_args) == 2:
            return (
                f"dict[{_describe_annotation(type_args[0])}, "
                f"{_describe_annotation(type_args[1])}]"
            )
        return "dict[Any, Any]"
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _resolve_expected_type(
    model_cls: type[BaseModel], path: tuple[str | int, ...]
) -> str:
    curr_model: type[BaseModel] | None = model_cls
    last_annotation: Any = None
    for segment in path:
        if isinstance(segment, int):
            continue
        if curr_model is not None and segment in curr_model.model_fields:
            field_info = curr_model.model_fields[segment]
            last_annotation = field_info.annotation
            curr_model = None
            if isinstance(last_annotation, type) and issubclass(
                last_annotation, BaseModel
            ):
                curr_model = last_annotation
            else:
                origin = get_origin(last_annotation)
                if origin in (Union, UnionType):
                    for arg in get_args(last_annotation):
                        if isinstance(arg, type) and issubclass(arg, BaseModel):
                            curr_model = arg
                            break
                elif origin is list:
                    args = get_args(last_annotation)
                    if (
                        args
                        and isinstance(args[0], type)
                        and issubclass(args[0], BaseModel)
                    ):
                        curr_model = args[0]
        else:
            break
    if last_annotation is not None:
        return _describe_annotation(last_annotation)
    return "unknown"


def _classify_list_kinds(items: list[Any], max_items: int = 256) -> str:
    if not items:
        return str(PrimitiveKind.EMPTY_LIST)
    kinds = sorted({str(classify_scalar(item)) for item in items[:max_items]})
    return f"list[{'|'.join(kinds)}]"


def _collect_extra_paths(
    model_cls: type[BaseModel],
    payload: Any,
    prefix: tuple[str | int, ...] = (),
) -> list[tuple[tuple[str | int, ...], str]]:
    extras: list[tuple[tuple[str | int, ...], str]] = []
    if not isinstance(payload, dict):
        return extras
    known_fields = model_cls.model_fields
    for key, val in payload.items():
        current_path = (*prefix, str(key))
        if key not in known_fields:
            if isinstance(val, dict):
                for sub_key, sub_val in val.items():
                    sub_path = (*current_path, str(sub_key))
                    if isinstance(sub_val, dict):
                        for leaf_key, leaf_val in sub_val.items():
                            leaf_kind = (
                                _classify_list_kinds(leaf_val)
                                if isinstance(leaf_val, list)
                                else str(classify_scalar(leaf_val))
                            )
                            extras.append(((*sub_path, str(leaf_key)), leaf_kind))
                    else:
                        sub_kind = (
                            _classify_list_kinds(sub_val)
                            if isinstance(sub_val, list)
                            else str(classify_scalar(sub_val))
                        )
                        extras.append((sub_path, sub_kind))
            elif isinstance(val, list):
                extras.append((current_path, _classify_list_kinds(val)))
            else:
                extras.append((current_path, str(classify_scalar(val))))
        else:
            annotation = known_fields[key].annotation
            nested_model = _extract_model_type(annotation)
            if nested_model is not None and isinstance(val, dict):
                extras.extend(_collect_extra_paths(nested_model, val, current_path))
            elif nested_model is not None and isinstance(val, list):
                for idx, item in enumerate(val):
                    if isinstance(item, dict):
                        extras.extend(
                            _collect_extra_paths(
                                nested_model, item, (*current_path, idx)
                            )
                        )
    return extras


def _extract_model_type(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin in (Union, UnionType, list):
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _tokenize_key(name: str) -> list[str]:
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    return [tok for tok in re.split(r"[^a-z0-9]+", s1) if tok]


_STOP_SUFFIXES = {
    "id",
    "ms",
    "sec",
    "secs",
    "seconds",
    "cents",
    "usd",
    "eur",
    "str",
    "num",
    "val",
    "value",
    "code",
    "text",
    "iso",
    "ts",
    "at",
    "on",
    "flag",
    "is",
    "has",
}

_SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"amount", "amt", "total", "price", "cost", "charge"}),
    frozenset({"currency", "curr", "ccy", "iso_currency"}),
    frozenset({"customer", "cust", "client", "user", "buyer", "contact"}),
    frozenset({"email", "mail", "email_address", "e_mail"}),
    frozenset({"created", "created_at", "creation", "timestamp", "created_ts", "issued"}),
    frozenset({"status", "state", "stage", "phase", "lifecycle"}),
    frozenset({"temp", "temperature", "degrees", "celsius", "fahrenheit"}),
    frozenset({"humidity", "relative_humidity", "moisture", "rh"}),
    frozenset({"wind", "wind_speed", "gust", "velocity", "kph", "mph"}),
    frozenset({"company", "org", "organization", "account", "firm"}),
    frozenset({"phone", "mobile", "telephone", "tel", "cell"}),
    frozenset({"active", "enabled", "is_active", "live", "verified"}),
    frozenset({"description", "desc", "summary", "details", "note"}),
    frozenset({"id", "identifier", "uid", "uuid", "key"}),
    frozenset({"deal", "opportunity", "pipeline"}),
    frozenset({"revenue", "arr", "mrr", "annual_revenue"}),
)


def _are_tokens_synonymous(t1: str, t2: str) -> bool:
    if t1 == t2:
        return True
    if (len(t1) >= 3 and t2.startswith(t1)) or (len(t2) >= 3 and t1.startswith(t2)):
        return True
    for group in _SYNONYM_GROUPS:
        if t1 in group and t2 in group:
            return True
    return False


def _type_compatible(expected_type: str, observed_kind: str) -> bool:
    exp = expected_type.lower()
    if observed_kind == PrimitiveKind.NULL:
        return "none" in exp
    if "list" in exp:
        return observed_kind == PrimitiveKind.EMPTY_LIST or observed_kind.startswith(
            "list["
        )
    if "int" in exp:
        return observed_kind in (
            PrimitiveKind.INT,
            PrimitiveKind.INT_STR,
            PrimitiveKind.FLOAT,
        )
    if "float" in exp:
        return observed_kind in (
            PrimitiveKind.FLOAT,
            PrimitiveKind.INT,
            PrimitiveKind.FLOAT_STR,
            PrimitiveKind.INT_STR,
        )
    if "bool" in exp:
        return observed_kind in (
            PrimitiveKind.BOOL,
            PrimitiveKind.BOOL_STR,
            PrimitiveKind.INT,
        )
    if "str" in exp or "literal" in exp:
        return observed_kind in (
            PrimitiveKind.STR,
            PrimitiveKind.ISO_DATETIME,
            PrimitiveKind.INT_STR,
            PrimitiveKind.FLOAT_STR,
            PrimitiveKind.BOOL_STR,
        )
    return True


def _score_candidate_path(
    missing_path: tuple[str | int, ...],
    expected_type: str,
    candidate_path: tuple[str | int, ...],
    observed_kind: str,
) -> float:
    if not _type_compatible(expected_type, observed_kind):
        return -1.0

    missing_key = str(missing_path[-1])
    missing_tokens = _tokenize_key(missing_key)
    cand_all_tokens: list[str] = []
    for seg in candidate_path:
        if isinstance(seg, str):
            cand_all_tokens.extend(_tokenize_key(seg))
    cand_leaf_tokens = _tokenize_key(str(candidate_path[-1]))

    if not missing_tokens or not cand_all_tokens:
        return 0.0

    matched_missing = 0
    for m_tok in missing_tokens:
        if any(_are_tokens_synonymous(m_tok, c_tok) for c_tok in cand_all_tokens):
            matched_missing += 1

    leaf_matches = sum(
        1
        for m_tok in missing_tokens
        if any(_are_tokens_synonymous(m_tok, c_tok) for c_tok in cand_leaf_tokens)
    )

    score = (matched_missing / len(missing_tokens)) * 0.7 + (
        leaf_matches / max(len(missing_tokens), len(cand_leaf_tokens))
    ) * 0.3

    missing_core = [t for t in missing_tokens if t not in _STOP_SUFFIXES]
    cand_core = [t for t in cand_all_tokens if t not in _STOP_SUFFIXES]
    if missing_core and cand_core and missing_core == cand_core:
        score += 0.35

    if missing_path[:-1] == candidate_path[:-1]:
        score += 0.1

    return score


def _classify_validation_error(err: Any, observed_val: Any) -> DriftKind:
    err_type = str(err.get("type", ""))
    if err_type == "missing":
        return DriftKind.MISSING_FIELD
    if err_type == "extra_forbidden":
        return DriftKind.EXTRA_FIELD
    null_err_markers = (
        "none",
        "string_type",
        "int_type",
        "float_type",
        "bool_type",
        "literal_error",
        "list_type",
        "dict_type",
    )
    if observed_val is None and any(m in err_type for m in null_err_markers):
        return DriftKind.NULLABILITY_DRIFT
    if "literal_error" in err_type or "enum" in err_type:
        return DriftKind.ENUM_DRIFT
    return DriftKind.TYPE_MISMATCH


def detect_drift(
    expected_model: type[BaseModel],
    raw_json: str | bytes | dict[str, Any],
    mapping_provider: SchemaMappingProvider | None = None,
) -> DriftReport:
    start_ns = time.perf_counter_ns()

    if isinstance(raw_json, (str, bytes)):
        try:
            parsed: Any = json.loads(raw_json)
        except ValueError:
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            return DriftReport(
                model_name=expected_model.__name__,
                has_drift=True,
                issues=(
                    DriftIssue(
                        kind=DriftKind.INVALID_JSON,
                        path=(),
                        expected_type="json_object",
                        observed_kind=PrimitiveKind.STR,
                    ),
                ),
                redacted_snapshot=PrimitiveKind.STR,
                flat_observed_kinds={},
                elapsed_ms=elapsed_ms,
            )
    else:
        parsed = raw_json

    redacted = redact_payload(parsed)
    flat_kinds = _flatten_redacted(redacted)
    extra_candidates = _collect_extra_paths(expected_model, parsed)

    raw_issues: list[DriftIssue] = []
    seen_paths: set[tuple[DriftKind, tuple[str | int, ...]]] = set()

    try:
        expected_model.model_validate(parsed, strict=True)
    except ValidationError as exc:
        for err in exc.errors():
            loc_tuple: tuple[str | int, ...] = tuple(
                int(x) if isinstance(x, int) else str(x) for x in err.get("loc", ())
            )
            while len(loc_tuple) > 1 and isinstance(loc_tuple[-1], int):
                loc_tuple = loc_tuple[:-1]
            field_loc: tuple[str | int, ...] = tuple(
                0 if isinstance(seg, int) else seg for seg in loc_tuple
            )
            found, observed_val = _lookup_path(parsed, field_loc)
            kind = _classify_validation_error(err, observed_val if found else None)
            expected_type = _resolve_expected_type(expected_model, field_loc)
            if not found:
                obs_kind_str = "missing"
            elif isinstance(observed_val, list):
                obs_kind_str = _classify_list_kinds(observed_val)
            elif isinstance(observed_val, dict):
                obs_kind_str = "object"
            else:
                obs_kind_str = str(classify_scalar(observed_val))

            key = (kind, field_loc)
            if key not in seen_paths:
                seen_paths.add(key)
                raw_issues.append(
                    DriftIssue(
                        kind=kind,
                        path=field_loc,
                        expected_type=expected_type,
                        observed_kind=obs_kind_str,
                    )
                )

    used_candidates: set[tuple[str | int, ...]] = set()
    enriched_issues: list[DriftIssue] = []
    unmatched_missing_indices: list[int] = []

    for issue in raw_issues:
        if issue.kind == DriftKind.MISSING_FIELD and extra_candidates:
            best_path: tuple[str | int, ...] | None = None
            best_score = 0.25
            for cand_path, cand_kind in extra_candidates:
                if cand_path in used_candidates:
                    continue
                score = _score_candidate_path(
                    issue.path, issue.expected_type, cand_path, cand_kind
                )
                if score > best_score:
                    best_score = score
                    best_path = cand_path
            if best_path is not None:
                used_candidates.add(best_path)
                enriched_issues.append(
                    DriftIssue(
                        kind=issue.kind,
                        path=issue.path,
                        expected_type=issue.expected_type,
                        observed_kind=issue.observed_kind,
                        candidate_source_path=best_path,
                    )
                )
                continue
            unmatched_missing_indices.append(len(enriched_issues))
        enriched_issues.append(issue)

    if (
        mapping_provider is not None
        and unmatched_missing_indices
        and extra_candidates
    ):
        from sentinel.llm_fallback import sanitize_candidate_paths

        unused_cands = [
            (p, k) for p, k in extra_candidates if p not in used_candidates
        ]
        sanitized_map = sanitize_candidate_paths(unused_cands)
        if sanitized_map:
            missing_req: dict[str, str] = {}
            idx_by_key: dict[str, int] = {}
            for idx in unmatched_missing_indices:
                m_issue = enriched_issues[idx]
                dot_key = ".".join(str(seg) for seg in m_issue.path)
                missing_req[dot_key] = m_issue.expected_type
                idx_by_key[dot_key] = idx
            extra_kinds = {k: v[1] for k, v in sanitized_map.items()}
            llm_matches = mapping_provider.resolve_opaque_mappings(
                expected_model.__name__, missing_req, extra_kinds
            )
            valid_paths_by_tuple = {p: k for p, k in unused_cands}
            for dot_key, cand_tuple in llm_matches.items():
                if dot_key not in idx_by_key or cand_tuple in used_candidates:
                    continue
                if cand_tuple not in valid_paths_by_tuple:
                    continue
                target_idx = idx_by_key[dot_key]
                target_issue = enriched_issues[target_idx]
                if not _type_compatible(
                    target_issue.expected_type, valid_paths_by_tuple[cand_tuple]
                ):
                    continue
                used_candidates.add(cand_tuple)
                enriched_issues[target_idx] = DriftIssue(
                    kind=target_issue.kind,
                    path=target_issue.path,
                    expected_type=target_issue.expected_type,
                    observed_kind=target_issue.observed_kind,
                    candidate_source_path=cand_tuple,
                )

    for cand_path, cand_kind in extra_candidates:
        top_segment = cand_path[:1]
        if (DriftKind.EXTRA_FIELD, cand_path) not in seen_paths and (
            DriftKind.EXTRA_FIELD,
            top_segment,
        ) not in seen_paths:
            seen_paths.add((DriftKind.EXTRA_FIELD, cand_path))
            enriched_issues.append(
                DriftIssue(
                    kind=DriftKind.EXTRA_FIELD,
                    path=cand_path,
                    expected_type="unexpected",
                    observed_kind=cand_kind,
                )
            )

    elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
    return DriftReport(
        model_name=expected_model.__name__,
        has_drift=len(enriched_issues) > 0,
        issues=tuple(enriched_issues),
        redacted_snapshot=redacted,
        flat_observed_kinds=flat_kinds,
        elapsed_ms=elapsed_ms,
    )
