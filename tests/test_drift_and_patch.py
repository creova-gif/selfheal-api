from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict

from sentinel import DriftKind, PrimitiveKind, detect_drift, redact_payload
from synthesizer import synthesize_patch, verify_patch


class StripeChargeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    amount: int
    currency: str
    customer_email: str
    paid: bool
    status: Literal["succeeded", "pending", "failed"]
    receipt_url: str | None = None


STRIPE_SOURCE = """from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict


class StripeChargeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    amount: int
    currency: str
    customer_email: str
    paid: bool
    status: Literal["succeeded", "pending", "failed"]
    receipt_url: str | None = None
"""

STRIPE_CANONICAL: dict[str, Any] = {
    "id": "ch_3Pq981LkdIwHu7ix",
    "amount": 4900,
    "currency": "usd",
    "customer_email": "alice.secret@example.com",
    "paid": True,
    "status": "succeeded",
    "receipt_url": "https://pay.stripe.com/receipts/rcpt_secret_123",
}


class HubSpotContactEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: int
    email: str
    lifecycle_stage: Literal["lead", "mql", "sql", "customer"]
    company_name: str
    annual_revenue: float
    is_active: bool
    tags: list[str]


HUBSPOT_SOURCE = """from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict


class HubSpotContactEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: int
    email: str
    lifecycle_stage: Literal["lead", "mql", "sql", "customer"]
    company_name: str
    annual_revenue: float
    is_active: bool
    tags: list[str]
"""

HUBSPOT_CANONICAL: dict[str, Any] = {
    "contact_id": 884210,
    "email": "cto-private@startup.io",
    "lifecycle_stage": "sql",
    "company_name": "Acme Cloud Inc",
    "annual_revenue": 1250000.50,
    "is_active": True,
    "tags": ["enterprise", "soc2"],
}


class WeatherObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    station_id: str
    temp_c: float
    humidity_pct: int
    wind_kph: float
    condition: Literal["clear", "rain", "snow", "cloudy"]
    alerts: list[str]
    is_daytime: bool


WEATHER_SOURCE = """from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict


class WeatherObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    station_id: str
    temp_c: float
    humidity_pct: int
    wind_kph: float
    condition: Literal["clear", "rain", "snow", "cloudy"]
    alerts: list[str]
    is_daytime: bool
"""

WEATHER_CANONICAL: dict[str, Any] = {
    "station_id": "KSFO-01",
    "temp_c": 18.4,
    "humidity_pct": 72,
    "wind_kph": 21.5,
    "condition": "clear",
    "alerts": ["high_surf"],
    "is_daytime": True,
}


def _mutate(base: dict[str, Any], **updates: Any) -> dict[str, Any]:
    copy = dict(base)
    copy.update(updates)
    return copy


def _rename(base: dict[str, Any], old_key: str, new_key: str, val: Any = ...) -> dict[str, Any]:
    copy = dict(base)
    popped = copy.pop(old_key)
    copy[new_key] = popped if val is ... else val
    return copy


def _drop(base: dict[str, Any], key: str) -> dict[str, Any]:
    copy = dict(base)
    copy.pop(key, None)
    return copy


CORE_10_MUTATIONS: list[tuple[str, type[BaseModel], str, dict[str, Any], dict[str, Any]]] = [
    (
        "stripe_01_rename_amount_to_amount_cents",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _rename(STRIPE_CANONICAL, "amount", "amount_cents"),
    ),
    (
        "stripe_02_coerce_amount_int_to_numeric_str",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _mutate(STRIPE_CANONICAL, amount="4900"),
    ),
    (
        "stripe_03_nest_customer_email_into_customer_obj",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _rename(
            STRIPE_CANONICAL,
            "customer_email",
            "customer",
            {"email": "alice.secret@example.com", "tier": "pro"},
        ),
    ),
    (
        "stripe_04_enum_drift_status_requires_capture",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _mutate(STRIPE_CANONICAL, status="requires_capture"),
    ),
    (
        "hubspot_05_coerce_contact_id_and_revenue_strings",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _mutate(HUBSPOT_CANONICAL, contact_id="884210", annual_revenue="1250000.50"),
    ),
    (
        "hubspot_06_nest_company_name_into_company_dict",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _rename(
            HUBSPOT_CANONICAL,
            "company_name",
            "company",
            {"name": "Acme Cloud Inc", "domain": "acme.io"},
        ),
    ),
    (
        "hubspot_07_tags_single_string_instead_of_list",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _mutate(HUBSPOT_CANONICAL, tags="enterprise"),
    ),
    (
        "weather_08_rename_temp_c_to_temperature_celsius",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _rename(WEATHER_CANONICAL, "temp_c", "temperature_celsius"),
    ),
    (
        "weather_09_nullability_drift_on_humidity_and_alerts",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(WEATHER_CANONICAL, humidity_pct=None, alerts=None),
    ),
    (
        "weather_10_bool_string_and_enum_expansion",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(WEATHER_CANONICAL, is_daytime="true", condition="thunderstorm"),
    ),
]


