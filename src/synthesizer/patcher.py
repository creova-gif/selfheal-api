from __future__ import annotations

from typing import Any

import libcst as cst
from pydantic import BaseModel, ConfigDict, ValidationError

from sentinel.detector import DriftKind, DriftReport


class PatchAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_model: str
    target_field: str
    drift_kind: DriftKind
    expected_type: str
    observed_kind: str
    source_path: tuple[str | int, ...] | None = None


class PatchPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_name: str
    actions: tuple[PatchAction, ...]


class PatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_name: str
    original_source: str
    patched_source: str
    plan: PatchPlan
    applied: bool


class VerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    syntax_valid: bool
    drifted_payload_valid: bool
    legacy_payload_valid: bool
    error_detail: str | None = None


def build_patch_plan(report: DriftReport) -> PatchPlan:
    actions: list[PatchAction] = []
    for issue in report.issues:
        if issue.kind == DriftKind.EXTRA_FIELD or issue.kind == DriftKind.INVALID_JSON:
            continue
        if not issue.path:
            continue
        field_name = str(issue.path[0])
        actions.append(
            PatchAction(
                target_model=report.model_name,
                target_field=field_name,
                drift_kind=issue.kind,
                expected_type=issue.expected_type,
                observed_kind=issue.observed_kind,
                source_path=issue.candidate_source_path,
            )
        )
    return PatchPlan(model_name=report.model_name, actions=tuple(actions))


def _default_expr_for_type(expected_type: str) -> str:
    exp = expected_type.lower()
    if "none" in exp:
        return "None"
    if exp.startswith("list"):
        return "[]"
    if exp.startswith("dict"):
        return "{}"
    if exp == "bool":
        return "False"
    if exp == "int":
        return "0"
    if exp == "float":
        return "0.0"
    if exp == "str":
        return "''"
    return "None"


