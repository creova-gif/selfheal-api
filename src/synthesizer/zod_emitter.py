from __future__ import annotations

from collections import defaultdict

from sentinel.detector import DriftKind
from synthesizer.patcher import PatchAction, PatchPlan


def _ts_default_for_type(expected_type: str) -> str:
    exp = expected_type.lower()
    if "none" in exp:
        return "null"
    if exp.startswith("list"):
        return "[]"
    if exp.startswith("dict"):
        return "{}"
    if exp == "bool":
        return "false"
    if exp == "int" or exp == "float":
        return "0"
    if exp == "str":
        return "''"
    return "null"


def _emit_ts_action_lines(action: PatchAction, action_idx: int = 0) -> list[str]:
    f = action.target_field
    exp = action.expected_type.lower()
    lines: list[str] = []
    v0 = f"_src_{f}_{action_idx}_0"
    v1 = f"_src_{f}_{action_idx}_1"

    if action.source_path is not None:
        if len(action.source_path) == 1:
            src = str(action.source_path[0])
            lines.append(
                f"  if (!({f!r} in normalized) && ({src!r} in normalized)) {{"
            )
            lines.append(f"    normalized[{f!r}] = normalized[{src!r}];")
            lines.append("  }")
        elif len(action.source_path) == 2:
            p0, p1 = str(action.source_path[0]), str(action.source_path[1])
            lines.append(
                f"  const {v0} = normalized[{p0!r}];"
            )
            lines.append(
                f"  if (!({f!r} in normalized) && typeof {v0} === 'object' "
                f"&& {v0} !== null && {p1!r} in {v0}) {{"
            )
            lines.append(
                f"    normalized[{f!r}] = ({v0} as Record<string, unknown>)[{p1!r}];"
            )
            lines.append("  }")
        elif len(action.source_path) == 3:
            p0, p1, p2 = (
                str(action.source_path[0]),
                str(action.source_path[1]),
                str(action.source_path[2]),
            )
            lines.append(f"  const {v0} = normalized[{p0!r}];")
            lines.append(
                f"  if (!({f!r} in normalized) && typeof {v0} === 'object' "
                f"&& {v0} !== null && {p1!r} in {v0}) {{"
            )
            lines.append(
                f"    const {v1} = ({v0} as Record<string, unknown>)[{p1!r}];"
            )
            lines.append(
                f"    if (typeof {v1} === 'object' && {v1} !== null "
                f"&& {p2!r} in {v1}) {{"
            )
            lines.append(
                f"      normalized[{f!r}] = ({v1} as Record<string, unknown>)[{p2!r}];"
            )
            lines.append("    }")
            lines.append("  }")

    if action.drift_kind == DriftKind.MISSING_FIELD and action.source_path is None:
        default_expr = _ts_default_for_type(exp)
        lines.append(f"  if (!({f!r} in normalized)) {{")
        lines.append(f"    normalized[{f!r}] = {default_expr};")
        lines.append("  }")

    if action.drift_kind == DriftKind.NULLABILITY_DRIFT and "none" not in exp:
        default_expr = _ts_default_for_type(exp)
        lines.append(f"  if (normalized[{f!r}] === null) {{")
        lines.append(f"    normalized[{f!r}] = {default_expr};")
        lines.append("  }")

    if "list[" in exp:
        lines.append(
            f"  if ({f!r} in normalized && !Array.isArray(normalized[{f!r}])) {{"
        )
        lines.append(
            f"    normalized[{f!r}] = normalized[{f!r}] === null ? [] : [normalized[{f!r}]];"
        )
        lines.append("  }")
        if "list[str]" in exp:
            lines.append(f"  if (Array.isArray(normalized[{f!r}])) {{")
            lines.append(
                f"    normalized[{f!r}] = (normalized[{f!r}] as unknown[]).map((v) => String(v));"
            )
            lines.append("  }")
        elif "list[int]" in exp:
            lines.append(f"  if (Array.isArray(normalized[{f!r}])) {{")
            lines.append(
                f"    normalized[{f!r}] = (normalized[{f!r}] as unknown[])"
                f".filter((v) => typeof v !== 'boolean' && !Number.isNaN(Number(v)))"
                f".map((v) => Math.trunc(Number(v)));"
            )
            lines.append("  }")
        elif "list[float]" in exp:
            lines.append(f"  if (Array.isArray(normalized[{f!r}])) {{")
            lines.append(
                f"    normalized[{f!r}] = (normalized[{f!r}] as unknown[])"
                f".filter((v) => typeof v !== 'boolean' && !Number.isNaN(Number(v)))"
                f".map((v) => Number(v));"
            )
            lines.append("  }")
        elif "list[bool]" in exp:
            lines.append(f"  if (Array.isArray(normalized[{f!r}])) {{")
            lines.append(
                f"    normalized[{f!r}] = (normalized[{f!r}] as unknown[]).map((v) => Boolean(v));"
            )
            lines.append("  }")
    elif "int" in exp and "dict" not in exp:
        lines.append(
            f"  if ({f!r} in normalized && (typeof normalized[{f!r}] === 'string' "
            f"|| typeof normalized[{f!r}] === 'number')) {{"
        )
        lines.append(f"    const _num = Number(normalized[{f!r}]);")
        lines.append("    if (!Number.isNaN(_num)) {")
        lines.append(f"      normalized[{f!r}] = Math.trunc(_num);")
        lines.append("    }")
        lines.append("  }")
    elif "float" in exp and "dict" not in exp:
        lines.append(
            f"  if ({f!r} in normalized && (typeof normalized[{f!r}] === 'string' "
            f"|| typeof normalized[{f!r}] === 'number')) {{"
        )
        lines.append(f"    const _num = Number(normalized[{f!r}]);")
        lines.append("    if (!Number.isNaN(_num)) {")
        lines.append(f"      normalized[{f!r}] = _num;")
        lines.append("    }")
        lines.append("  }")
    elif "bool" in exp:
        lines.append(f"  if (typeof normalized[{f!r}] === 'string') {{")
        lines.append(
            f"    const _s = (normalized[{f!r}] as string).trim().toLowerCase();"
        )
        lines.append(
            f"    if (_s === 'true' || _s === '1' || _s === 'yes') normalized[{f!r}] = true;"
        )
        lines.append(
            f"    else if (_s === 'false' || _s === '0' || _s === 'no') normalized[{f!r}] = false;"
        )
        lines.append(f"  }} else if (typeof normalized[{f!r}] === 'number') {{")
        lines.append(f"    normalized[{f!r}] = Boolean(normalized[{f!r}]);")
        lines.append("  }")
    elif exp == "str" or exp == "str | none":
        lines.append(
            f"  if ({f!r} in normalized && normalized[{f!r}] !== null "
            f"&& typeof normalized[{f!r}] !== 'string') {{"
        )
        lines.append(f"    if (typeof normalized[{f!r}] === 'object') {{")
        lines.append(
            f"      const _obj = normalized[{f!r}] as Record<string, unknown>;"
        )
        lines.append(
            "      for (const _k of ['id', 'code', 'value', 'name', 'email', 'address']) {"
        )
        lines.append("        if (typeof _obj[_k] === 'string') {")
        lines.append(f"          normalized[{f!r}] = _obj[_k];")
        lines.append("          break;")
        lines.append("        }")
        lines.append("      }")
        lines.append("    } else {")
        lines.append(f"      normalized[{f!r}] = String(normalized[{f!r}]);")
        lines.append("    }")
        lines.append("  }")

    return lines