EXTENDED_30_MUTATIONS: list[tuple[str, type[BaseModel], str, dict[str, Any], dict[str, Any]]] = [
    # Additional Stripe mutations (11-20)
    (
        "stripe_11_rename_id_to_charge_id",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _rename(STRIPE_CANONICAL, "id", "charge_id"),
    ),
    (
        "stripe_12_rename_currency_to_iso_currency",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _rename(STRIPE_CANONICAL, "currency", "iso_currency"),
    ),
    (
        "stripe_13_paid_boolean_as_string_true",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _mutate(STRIPE_CANONICAL, paid="true"),
    ),
    (
        "stripe_14_paid_boolean_as_int_one",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _mutate(STRIPE_CANONICAL, paid=1),
    ),
    (
        "stripe_15_amount_as_float_4900_0",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _mutate(STRIPE_CANONICAL, amount=4900.0),
    ),
    (
        "stripe_16_currency_nested_object_with_code",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _mutate(STRIPE_CANONICAL, currency={"code": "usd", "symbol": "$"}),
    ),
    (
        "stripe_17_status_nullability_drift",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _mutate(STRIPE_CANONICAL, status=None),
    ),
    (
        "stripe_18_missing_paid_field_dropped",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _drop(STRIPE_CANONICAL, "paid"),
    ),
    (
        "stripe_19_extra_metadata_and_risk_score_fields",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _mutate(STRIPE_CANONICAL, risk_score=12, payment_method_type="card"),
    ),
    (
        "stripe_20_rename_amount_to_total_amount_and_str",
        StripeChargeEvent,
        STRIPE_SOURCE,
        STRIPE_CANONICAL,
        _rename(STRIPE_CANONICAL, "amount", "total_amount", "9900"),
    ),
    # Additional HubSpot mutations (21-30)
    (
        "hubspot_21_rename_email_to_email_address",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _rename(HUBSPOT_CANONICAL, "email", "email_address"),
    ),
    (
        "hubspot_22_rename_lifecycle_stage_to_stage",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _rename(HUBSPOT_CANONICAL, "lifecycle_stage", "stage"),
    ),
    (
        "hubspot_23_enum_drift_lifecycle_evangelist",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _mutate(HUBSPOT_CANONICAL, lifecycle_stage="evangelist"),
    ),
    (
        "hubspot_24_contact_id_as_float_884210_0",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _mutate(HUBSPOT_CANONICAL, contact_id=884210.0),
    ),
    (
        "hubspot_25_annual_revenue_null",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _mutate(HUBSPOT_CANONICAL, annual_revenue=None),
    ),
    (
        "hubspot_26_is_active_string_false",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _mutate(HUBSPOT_CANONICAL, is_active="false"),
    ),
    (
        "hubspot_27_tags_list_of_ints_coerced_to_str",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _mutate(HUBSPOT_CANONICAL, tags=[101, 202]),
    ),
    (
        "hubspot_28_rename_annual_revenue_to_arr",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _rename(HUBSPOT_CANONICAL, "annual_revenue", "arr"),
    ),
    (
        "hubspot_29_nest_email_under_contact_profile",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _rename(
            HUBSPOT_CANONICAL,
            "email",
            "profile",
            {"email_address": "cto-private@startup.io"},
        ),
    ),
    (
        "hubspot_30_missing_tags_field_dropped",
        HubSpotContactEvent,
        HUBSPOT_SOURCE,
        HUBSPOT_CANONICAL,
        _drop(HUBSPOT_CANONICAL, "tags"),
    ),
    # Additional Weather mutations (31-40)
    (
        "weather_31_rename_humidity_pct_to_relative_humidity",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _rename(WEATHER_CANONICAL, "humidity_pct", "relative_humidity"),
    ),
    (
        "weather_32_rename_wind_kph_to_wind_speed_kph",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _rename(WEATHER_CANONICAL, "wind_kph", "wind_speed_kph"),
    ),
    (
        "weather_33_temp_c_as_numeric_string",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(WEATHER_CANONICAL, temp_c="18.4"),
    ),
    (
        "weather_34_humidity_pct_as_float_72_0",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(WEATHER_CANONICAL, humidity_pct=72.0),
    ),
    (
        "weather_35_wind_kph_as_numeric_string",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(WEATHER_CANONICAL, wind_kph="21.5"),
    ),
    (
        "weather_36_station_id_as_int_coerced_to_str",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(WEATHER_CANONICAL, station_id=94101),
    ),
    (
        "weather_37_nest_wind_kph_under_wind_speed",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _rename(WEATHER_CANONICAL, "wind_kph", "wind", {"speed_kph": 21.5}),
    ),
    (
        "weather_38_alerts_single_str_instead_of_list",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(WEATHER_CANONICAL, alerts="high_wind_warning"),
    ),
    (
        "weather_39_missing_alerts_dropped_with_extra_sensor_ts",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(_drop(WEATHER_CANONICAL, "alerts"), sensor_ts="2026-11-25T15:00:00Z"),
    ),
    (
        "weather_40_combined_temp_str_and_humidity_str",
        WeatherObservation,
        WEATHER_SOURCE,
        WEATHER_CANONICAL,
        _mutate(WEATHER_CANONICAL, temp_c="19.2", humidity_pct="68"),
    ),
]