def _coercion_lines_for_action(action: PatchAction) -> list[str]:
    f = action.target_field
    exp = action.expected_type
    exp_lower = exp.lower()
    lines: list[str] = []

    if action.source_path is not None:
        if len(action.source_path) == 1:
            src_key = str(action.source_path[0])
            lines.append(
                f"if {f!r} not in normalized and {src_key!r} in normalized:"
            )
            lines.append(f"    normalized[{f!r}] = normalized[{src_key!r}]")
        elif len(action.source_path) == 2:
            p0, p1 = str(action.source_path[0]), str(action.source_path[1])
            cond = (
                f"if {f!r} not in normalized "
                f"and isinstance(normalized.get({p0!r}), dict) "
                f"and {p1!r} in normalized[{p0!r}]:"
            )
            lines.append(cond)
            lines.append(f"    normalized[{f!r}] = normalized[{p0!r}][{p1!r}]")
        elif len(action.source_path) == 3:
            p0, p1, p2 = (
                str(action.source_path[0]),
                str(action.source_path[1]),
                str(action.source_path[2]),
            )
            cond = (
                f"if {f!r} not in normalized "
                f"and isinstance(normalized.get({p0!r}), dict) "
                f"and isinstance(normalized[{p0!r}].get({p1!r}), dict) "
                f"and {p2!r} in normalized[{p0!r}][{p1!r}]:"
            )
            lines.append(cond)
            lines.append(
                f"    normalized[{f!r}] = normalized[{p0!r}][{p1!r}][{p2!r}]"
            )

    if action.drift_kind == DriftKind.MISSING_FIELD and action.source_path is None:
        default_expr = _default_expr_for_type(exp)
        lines.append(f"if {f!r} not in normalized:")
        lines.append(f"    normalized[{f!r}] = {default_expr}")

    if action.drift_kind == DriftKind.NULLABILITY_DRIFT:
        if "none" not in exp_lower:
            default_expr = _default_expr_for_type(exp)
            lines.append(f"if normalized.get({f!r}) is None:")
            lines.append(f"    normalized[{f!r}] = {default_expr}")

    if "list[" in exp_lower:
        lines.append(
            f"if {f!r} in normalized and not isinstance(normalized[{f!r}], list):"
        )
        lines.append(
            f"    normalized[{f!r}] = [] if normalized[{f!r}] is None else [normalized[{f!r}]]"
        )
        if "list[str]" in exp_lower:
            lines.append(f"if isinstance(normalized.get({f!r}), list):")
            lines.append(
                f"    normalized[{f!r}] = [str(item) for item in normalized[{f!r}]]"
            )
        elif "list[int]" in exp_lower:
            lines.append(f"if isinstance(normalized.get({f!r}), list):")
            comp = (
                f"    normalized[{f!r}] = ["
                f"int(float(item)) for item in normalized[{f!r}] "
                f"if isinstance(item, (int, float, str))]"
            )
            lines.append(comp)
    elif "int" in exp_lower and "dict" not in exp_lower:
        cond = (
            f"if {f!r} in normalized "
            f"and isinstance(normalized[{f!r}], (str, float)) "
            f"and not isinstance(normalized[{f!r}], bool):"
        )
        lines.append(cond)
        lines.append("    try:")
        lines.append(f"        normalized[{f!r}] = int(float(normalized[{f!r}]))")
        lines.append("    except (ValueError, TypeError):")
        lines.append("        pass")
    elif "float" in exp_lower and "dict" not in exp_lower:
        cond = (
            f"if {f!r} in normalized "
            f"and isinstance(normalized[{f!r}], (str, int)) "
            f"and not isinstance(normalized[{f!r}], bool):"
        )
        lines.append(cond)
        lines.append("    try:")
        lines.append(f"        normalized[{f!r}] = float(normalized[{f!r}])")
        lines.append("    except (ValueError, TypeError):")
        lines.append("        pass")
    elif "bool" in exp_lower:
        lines.append(f"if {f!r} in normalized and isinstance(normalized[{f!r}], str):")
        lines.append(
            f"    if normalized[{f!r}].strip().lower() in ('true', '1', 'yes'):"
        )
        lines.append(f"        normalized[{f!r}] = True")
        lines.append(
            f"    elif normalized[{f!r}].strip().lower() in ('false', '0', 'no'):"
        )
        lines.append(f"        normalized[{f!r}] = False")
        cond = (
            f"elif {f!r} in normalized "
            f"and isinstance(normalized[{f!r}], int) "
            f"and not isinstance(normalized[{f!r}], bool):"
        )
        lines.append(cond)
        lines.append(f"    normalized[{f!r}] = bool(normalized[{f!r}])")
    elif exp_lower == "str" or exp_lower == "str | none":
        cond = (
            f"if {f!r} in normalized "
            f"and normalized[{f!r}] is not None "
            f"and not isinstance(normalized[{f!r}], str):"
        )
        lines.append(cond)
        lines.append(f"    if isinstance(normalized[{f!r}], dict):")
        lines.append(
            "        for _k in ('id', 'code', 'value', 'name', 'email', 'address'):"
        )
        inner_cond = (
            f"            if _k in normalized[{f!r}] "
            f"and isinstance(normalized[{f!r}][_k], str):"
        )
        lines.append(inner_cond)
        lines.append(f"                normalized[{f!r}] = normalized[{f!r}][_k]")
        lines.append("                break")
        lines.append("        else:")
        lines.append(f"            normalized[{f!r}] = str(normalized[{f!r}])")
        lines.append("    else:")
        lines.append(f"        normalized[{f!r}] = str(normalized[{f!r}])")

    return lines