def synthesize_zod_preprocessor(plan: PatchPlan) -> str:
    grouped: dict[str, list[PatchAction]] = defaultdict(list)
    for action in plan.actions:
        grouped[action.target_model].append(action)

    ordered_models = [m for m in grouped if m != plan.model_name]
    if plan.model_name in grouped or not ordered_models:
        ordered_models.append(plan.model_name)

    sections: list[str] = ["import { z } from 'zod';", ""]
    for model_name in ordered_models:
        actions = grouped.get(model_name, [])
        fn_name = f"normalize{model_name}Drift"
        sections.append(f"export function {fn_name}(data: unknown): unknown {{")
        sections.append("  if (typeof data !== 'object' || data === null || Array.isArray(data)) {")
        sections.append("    return data;")
        sections.append("  }")
        sections.append(
            "  const normalized: Record<string, unknown> = "
            "{ ...(data as Record<string, unknown>) };"
        )
        for idx, action in enumerate(actions):
            sections.extend(_emit_ts_action_lines(action, action_idx=idx))
        if model_name == plan.model_name:
            child_links: dict[str, str] = {}
            for other_model, other_actions in grouped.items():
                if other_model == plan.model_name:
                    continue
                for act in other_actions:
                    parent_path = act.nested_path or act.target_parent_path
                    if parent_path:
                        top_field = str(parent_path[0])
                        child_links.setdefault(top_field, f"normalize{other_model}Drift")
            for top_field, child_fn in child_links.items():
                sections.append(
                    f"  if (Array.isArray(normalized[{top_field!r}])) {{"
                )
                sections.append(
                    f"    normalized[{top_field!r}] = "
                    f"(normalized[{top_field!r}] as unknown[]).map((item) => {child_fn}(item));"
                )
                sections.append(
                    f"  }} else if (typeof normalized[{top_field!r}] === 'object' "
                    f"&& normalized[{top_field!r}] !== null) {{"
                )
                sections.append(
                    f"    normalized[{top_field!r}] = {child_fn}(normalized[{top_field!r}]);"
                )
                sections.append("  }")
        sections.append("  return normalized;")
        sections.append("}")
        sections.append("")

    root_fn = f"normalize{plan.model_name}Drift"
    sections.append(
        "export function createSelfHealZodAdapter<T extends z.ZodTypeAny>(schema: T) {"
    )
    sections.append(f"  return z.preprocess({root_fn}, schema);")
    sections.append("}")
    sections.append("")
    return "\n".join(sections)