ALL_40_MUTATIONS = CORE_10_MUTATIONS + EXTENDED_30_MUTATIONS


def test_canonical_payloads_have_no_drift() -> None:
    cases: list[tuple[type[BaseModel], dict[str, Any]]] = [
        (StripeChargeEvent, STRIPE_CANONICAL),
        (HubSpotContactEvent, HUBSPOT_CANONICAL),
        (WeatherObservation, WEATHER_CANONICAL),
    ]
    for model_cls, payload in cases:
        detect_drift(model_cls, payload)
    for model_cls, payload in cases:
        report = detect_drift(model_cls, json.dumps(payload))
        assert not report.has_drift
        assert report.issues == ()
        assert report.elapsed_ms < 2.0


def test_pii_redaction_strips_all_secret_scalars() -> None:
    raw = json.dumps(STRIPE_CANONICAL)
    report = detect_drift(StripeChargeEvent, raw)
    serialized_report = report.model_dump_json()

    assert "alice.secret@example.com" not in serialized_report
    assert "ch_3Pq981LkdIwHu7ix" not in serialized_report
    assert "rcpt_secret_123" not in serialized_report
    assert "4900" not in serialized_report
    assert report.flat_observed_kinds["customer_email"] == PrimitiveKind.STR
    assert report.flat_observed_kinds["amount"] == PrimitiveKind.INT


@pytest.mark.parametrize(
    ("name", "model_cls", "source_code", "canonical", "mutated"),
    CORE_10_MUTATIONS,
    ids=[m[0] for m in CORE_10_MUTATIONS],
)
def test_core_10_mutation_suite_detection_and_patch(
    name: str,
    model_cls: type[BaseModel],
    source_code: str,
    canonical: dict[str, Any],
    mutated: dict[str, Any],
) -> None:
    report = detect_drift(model_cls, mutated)
    assert report.has_drift, f"Expected drift to be detected for {name}"
    assert len(report.issues) >= 1

    patch_res = synthesize_patch(source_code, report)
    assert patch_res.applied, f"Expected CST patch to be applied for {name}"

    verification = verify_patch(patch_res, mutated, canonical)
    assert verification.passed, (
        f"Verification failed for {name}: {verification.error_detail}\n"
        f"Patched source:\n{patch_res.patched_source}"
    )


def test_40_mutation_benchmark_thresholds() -> None:
    detected = 0
    patched_passed = 0
    latencies_ms: list[float] = []

    for _ in range(3):
        for _name, model_cls, _src, _canonical, mutated in ALL_40_MUTATIONS:
            detect_drift(model_cls, mutated)

    for _name, model_cls, _src, _canonical, mutated in ALL_40_MUTATIONS:
        report = detect_drift(model_cls, mutated)
        latencies_ms.append(report.elapsed_ms)
        if report.has_drift:
            detected += 1

    for _name, model_cls, source_code, canonical, mutated in ALL_40_MUTATIONS:
        report = detect_drift(model_cls, mutated)
        patch_res = synthesize_patch(source_code, report)
        verification = verify_patch(patch_res, mutated, canonical)
        if verification.passed:
            patched_passed += 1

    recall = detected / len(ALL_40_MUTATIONS)
    pass_rate = patched_passed / len(ALL_40_MUTATIONS)
    sorted_lat = sorted(latencies_ms)
    median_ms = sorted_lat[len(sorted_lat) // 2]
    p99_ms = sorted_lat[int(len(sorted_lat) * 0.99)]

    assert recall == 1.0, f"Expected 100% drift detection recall, got {recall:.2%}"
    assert pass_rate >= 0.85, f"Expected >= 85% patch pass rate, got {pass_rate:.2%}"
    assert median_ms < 2.0, f"Expected < 2ms median runtime check, got {median_ms:.3f}ms"
    assert p99_ms < 2.0, f"Expected < 2ms p99 runtime check, got {p99_ms:.3f}ms"


def test_synthesized_patch_passes_mypy_strict(tmp_path: Path) -> None:
    _, model_cls, source_code, canonical, mutated = CORE_10_MUTATIONS[0]
    report = detect_drift(model_cls, mutated)
    patch_res = synthesize_patch(source_code, report)
    verification = verify_patch(patch_res, mutated, canonical)
    assert verification.passed

    patched_file = tmp_path / "patched_stripe_model.py"
    patched_file.write_text(patch_res.patched_source, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(patched_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"mypy --strict failed on patched file:\n{proc.stdout}\n{proc.stderr}"
    )


def test_invalid_json_detected_cleanly() -> None:
    report = detect_drift(StripeChargeEvent, "{invalid_json:")
    assert report.has_drift
    assert len(report.issues) == 1
    assert report.issues[0].kind == DriftKind.INVALID_JSON
    assert redact_payload({}) == PrimitiveKind.EMPTY_OBJECT
    assert redact_payload([]) == PrimitiveKind.EMPTY_LIST
