"""Garmin read adapter that returns in-memory CoachApplication ingest batches.

No collected payload, cursor, health state, credential, or token is written to
an application data directory.  The only filesystem use is the private
short-lived token directory issued by CoachApplication.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from garminconnect import Garmin

from coach.application import (
    CapturePointer,
    CollectedBatch,
    CollectedCapture,
    CollectedTrainingSession,
    CollectionCheckpoint,
    ControlledObservation,
    RepairableAuthenticationError,
    TransientCollectionError,
    _AuthenticatedTokenSession,
    _ExternalCollection,
)

from . import endpoints


_ACTIVITY_OVERLAP_DAYS = 3
_ACTIVITY_PAGE_SIZE = 20
_ACTIVITY_RECONCILIATION_DAYS = 7
_INCREMENTAL_DAILY_OVERLAP_DAYS = 2
_MAX_INCREMENTAL_DAYS = 730


@dataclass(slots=True)
class _GarminSession:
    client: Garmin
    initial_days: int
    activity_limit: int


class GarminCollectionAdapter:
    """Provider adapter used only from CoachApplication collection jobs."""

    def authenticate(
        self,
        token_directory: Path,
        credentials: bytes | None,
        cached_tokens: bool,
    ) -> _AuthenticatedTokenSession:
        initial_days = 365
        activity_limit = 100
        email: str | None = None
        password: str | None = None
        bundle = token_directory / "tokens.bundle"
        if cached_tokens:
            try:
                metadata = _restore_token_bundle(bundle, token_directory)
                initial_days = _bounded_int(metadata.get("initial_days"), 365, 1, 730)
                activity_limit = _bounded_int(metadata.get("activity_limit"), 100, 1, 500)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raise RepairableAuthenticationError("authentication_required") from None
        else:
            try:
                decoded = json.loads((credentials or b"").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RepairableAuthenticationError("credentials_rejected") from None
            if not isinstance(decoded, dict):
                raise RepairableAuthenticationError("credentials_rejected")
            email = decoded.get("email")
            password = decoded.get("password")
            if not isinstance(email, str) or not email.strip() or not isinstance(password, str) or not password:
                raise RepairableAuthenticationError("credentials_rejected")
            initial_days = _bounded_int(decoded.get("initial_days"), 365, 1, 730)
            activity_limit = _bounded_int(decoded.get("activity_limit"), 100, 1, 500)

        def reject_mfa() -> str:
            raise RepairableAuthenticationError("mfa_required")

        client = Garmin(email, password, prompt_mfa=reject_mfa)
        try:
            client.login(str(token_directory))
        except RepairableAuthenticationError:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if "mfa" in message or "multi-factor" in message:
                raise RepairableAuthenticationError("mfa_required") from None
            if "429" in message or "rate" in message or "too many" in message:
                raise TransientCollectionError("rate_limited") from None
            raise RepairableAuthenticationError("credentials_rejected") from None
        rotated = _create_token_bundle(
            token_directory,
            initial_days=initial_days,
            activity_limit=activity_limit,
        )
        return _AuthenticatedTokenSession(
            _GarminSession(client, initial_days, activity_limit), rotated
        )

    def collect(
        self,
        session: Any,
        kind: str,
        prior_checkpoint: Mapping[str, Any] | None = None,
    ) -> _ExternalCollection:
        if not isinstance(session, _GarminSession):
            raise ValueError("invalid Garmin session")
        if kind not in {"initial_sync", "incremental"}:
            raise ValueError("unsupported collection kind")
        if prior_checkpoint is not None and not isinstance(prior_checkpoint, Mapping):
            raise ValueError("invalid prior checkpoint")
        today = date.today()
        collection_dates = _daily_collection_dates(
            today,
            kind,
            session.initial_days,
            prior_checkpoint,
        )
        captures: list[CollectedCapture] = []
        observations: list[ControlledObservation] = []
        sessions: list[CollectedTrainingSession] = []

        for local_date in collection_dates:
            iso = local_date.isoformat()
            daily_payloads: dict[str, Any] = {}
            for name, function in endpoints.DAILY.items():
                payload = _pull_optional(function, session.client, iso)
                if payload is None:
                    if name == "stats":
                        raise TransientCollectionError("provider_unavailable")
                    continue
                daily_payloads[name] = payload
                captures.append(
                    CollectedCapture(
                        record_kind=f"daily.{name}",
                        source_key=iso,
                        payload=payload,
                        source_at=datetime.now(timezone.utc),
                        provenance={"provider": "garmin", "endpoint": name},
                    )
                )
            observations.extend(_daily_observations(local_date, daily_payloads))

        for name, function in endpoints.PLANS.items():
            payload = _pull_optional(function, session.client, today)
            if payload is not None:
                captures.append(
                    CollectedCapture(
                        f"plan.{name}",
                        today.isoformat(),
                        payload,
                        provenance={"provider": "garmin", "endpoint": name},
                    )
                )

        reconcile_activities = _reconcile_activities(
            today,
            kind,
            prior_checkpoint,
        )
        listed_activities, detail_activities = _activities_to_collect(
            session.client,
            session.activity_limit,
            cutoff=(
                _activity_cutoff(prior_checkpoint)
                if kind == "incremental" and not reconcile_activities
                else None
            ),
        )
        for activity_id, listed in listed_activities:
            captures.append(
                CollectedCapture(
                    "activity.list_summary",
                    activity_id,
                    listed,
                    provenance={"provider": "garmin", "endpoint": "activities"},
                )
            )
        for activity_id, listed in detail_activities:
            details: dict[str, Any] = {}
            for name, function in endpoints.ACTIVITY_DETAIL.items():
                payload = _pull_optional(function, session.client, listed["activityId"])
                if payload is None:
                    if name == "summary":
                        raise TransientCollectionError("provider_unavailable")
                    continue
                details[name] = payload
                captures.append(
                    CollectedCapture(
                        f"activity.{name}",
                        activity_id,
                        payload,
                        provenance={"provider": "garmin", "endpoint": name},
                    )
                )
            summary = details.get("summary")
            if isinstance(summary, dict):
                canonical = _activity_session(activity_id, summary)
                if canonical is not None:
                    sessions.append(canonical)

        # Profile payloads are immutable captures; canonical identity remains
        # provider-neutral and display-name changes stay an explicit App command.
        for name, function in endpoints.SNAPSHOT.items():
            payload = _pull_optional(function, session.client)
            if payload is not None:
                captures.append(
                    CollectedCapture(
                        f"profile.{name}",
                        today.isoformat(),
                        payload,
                        provenance={"provider": "garmin", "endpoint": name},
                    )
                )

        if not captures:
            raise TransientCollectionError("provider_unavailable")
        semantics = json.dumps(
            [(item.record_kind, item.source_key, item.payload) for item in captures],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        batch = CollectedBatch(
            # CoachApplication replaces this sentinel with the job-owned opaque
            # source capability before validation; provider code never gets it.
            source=None,  # type: ignore[arg-type]
            captures=tuple(captures),
            idempotency_key=f"garmin:{hashlib.sha256(semantics).hexdigest()}",
            sessions=tuple(sessions),
            observations=tuple(observations),
        )
        prior_reconciliation = (
            _checkpoint_date(prior_checkpoint, "last_activity_reconciliation")
            or _checkpoint_date(prior_checkpoint, "through_date")
        )
        checkpoint_cursor: dict[str, Any] = {
            "through_date": today.isoformat(),
            "activity_window": session.activity_limit,
            "activity_overlap_days": _ACTIVITY_OVERLAP_DAYS,
            "last_activity_reconciliation": (
                today if reconcile_activities else prior_reconciliation or today
            ).isoformat(),
        }
        if listed_activities:
            latest_id, latest = listed_activities[0]
            checkpoint_cursor["latest_activity_id"] = latest_id
            latest_date = _listed_activity_date(latest)
            if latest_date is not None:
                checkpoint_cursor["latest_activity_date"] = latest_date.isoformat()
        return _ExternalCollection(
            batch,
            CollectionCheckpoint(kind, checkpoint_cursor),
        )


def _checkpoint_date(
    checkpoint: Mapping[str, Any] | None,
    key: str,
) -> date | None:
    if checkpoint is None:
        return None
    value = checkpoint.get(key)
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _daily_collection_dates(
    today: date,
    kind: str,
    initial_days: int,
    prior_checkpoint: Mapping[str, Any] | None,
) -> tuple[date, ...]:
    if kind == "initial_sync":
        days = initial_days
    else:
        through = _checkpoint_date(prior_checkpoint, "through_date")
        if through is None:
            days = _INCREMENTAL_DAILY_OVERLAP_DAYS
        else:
            boundary = min(through, today)
            starts_on = boundary - timedelta(
                days=_INCREMENTAL_DAILY_OVERLAP_DAYS - 1
            )
            earliest = today - timedelta(days=_MAX_INCREMENTAL_DAYS - 1)
            starts_on = max(starts_on, earliest)
            days = (today - starts_on).days + 1
    return tuple(today - timedelta(days=offset) for offset in range(days))


def _reconcile_activities(
    today: date,
    kind: str,
    prior_checkpoint: Mapping[str, Any] | None,
) -> bool:
    if kind == "initial_sync" or prior_checkpoint is None:
        return True
    reconciled_on = (
        _checkpoint_date(prior_checkpoint, "last_activity_reconciliation")
        or _checkpoint_date(prior_checkpoint, "through_date")
    )
    return (
        reconciled_on is None
        or (today - min(reconciled_on, today)).days
        >= _ACTIVITY_RECONCILIATION_DAYS
    )


def _activity_cutoff(
    prior_checkpoint: Mapping[str, Any] | None,
) -> date | None:
    through = _checkpoint_date(prior_checkpoint, "through_date")
    if through is None:
        return None
    return through - timedelta(days=_ACTIVITY_OVERLAP_DAYS)


def _listed_activity_date(listed: Mapping[str, Any]) -> date | None:
    for key in ("startTimeLocal", "startTimeGMT"):
        value = listed.get(key)
        if not isinstance(value, str):
            continue
        try:
            return datetime.fromisoformat(value.replace(" ", "T")).date()
        except ValueError:
            continue
    return None


def _activities_to_collect(
    client: Garmin,
    scan_limit: int,
    *,
    cutoff: date | None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    listed_activities: list[tuple[str, dict[str, Any]]] = []
    detail_activities: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    offset = 0
    scanned = 0
    reached_cutoff = False
    while scanned < scan_limit and not reached_cutoff:
        page_limit = min(_ACTIVITY_PAGE_SIZE, scan_limit - scanned)
        try:
            page = client.get_activities(offset, page_limit)
        except Exception as exc:
            _raise_provider_error(exc)
        if not isinstance(page, list):
            raise ValueError("Garmin activities response is invalid")
        if not page:
            break
        scanned += len(page)
        offset += len(page)
        for listed in page:
            if not isinstance(listed, dict) or listed.get("activityId") is None:
                continue
            activity_id = str(listed["activityId"])
            if activity_id in seen:
                continue
            seen.add(activity_id)
            listed_activities.append((activity_id, listed))
            listed_date = _listed_activity_date(listed)
            if cutoff is not None and listed_date is not None and listed_date < cutoff:
                reached_cutoff = True
                break
            detail_activities.append((activity_id, listed))
        if len(page) < page_limit:
            break
    return listed_activities, detail_activities


def credential_bundle(email: str, password: str, *, initial_days: int = 365) -> bytes:
    if not isinstance(email, str) or not email.strip() or not isinstance(password, str) or not password:
        raise ValueError("Garmin credentials are required")
    if isinstance(initial_days, bool) or not isinstance(initial_days, int) or not 1 <= initial_days <= 730:
        raise ValueError("initial_days must be between 1 and 730")
    return json.dumps(
        {
            "email": email.strip(),
            "password": password,
            "initial_days": initial_days,
            "activity_limit": 100,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("integer setting is out of bounds")
    return value


def _create_token_bundle(directory: Path, *, initial_days: int, activity_limit: int) -> bytes:
    files: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name == "tokens.bundle":
            continue
        files[path.name] = base64.b64encode(path.read_bytes()).decode("ascii")
    if not files:
        raise RepairableAuthenticationError("authentication_required")
    return json.dumps(
        {
            "schema_version": 1,
            "initial_days": initial_days,
            "activity_limit": activity_limit,
            "files": files,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _restore_token_bundle(bundle: Path, directory: Path) -> Mapping[str, Any]:
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported token bundle")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("empty token bundle")
    bundle.unlink()
    for name, encoded in files.items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(encoded, str):
            raise ValueError("invalid token bundle")
        path = directory / name
        path.write_bytes(base64.b64decode(encoded, validate=True))
        path.chmod(0o600)
    return payload


def _pull_optional(function, *args):
    """Return provider-declared no-data while never hiding endpoint failure."""
    try:
        return function(*args)
    except Exception as exc:
        _raise_provider_error(exc)


def _raise_provider_error(exc: Exception) -> None:
    message = str(exc).lower()
    if "429" in message or "rate" in message or "too many" in message:
        raise TransientCollectionError("rate_limited") from None
    raise TransientCollectionError("provider_unavailable") from None


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _daily_observations(local_date: date, payloads: Mapping[str, Any]) -> list[ControlledObservation]:
    definitions: list[tuple[str, str, str, str, Any]] = []
    stats = payloads.get("stats") if isinstance(payloads.get("stats"), dict) else {}
    definitions.extend(
        (
            ("stats", "daily_steps", "integer", "count", stats.get("totalSteps")),
            ("stats", "daily_distance", "decimal", "metres", stats.get("totalDistanceMeters")),
            ("stats", "daily_active_energy", "integer", "kilocalories", stats.get("activeKilocalories")),
            ("stats", "daily_resting_heart_rate", "integer", "beats_per_minute", stats.get("restingHeartRate")),
            ("stats", "daily_minimum_heart_rate", "integer", "beats_per_minute", stats.get("minHeartRate")),
            ("stats", "daily_maximum_heart_rate", "integer", "beats_per_minute", stats.get("maxHeartRate")),
            ("stats", "daily_average_stress", "decimal", "score", stats.get("averageStressLevel")),
            ("stats", "daily_moderate_intensity_minutes", "integer", "minutes", stats.get("moderateIntensityMinutes")),
            ("stats", "daily_vigorous_intensity_minutes", "integer", "minutes", stats.get("vigorousIntensityMinutes")),
            ("stats", "daily_floors_ascended", "decimal", "floors", stats.get("floorsAscended")),
        )
    )
    sleep = payloads.get("sleep")
    sleep_dto = _nested(sleep, "dailySleepDTO") or {}
    definitions.extend(
        (
            ("sleep", "daily_sleep_duration", "integer", "seconds", sleep_dto.get("sleepTimeSeconds")),
            ("sleep", "daily_sleep_score", "decimal", "score", _nested(sleep_dto, "sleepScores", "overall", "value")),
        )
    )
    hrv = payloads.get("hrv")
    definitions.extend(
        (
            ("hrv", "nightly_hrv_average", "decimal", "milliseconds", _nested(hrv, "hrvSummary", "lastNightAvg")),
            ("hrv", "daily_hrv_status", "text", "status", _nested(hrv, "hrvSummary", "status")),
        )
    )
    readiness = payloads.get("training_readiness")
    if isinstance(readiness, list):
        readiness = readiness[0] if readiness else {}
    readiness = readiness if isinstance(readiness, dict) else {}
    definitions.extend(
        (
            ("training_readiness", "daily_training_readiness", "decimal", "score", readiness.get("score")),
            ("training_readiness", "daily_training_readiness_level", "text", "status", readiness.get("level")),
        )
    )
    status = payloads.get("training_status")
    definitions.extend(
        (
            ("training_status", "daily_training_status", "boolean", "status", _nested(status, "latestTrainingStatusData") is not None),
            ("training_status", "daily_vo2max_running", "decimal", "millilitres_per_kilogram_minute", _nested(status, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue")),
        )
    )
    battery = payloads.get("body_battery")
    battery = battery[0] if isinstance(battery, list) and battery else {}
    definitions.extend(
        (
            ("body_battery", "daily_body_battery_charged", "integer", "points", battery.get("charged") if isinstance(battery, dict) else None),
            ("body_battery", "daily_body_battery_drained", "integer", "points", battery.get("drained") if isinstance(battery, dict) else None),
        )
    )
    observations: list[ControlledObservation] = []
    for endpoint, definition, value_type, unit, value in definitions:
        if endpoint not in payloads:
            continue
        if value_type == "integer" and not isinstance(value, bool):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
                value = int(value)
        observations.append(
            ControlledObservation(
                capture=CapturePointer(f"daily.{endpoint}", local_date.isoformat()),
                definition=definition,
                value_type=value_type,
                unit=unit,
                window_kind="calendar_day",
                method="source_reported",
                status="missing" if value is None else "observed",
                value=value,
                local_date=local_date,
                provenance={"provider": "garmin"},
            )
        )
    return observations


def _activity_session(activity_id: str, summary: Mapping[str, Any]) -> CollectedTrainingSession | None:
    local_start_value = _nested(summary, "summaryDTO", "startTimeLocal") or summary.get("startTimeLocal")
    if not isinstance(local_start_value, str):
        return None
    try:
        local_start = datetime.fromisoformat(local_start_value.replace(" ", "T"))
    except ValueError:
        return None
    activity_type = _nested(summary, "activityTypeDTO", "typeKey") or _nested(summary, "activityType", "typeKey")
    sport = {
        "running": "running",
        "trail_running": "running",
        "treadmill_running": "running",
        "track_running": "running",
        "virtual_running": "running",
    }.get(activity_type, str(activity_type or "unknown"))
    duration = _nested(summary, "summaryDTO", "duration") or summary.get("duration")
    distance = _nested(summary, "summaryDTO", "distance") or summary.get("distance")
    return CollectedTrainingSession(
        capture=CapturePointer("activity.summary", activity_id),
        local_date=local_start.date(),
        local_start=local_start,
        timing_precision="local_datetime",
        sport=sport,
        title=summary.get("activityName"),
        duration_value=duration if isinstance(duration, (int, float)) and duration > 0 else None,
        duration_unit="seconds" if isinstance(duration, (int, float)) and duration > 0 else None,
        duration_basis="source_reported" if isinstance(duration, (int, float)) and duration > 0 else None,
        distance_value=distance if isinstance(distance, (int, float)) and distance > 0 else None,
        distance_unit="metres" if isinstance(distance, (int, float)) and distance > 0 else None,
    )