def _build_validator_method_cst(actions: tuple[PatchAction, ...]) -> cst.FunctionDef:
    body_lines: list[str] = [
        "@model_validator(mode='before')",
        "@classmethod",
        "def _selfheal_normalize_drift(cls, data: Any) -> Any:",
        "    if not isinstance(data, dict):",
        "        return data",
        "    normalized: dict[str, Any] = dict(data)",
    ]
    seen_fields: set[tuple[str, DriftKind, tuple[str | int, ...] | None]] = set()
    for action in actions:
        dedup_key = (action.target_field, action.drift_kind, action.source_path)
        if dedup_key in seen_fields:
            continue
        seen_fields.add(dedup_key)
        for line in _coercion_lines_for_action(action):
            body_lines.append(f"    {line}")

    body_lines.append("    return normalized")
    module_snippet = cst.parse_module("\n".join(body_lines) + "\n")
    func_def = module_snippet.body[0]
    if not isinstance(func_def, cst.FunctionDef):
        raise TypeError("Expected FunctionDef from synthesized validator snippet")
    return func_def


class _ModelPatchTransformer(cst.CSTTransformer):
    def __init__(self, plan: PatchPlan) -> None:
        super().__init__()
        self._plan = plan
        self._widen_to_str_fields: set[str] = {
            a.target_field
            for a in plan.actions
            if a.drift_kind == DriftKind.ENUM_DRIFT
        }
        self._widen_nullable_fields: set[str] = {
            a.target_field
            for a in plan.actions
            if a.drift_kind == DriftKind.NULLABILITY_DRIFT
            and a.expected_type.lower().startswith("literal")
        }
        self._has_any_import = False
        self._has_model_validator_import = False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        if isinstance(node.module, cst.Name):
            if node.module.value == "typing" and isinstance(node.names, tuple):
                for alias in node.names:
                    if isinstance(alias.name, cst.Name) and alias.name.value == "Any":
                        self._has_any_import = True
            elif node.module.value == "pydantic" and isinstance(node.names, tuple):
                for alias in node.names:
                    if (
                        isinstance(alias.name, cst.Name)
                        and alias.name.value == "model_validator"
                    ):
                        self._has_model_validator_import = True
        return True

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef:
        if updated_node.name.value != self._plan.model_name:
            return updated_node

        new_stmts: list[cst.BaseStatement | cst.BaseSmallStatement] = []
        for stmt in updated_node.body.body:
            if isinstance(stmt, cst.SimpleStatementLine):
                updated_line = self._transform_class_attribute_line(stmt)
                new_stmts.append(updated_line)
            elif (
                isinstance(stmt, cst.FunctionDef)
                and stmt.name.value == "_selfheal_normalize_drift"
            ):
                continue
            else:
                new_stmts.append(stmt)

        if self._plan.actions:
            validator_fn = _build_validator_method_cst(self._plan.actions)
            new_stmts.append(validator_fn)

        return updated_node.with_changes(
            body=updated_node.body.with_changes(body=tuple(new_stmts))
        )

    def _transform_class_attribute_line(
        self, line: cst.SimpleStatementLine
    ) -> cst.SimpleStatementLine:
        new_small_stmts: list[cst.BaseSmallStatement] = []
        for small in line.body:
            if isinstance(small, cst.AnnAssign) and isinstance(
                small.target, cst.Name
            ):
                field_name = small.target.value
                if field_name in self._widen_to_str_fields:
                    small = small.with_changes(
                        annotation=cst.Annotation(annotation=cst.Name("str"))
                    )
                elif field_name in self._widen_nullable_fields:
                    small = small.with_changes(
                        annotation=cst.Annotation(
                            annotation=cst.parse_expression("str | None")
                        )
                    )
            elif isinstance(small, cst.Assign):
                for assign_target in small.targets:
                    if (
                        isinstance(assign_target.target, cst.Name)
                        and assign_target.target.value == "model_config"
                    ):
                        small = small.with_changes(
                            value=cst.parse_expression("ConfigDict(extra='ignore')")
                        )
            new_small_stmts.append(small)
        return line.with_changes(body=tuple(new_small_stmts))

    def leave_Module(
        self, original_node: cst.Module, updated_node: cst.Module
    ) -> cst.Module:
        if not self._plan.actions:
            return updated_node

        prepend_stmts: list[cst.BaseStatement] = []
        if not self._has_any_import:
            prepend_stmts.append(cst.parse_statement("from typing import Any\n"))
        if not self._has_model_validator_import:
            prepend_stmts.append(
                cst.parse_statement("from pydantic import model_validator\n")
            )

        if not prepend_stmts:
            return updated_node

        insert_idx = 0
        for idx, stmt in enumerate(updated_node.body):
            if isinstance(stmt, cst.SimpleStatementLine) and any(
                isinstance(s, cst.ImportFrom)
                and isinstance(s.module, cst.Name)
                and s.module.value == "__future__"
                for s in stmt.body
            ):
                insert_idx = idx + 1
                break

        new_body = (
            list(updated_node.body[:insert_idx])
            + prepend_stmts
            + list(updated_node.body[insert_idx:])
        )
        return updated_node.with_changes(body=tuple(new_body))


def synthesize_patch(source_code: str, report: DriftReport) -> PatchResult:
    plan = build_patch_plan(report)
    if not report.has_drift:
        return PatchResult(
            model_name=report.model_name,
            original_source=source_code,
            patched_source=source_code,
            plan=plan,
            applied=False,
        )

    module = cst.parse_module(source_code)
    transformer = _ModelPatchTransformer(plan)
    patched_module = module.visit(transformer)
    patched_code = patched_module.code

    return PatchResult(
        model_name=report.model_name,
        original_source=source_code,
        patched_source=patched_code,
        plan=plan,
        applied=patched_code != source_code,
    )


def verify_patch(
    patch_result: PatchResult,
    drifted_payload: dict[str, Any],
    legacy_payload: dict[str, Any] | None = None,
) -> VerificationResult:
    try:
        cst.parse_module(patch_result.patched_source)
    except cst.ParserSyntaxError as exc:
        return VerificationResult(
            passed=False,
            syntax_valid=False,
            drifted_payload_valid=False,
            legacy_payload_valid=False,
            error_detail=f"Syntax error in patched CST: {exc}",
        )

    namespace: dict[str, Any] = {}
    try:
        exec(patch_result.patched_source, namespace)
    except Exception as exc:  # noqa: BLE001
        return VerificationResult(
            passed=False,
            syntax_valid=True,
            drifted_payload_valid=False,
            legacy_payload_valid=False,
            error_detail=f"Failed to execute patched module: {exc}",
        )

    model_cls = namespace.get(patch_result.model_name)
    if not isinstance(model_cls, type) or not issubclass(model_cls, BaseModel):
        return VerificationResult(
            passed=False,
            syntax_valid=True,
            drifted_payload_valid=False,
            legacy_payload_valid=False,
            error_detail=f"Model {patch_result.model_name} not found in patched source",
        )

    try:
        model_cls.model_rebuild(_types_namespace=namespace)
        model_cls.model_validate(drifted_payload, strict=True)
        drifted_ok = True
    except ValidationError as exc:
        return VerificationResult(
            passed=False,
            syntax_valid=True,
            drifted_payload_valid=False,
            legacy_payload_valid=False,
            error_detail=f"Drifted payload validation failed: {exc}",
        )

    legacy_ok = True
    if legacy_payload is not None:
        try:
            model_cls.model_validate(legacy_payload, strict=True)
        except ValidationError as exc:
            return VerificationResult(
                passed=False,
                syntax_valid=True,
                drifted_payload_valid=drifted_ok,
                legacy_payload_valid=False,
                error_detail=f"Legacy payload regression failed: {exc}",
            )

    return VerificationResult(
        passed=drifted_ok and legacy_ok,
        syntax_valid=True,
        drifted_payload_valid=drifted_ok,
        legacy_payload_valid=legacy_ok,
        error_detail=None,
    )
