"""One-shot, fail-closed importer for frozen legacy profile file stores.

This is maintenance tooling, never a runtime adapter.  It has no defaults for
its source or PostgreSQL target, never loads dotenv, never decrypts legacy
integration material, and emits only privacy-safe categories, counts, and
allowlisted codes.

The transaction unit is one Profile.  Registry binding, source captures,
canonical projections, App Records, operational state, encrypted integration
material, and immutable import evidence commit together.  A failed Profile is
rolled back without undoing earlier completed Profiles; rerunning resumes from
the immutable evidence rows and verifies the target before reporting success.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import stat
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.types.json import Jsonb

from coach.data import (
    _coerce_goal_event_record,
    _coerce_plan_record,
    _coerce_session_record,
)
from coach.postgres._app_record_store import AppRecordStore
from coach.postgres._capture_store import (
    CaptureInput,
    CaptureStore,
    ObservationInput,
    RecordPointerInput,
    SessionLoadInput,
    TrainingSessionInput,
    _prepare_graph,
)
from coach.postgres.config import DatabaseSettings
from coach.postgres.encryption import EncryptedBlob
from collector.postgres_adapter import (
    _activity_session,
    _daily_observations,
    credential_bundle,
)

_IMPORTER_VERSION = 2
_REQUIRED_REVISION = "0008_file_import_units"
_PROFILE_RE = re.compile(r"^[0-9a-f]{32}$")
_APP_RE = re.compile(r"^app:([0-9a-f]{32})$")
_SAFE_CATEGORY = re.compile(r"^[a-z][a-z0-9_]*$")
_ALLOWED_JOB_STATES = {"running", "succeeded", "failed"}

# Resource limits are deliberately conservative for a one-athlete file store.
# They are enforced while descriptors are open, before JSON/base64 expansion.
MAX_PROFILES = 100
MAX_FILES = 10_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_AGGREGATE_BYTES = 256 * 1024 * 1024
MAX_TOKEN_ENTRIES = 64
MAX_TOKEN_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_CONTAINERS = 100_000

_OBSERVATION_DERIVED_FIELDS = {
    "daily_steps": "steps",
    "daily_distance": "distance_m",
    "daily_active_energy": "active_kcal",
    "daily_resting_heart_rate": "resting_hr",
    "daily_minimum_heart_rate": "min_hr",
    "daily_maximum_heart_rate": "max_hr",
    "daily_average_stress": "avg_stress",
    "daily_moderate_intensity_minutes": "intensity_min_moderate",
    "daily_vigorous_intensity_minutes": "intensity_min_vigorous",
    "daily_floors_ascended": "floors_ascended",
    "daily_sleep_duration": "sleep_seconds",
    "daily_sleep_score": "sleep_score",
    "daily_deep_sleep_duration": "deep_sleep_s",
    "daily_light_sleep_duration": "light_sleep_s",
    "daily_rem_sleep_duration": "rem_sleep_s",
    "daily_awake_sleep_duration": "awake_s",
    "nightly_hrv_average": "hrv_avg",
    "daily_hrv_status": "hrv_status",
    "daily_training_readiness": "training_readiness",
    "daily_training_readiness_level": "training_readiness_level",
    "daily_training_status": "training_status",
    "daily_vo2max_running": "vo2max_running",
    "daily_body_battery_charged": "body_battery_charged",
    "daily_body_battery_drained": "body_battery_drained",
}

_DERIVED_OBSERVATION_DEFINITIONS: dict[str, tuple[str, str]] = {
    "daily_steps": ("integer", "count"),
    "daily_distance": ("decimal", "metres"),
    "daily_active_energy": ("integer", "kilocalories"),
    "daily_resting_heart_rate": ("integer", "beats_per_minute"),
    "daily_minimum_heart_rate": ("integer", "beats_per_minute"),
    "daily_maximum_heart_rate": ("integer", "beats_per_minute"),
    "daily_average_stress": ("decimal", "score"),
    "daily_moderate_intensity_minutes": ("integer", "minutes"),
    "daily_vigorous_intensity_minutes": ("integer", "minutes"),
    "daily_floors_ascended": ("decimal", "floors"),
    "daily_sleep_duration": ("integer", "seconds"),
    "daily_sleep_score": ("decimal", "score"),
    "daily_deep_sleep_duration": ("integer", "seconds"),
    "daily_light_sleep_duration": ("integer", "seconds"),
    "daily_rem_sleep_duration": ("integer", "seconds"),
    "daily_awake_sleep_duration": ("integer", "seconds"),
    "nightly_hrv_average": ("decimal", "milliseconds"),
    "daily_hrv_status": ("text", "status"),
    "daily_training_readiness": ("decimal", "score"),
    "daily_training_readiness_level": ("text", "status"),
    "daily_training_status": ("boolean", "status"),
    "daily_vo2max_running": ("decimal", "millilitres_per_kilogram_minute"),
    "daily_body_battery_charged": ("integer", "points"),
    "daily_body_battery_drained": ("integer", "points"),
}

_CANONICAL_CAPTURE_PROVENANCE = {
    "provider": "legacy_canonical",
    "migration": "legacy_file_store",
    "history": "latest_available_only",
}
_RAW_CAPTURE_PROVENANCE = {
    "provider": "garmin",
    "migration": "legacy_file_store",
    "history": "latest_available_only",
}


class MaintenanceError(RuntimeError):
    """A sanitized fail-closed maintenance error."""

    def __init__(self, code: str) -> None:
        if not _SAFE_CATEGORY.fullmatch(code):
            code = "maintenance_failed"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Issue:
    category: str
    code: str
    count: int = 1

    def public(self) -> dict[str, Any]:
        return {"category": self.category, "code": self.code, "count": self.count}


@dataclass(slots=True, repr=False)
class SourceSnapshot:
    """A bounded file tree captured through no-follow directory descriptors."""

    absolute_root: Path
    files: dict[PurePosixPath, bytes]
    directories: set[PurePosixPath]

    def exists(self, relative: PurePosixPath | str) -> bool:
        key = PurePosixPath(relative)
        return key in self.files or key in self.directories

    def is_dir(self, relative: PurePosixPath | str) -> bool:
        return PurePosixPath(relative) in self.directories

    def read(self, relative: PurePosixPath | str) -> bytes:
        try:
            return self.files[PurePosixPath(relative)]
        except KeyError:
            raise FileNotFoundError from None

    def files_under(self, relative: PurePosixPath | str) -> list[PurePosixPath]:
        root = PurePosixPath(relative)
        prefix = root.parts
        return sorted(
            (path for path in self.files if path.parts[: len(prefix)] == prefix),
            key=lambda path: path.as_posix(),
        )

    def children(self, relative: PurePosixPath | str) -> set[PurePosixPath]:
        root = PurePosixPath(relative)
        prefix = root.parts
        depth = len(prefix) + 1
        values = set(self.files) | self.directories
        return {
            PurePosixPath(*path.parts[:depth])
            for path in values
            if len(path.parts) >= depth and path.parts[: len(prefix)] == prefix
        }


@dataclass(slots=True, repr=False)
class LegacyJob:
    state: str
    kind: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    safe_code: str | None
    request_key: str


@dataclass(slots=True, repr=False)
class LegacyProfile:
    identifier: UUID
    subject: str
    display_name: str | None
    created_at: datetime
    root: PurePosixPath
    manifest_hash: bytes
    captures: list[CaptureInput] = field(default_factory=list)
    sessions: list[TrainingSessionInput] = field(default_factory=list)
    observations: list[ObservationInput] = field(default_factory=list)
    app_sessions: list[dict[str, Any]] = field(default_factory=list)
    goal_events: list[dict[str, Any]] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    checkpoint: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    jobs: list[LegacyJob] = field(default_factory=list)
    legacy_credentials_ciphertext: bytes | None = None
    credential_bundle: bytes | None = None
    token_bundle: bytes | None = None
    derived_daily: dict[str, dict[str, Any]] = field(default_factory=dict)
    derived_activity_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    derived_activities: list[dict[str, Any]] = field(default_factory=list)
    derived_athlete: dict[str, Any] | None = None
    derived_timeline: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        jobs = len(self.jobs) + int(self.health is not None and not self.jobs)
        return {
            "profiles": 1,
            "sources": 1,
            "ingest_batches": 1,
            "collected_records": len(self.captures),
            "captures": len(self.captures),
            "observation_heads": len(self.captures),
            "collected_sessions": len(self.sessions),
            "collected_session_loads": sum(len(item.loads) for item in self.sessions),
            "observations": len(self.observations),
            "app_sessions": len(self.app_sessions),
            "app_session_loads": sum(
                len(item["content"].get("loads") or ()) for item in self.app_sessions
            ),
            "goal_events": len(self.goal_events),
            "training_plans": len(self.plans),
            "plan_revisions": sum(item["revision"] for item in self.plans),
            "planned_sessions": sum(
                len({session["id"] for revision in item["content"]["revisions"] for session in revision["plan"]["planned_sessions"]})
                for item in self.plans
            ),
            "plan_goal_references": sum(
                len(revision["plan"]["goal_events"])
                for item in self.plans for revision in item["content"]["revisions"]
            ),
            "plan_constraints": sum(
                len(revision["plan"]["constraints"])
                for item in self.plans for revision in item["content"]["revisions"]
            ),
            "plan_session_snapshots": sum(
                len(revision["plan"]["planned_sessions"])
                for item in self.plans for revision in item["content"]["revisions"]
            ),
            "plan_revision_matches": sum(
                len(session["matches"])
                for item in self.plans
                for revision in item["content"]["revisions"]
                for session in revision["plan"]["planned_sessions"]
            ),
            "current_matches": sum(
                len(session["matches"])
                for item in self.plans
                for session in item["content"]["revisions"][-1]["plan"]["planned_sessions"]
            ),
            "checkpoints": int(self.checkpoint is not None and _health_times(self.health)[2] is not None),
            "health": int(self.health is not None),
            "jobs": jobs,
            "job_runs": jobs,
            "job_attempts": jobs,
            "credential_bundles": int(self.legacy_credentials_ciphertext is not None),
            "token_bundles": int(self.token_bundle is not None),
            "import_units": 1,
        }


@dataclass(slots=True, repr=False)
class Inventory:
    profiles: list[LegacyProfile]
    issues: list[Issue]

    def counts(self) -> dict[str, int]:
        result: Counter[str] = Counter()
        for profile in self.profiles:
            result.update(profile.counts())
        return dict(sorted(result.items()))

    def all_issues(self) -> list[Issue]:
        return _coalesce_issues([*self.issues, *(issue for p in self.profiles for issue in p.issues)])


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    operation: str
    counts: Mapping[str, tuple[int, int | None]]
    issues: Sequence[Issue]

    @property
    def ok(self) -> bool:
        return not self.issues

    def public(self) -> dict[str, Any]:
        rows = []
        for category, (source, target) in sorted(self.counts.items()):
            row: dict[str, Any] = {"category": category, "source": source}
            if target is not None:
                row["target"] = target
            rows.append(row)
        return {
            "operation": self.operation,
            "ok": self.ok,
            "counts": rows,
            "issues": [issue.public() for issue in _coalesce_issues(self.issues)],
        }


FailureHook = Callable[[str, int], None]


def inventory_store(source_root: Path, clerk_issuer: str) -> Inventory:
    """Read one bounded, descriptor-frozen snapshot of the supplied store."""
    snapshot = _snapshot_source(source_root)
    if not isinstance(clerk_issuer, str) or not clerk_issuer.strip():
        raise MaintenanceError("invalid_actor_issuer")
    registry = _read_snapshot_json(snapshot, PurePosixPath("profiles/registry.json"), dict)
    if registry is None or registry.get("schema_version") != 1 or not isinstance(
        registry.get("profiles"), dict
    ):
        raise MaintenanceError("invalid_registry")

    issues: list[Issue] = []
    profiles: list[LegacyProfile] = []
    subjects: Counter[str] = Counter()
    registry_rows = registry["profiles"]
    if len(registry_rows) > MAX_PROFILES:
        raise MaintenanceError("source_limit_exceeded")
    ordered = sorted(
        registry_rows.items(),
        key=lambda item: (item[1].get("seq", 0) if isinstance(item[1], dict) else 0, item[0]),
    )
    expected_profile_roots: set[str] = set()
    for encoded_id, row in ordered:
        if not isinstance(row, dict) or not _PROFILE_RE.fullmatch(encoded_id):
            issues.append(Issue("profile", "invalid_identity"))
            continue
        expected_profile_roots.add(encoded_id)
        subject = row.get("clerk_subject")
        if not isinstance(subject, str) or not subject:
            issues.append(Issue("profile", "invalid_binding"))
            continue
        subjects[subject] += 1
        try:
            created_at = _timestamp(row.get("created_at"))
        except MaintenanceError:
            issues.append(Issue("profile", "invalid_timestamp"))
            continue
        profile_root = PurePosixPath("profiles") / encoded_id
        if not snapshot.is_dir(profile_root):
            issues.append(Issue("profile", "missing_store"))
            continue
        profiles.append(
            _inventory_profile(
                snapshot,
                profile_root,
                UUID(hex=encoded_id),
                subject,
                row.get("display_name") if isinstance(row.get("display_name"), str) else None,
                created_at,
                row,
            )
        )

    actual_profile_roots = {
        child.name
        for child in snapshot.children("profiles")
        if snapshot.is_dir(child) and _PROFILE_RE.fullmatch(child.name)
    }
    extras = len(actual_profile_roots - expected_profile_roots)
    if extras:
        issues.append(Issue("profile", "orphan_store", extras))
    duplicates = sum(count - 1 for count in subjects.values() if count > 1)
    if duplicates:
        issues.append(Issue("profile", "duplicate_binding", duplicates))

    expected_jobs = {
        hashlib.sha256(profile.subject.encode("utf-8")).hexdigest()[:32]
        for profile in profiles
    }
    job_files = snapshot.files_under("jobs") if snapshot.is_dir("jobs") else []
    orphan_jobs = sum(
        path.suffix == ".json" and path.stem not in expected_jobs for path in job_files
    )
    if orphan_jobs:
        issues.append(Issue("job", "orphan_reference", orphan_jobs))

    _validate_references(profiles)
    return Inventory(profiles, issues)


def import_store(
    source_root: Path,
    settings: DatabaseSettings,
    clerk_issuer: str,
    encryption_key: bytes,
    legacy_source_key: bytes | None = None,
    *,
    failure_hook: FailureHook | None = None,
) -> MaintenanceReport:
    """Import each Profile as one explicit owner transaction and reconcile it."""
    inventory = inventory_store(source_root, clerk_issuer)
    blockers = inventory.all_issues()
    if blockers:
        return MaintenanceReport(
            "import", {key: (value, None) for key, value in inventory.counts().items()}, blockers
        )
    _validate_fernet_key(encryption_key, "invalid_encryption_key")
    _prepare_integration_material(inventory, legacy_source_key)

    capture_store = CaptureStore(settings)
    try:
        with psycopg.connect(settings.url, autocommit=True) as connection:
            _require_schema(connection)
            _require_maintenance_owner(connection)
            for ordinal, profile in enumerate(inventory.profiles):
                with connection.transaction():
                    existing = connection.execute(
                        "SELECT source_manifest_hash, importer_version "
                        "FROM file_import_units WHERE profile_id = %s",
                        (profile.identifier,),
                    ).fetchone()
                    if existing is not None:
                        if existing != (profile.manifest_hash, _IMPORTER_VERSION):
                            raise MaintenanceError("source_manifest_changed")
                        if failure_hook is not None:
                            failure_hook("verified_unit", ordinal)
                        continue
                    _import_profile(
                        connection,
                        capture_store,
                        profile,
                        clerk_issuer.strip(),
                        encryption_key,
                        ordinal,
                        failure_hook,
                    )
                    # The descriptor-frozen source is opened and inventoried
                    # again at the final evidence boundary. Any writer change,
                    # including an unsupported file, aborts this Profile unit.
                    fresh = inventory_store(source_root, clerk_issuer)
                    fresh_profile = next(
                        (item for item in fresh.profiles if item.identifier == profile.identifier),
                        None,
                    )
                    if (
                        fresh.all_issues()
                        or fresh_profile is None
                        or fresh_profile.manifest_hash != profile.manifest_hash
                    ):
                        raise MaintenanceError("source_manifest_changed")
                    connection.execute(
                        """
                        INSERT INTO file_import_units (
                            profile_id, importer_version, source_manifest_hash, inventory_counts
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            profile.identifier,
                            _IMPORTER_VERSION,
                            profile.manifest_hash,
                            Jsonb(profile.counts()),
                        ),
                    )
                    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    if failure_hook is not None:
                        failure_hook("complete", ordinal)
    except MaintenanceError:
        raise
    except Exception:
        raise MaintenanceError("database_import_failed") from None
    return reconcile_store(
        source_root,
        settings,
        clerk_issuer,
        encryption_key,
        legacy_source_key,
    )


def reconcile_store(
    source_root: Path,
    settings: DatabaseSettings,
    clerk_issuer: str,
    encryption_key: bytes | None = None,
    legacy_source_key: bytes | None = None,
) -> MaintenanceReport:
    """Compare complete source and PostgreSQL sets; emit sanitized aggregates."""
    inventory = inventory_store(source_root, clerk_issuer)
    _prepare_integration_material(inventory, legacy_source_key)
    if encryption_key is not None:
        _validate_fernet_key(encryption_key, "invalid_encryption_key")
    issues = list(inventory.all_issues())
    expected_counts = inventory.counts()
    target_counts: Counter[str] = Counter()
    try:
        with psycopg.connect(settings.url) as connection:
            _require_schema(connection)
            global_issues, global_counts = _reconcile_global_roots(
                connection, inventory, clerk_issuer.strip()
            )
            issues.extend(global_issues)
            target_counts.update(global_counts)
            for profile in inventory.profiles:
                profile_issues, counts = _reconcile_profile(
                    connection,
                    profile,
                    clerk_issuer.strip(),
                    encryption_key,
                )
                issues.extend(profile_issues)
                target_counts.update(counts)
    except MaintenanceError:
        raise
    except Exception:
        raise MaintenanceError("database_reconciliation_failed") from None

    for category, source_count in expected_counts.items():
        target_count = target_counts.get(category, 0)
        if source_count != target_count:
            issues.append(Issue(category, "count_mismatch", abs(source_count - target_count)))
    counts = {
        category: (expected_counts.get(category, 0), target_counts.get(category, 0))
        for category in sorted(set(expected_counts) | set(target_counts))
    }
    return MaintenanceReport("shadow", counts, _coalesce_issues(issues))


def _inventory_profile(
    snapshot: SourceSnapshot,
    root: PurePosixPath,
    identifier: UUID,
    subject: str,
    display_name: str | None,
    created_at: datetime,
    registry_row: Mapping[str, Any],
) -> LegacyProfile:
    profile = LegacyProfile(identifier, subject, display_name, created_at, root, b"")
    digest = hashlib.sha256(
        json.dumps(
            {"id": identifier.hex, "row": registry_row},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    for path in snapshot.files_under(root):
        relative = path.relative_to(root)
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(snapshot.read(path))
    job_key = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]
    job_path = PurePosixPath("jobs") / f"{job_key}.json"
    if job_path in snapshot.files:
        digest.update(b"job/current\0" + snapshot.read(job_path))

    raw_payloads: dict[tuple[str, str], Any] = {}
    raw_root = root / "raw"
    for path in snapshot.files_under(raw_root):
        if path.suffix != ".json":
            profile.issues.append(Issue("capture", "unsupported_entry"))
            continue
        payload = _decode_json(snapshot.read(path))
        if payload is _INVALID:
            profile.issues.append(Issue("capture", "invalid_json"))
            continue
        kind, key = _raw_identity(path.relative_to(raw_root))
        identity = (kind, key)
        if identity in raw_payloads:
            profile.issues.append(Issue("capture", "duplicate_identity"))
            continue
        raw_payloads[identity] = payload
        _add_capture(profile, kind, key, payload, _RAW_CAPTURE_PROVENANCE)

    daily_payloads: dict[str, dict[str, Any]] = defaultdict(dict)
    for (kind, key), payload in raw_payloads.items():
        if kind.startswith("daily.") and kind != "daily._complete":
            daily_payloads[key][kind.removeprefix("daily.")] = payload
        if kind == "activity.summary" and isinstance(payload, dict):
            projected = _activity_session(key, payload)
            if projected is not None:
                profile.sessions.append(_session_input(projected))
    for iso, payloads in sorted(daily_payloads.items()):
        try:
            local_date = date.fromisoformat(iso)
        except ValueError:
            profile.issues.append(Issue("observation", "invalid_identity"))
            continue
        profile.observations.extend(
            _observation_input(observation)
            for observation in _daily_observations(local_date, payloads)
        )

    derived_root = root / "derived"
    for path in snapshot.files_under(derived_root):
        relative = path.relative_to(derived_root)
        content = snapshot.read(path)
        if relative.as_posix() == "timeline.jsonl":
            rows: list[dict[str, Any]] = []
            for line in content.splitlines():
                if not line.strip():
                    continue
                value = _decode_json(line)
                if not isinstance(value, dict):
                    profile.issues.append(Issue("projection", "invalid_json"))
                    continue
                rows.append(value)
            profile.derived_timeline = rows
            continue
        if path.suffix != ".json":
            profile.issues.append(Issue("projection", "unsupported_entry"))
            continue
        payload = _decode_json(content)
        if payload is _INVALID:
            profile.issues.append(Issue("projection", "invalid_json"))
            continue
        if len(relative.parts) == 2 and relative.parts[0] == "daily" and isinstance(payload, dict):
            profile.derived_daily[path.stem] = payload
        elif relative.as_posix() == "activities.json" and isinstance(payload, list):
            profile.derived_activities = [item for item in payload if isinstance(item, dict)]
            if len(profile.derived_activities) != len(payload):
                profile.issues.append(Issue("projection", "invalid_record"))
        elif len(relative.parts) == 2 and relative.parts[0] == "activities" and isinstance(payload, dict):
            profile.derived_activity_files[path.stem] = payload
        elif relative.as_posix() == "athlete.json" and isinstance(payload, dict):
            profile.derived_athlete = payload
        else:
            profile.issues.append(Issue("projection", "unsupported_entry"))

    activity_keys = [str(item.get("activity_id")) for item in profile.derived_activities]
    duplicated_activities = sum(
        count - 1 for count in Counter(activity_keys).values() if count > 1
    )
    if duplicated_activities:
        profile.issues.append(Issue("projection", "duplicate_identity", duplicated_activities))
    _check_derived_projection(profile, raw_payloads, daily_payloads)
    _reconstruct_canonical_authority(profile, raw_payloads, daily_payloads)

    _inventory_app_records(snapshot, profile)
    _inventory_operational_state(snapshot, profile)
    _inventory_integration_material(snapshot, profile)
    profile.manifest_hash = digest.digest()
    return profile


def _inventory_app_records(snapshot: SourceSnapshot, profile: LegacyProfile) -> None:
    app_root = profile.root / "app"
    kinds = {
        "sessions": (_coerce_session_record, profile.app_sessions),
        "goal_events": (_coerce_goal_event_record, profile.goal_events),
        "plans": (_coerce_plan_record, profile.plans),
    }
    known_paths: set[PurePosixPath] = set()
    for subdir, (coerce, destination) in kinds.items():
        directory = app_root / subdir
        for path in snapshot.files_under(directory):
            known_paths.add(path)
            if path.suffix != ".json" or not re.fullmatch(r"[0-9a-f]{32}", path.stem):
                profile.issues.append(Issue("app_record", "invalid_identity"))
                continue
            payload = _decode_json(snapshot.read(path))
            if not isinstance(payload, dict):
                profile.issues.append(Issue("app_record", "invalid_json"))
                continue
            try:
                record = coerce(payload, f"app:{path.stem}")
                if record.get("provenance") != {"source": "manual"}:
                    profile.issues.append(Issue("app_record", "unsupported_provenance"))
                    continue
                destination.append(record)
            except Exception:
                profile.issues.append(Issue("app_record", "invalid_record"))
    for path in snapshot.files_under(app_root):
        if path not in known_paths:
            profile.issues.append(Issue("app_record", "unsupported_entry"))


def _inventory_operational_state(
    snapshot: SourceSnapshot, profile: LegacyProfile
) -> None:
    state_path = profile.root / "index/state.json"
    legacy_state: dict[str, Any] | None = None
    if state_path in snapshot.files:
        state = _decode_json(snapshot.read(state_path))
        if isinstance(state, dict):
            legacy_state = state
        else:
            profile.issues.append(Issue("checkpoint", "invalid_json"))
    health_path = profile.root / "index/collector-health.json"
    if health_path in snapshot.files:
        health = _decode_json(snapshot.read(health_path))
        if isinstance(health, dict) and health.get("schema_version") == 1:
            profile.health = health
            active = _active_health_states(health)
            if active:
                profile.issues.append(Issue("health", "active_writer", active))
        else:
            profile.issues.append(Issue("health", "invalid_record"))

    operational_cursor: dict[str, Any] = {}
    if legacy_state is not None:
        operational_cursor["legacy_state"] = legacy_state
    if profile.health is not None:
        operational_cursor["legacy_health"] = profile.health

    key = hashlib.sha256(profile.subject.encode("utf-8")).hexdigest()[:32]
    job_path = PurePosixPath("jobs") / f"{key}.json"
    if job_path in snapshot.files:
        payload = _decode_json(snapshot.read(job_path))
        try:
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise MaintenanceError("invalid_job")
            state = payload.get("state")
            action = payload.get("action")
            if state not in _ALLOWED_JOB_STATES or action not in {"connect", "refresh"}:
                raise MaintenanceError("invalid_job")
            kind = "initial_sync" if action == "connect" else "incremental"
            started = _timestamp(payload.get("started_at") or payload.get("updated_at"))
            finished = None if state == "running" else _timestamp(
                payload.get("finished_at") or payload.get("updated_at")
            )
            profile.jobs.append(
                LegacyJob(
                    state,
                    kind,
                    started,
                    started,
                    finished,
                    None if state in {"succeeded", "running"} else _safe_job_code(payload.get("error")),
                    f"legacy-job:{key}",
                )
            )
            operational_cursor["legacy_job"] = payload
            if state == "running" or finished is None:
                profile.issues.append(Issue("job", "active_writer"))
        except MaintenanceError:
            profile.issues.append(Issue("job", "invalid_record"))
    if operational_cursor:
        profile.checkpoint = operational_cursor


def _inventory_integration_material(
    snapshot: SourceSnapshot, profile: LegacyProfile
) -> None:
    credentials = profile.root / "secrets/garmin.enc"
    if credentials in snapshot.files:
        content = snapshot.read(credentials)
        if content:
            profile.legacy_credentials_ciphertext = content
        else:
            profile.issues.append(Issue("integration", "invalid_material"))
    tokens = profile.root / "garmin-tokens"
    token_paths = snapshot.files_under(tokens)
    if len(token_paths) > MAX_TOKEN_ENTRIES:
        raise MaintenanceError("token_limit_exceeded")
    if token_paths:
        files: dict[str, str] = {}
        raw_total = 0
        for path in token_paths:
            relative = path.relative_to(tokens)
            content = snapshot.read(path)
            raw_total += len(content)
            if len(relative.parts) != 1 or not relative.name or relative.name == "tokens.bundle":
                profile.issues.append(Issue("integration", "unsupported_entry"))
                continue
            files[relative.name] = base64.b64encode(content).decode("ascii")
        if raw_total > MAX_TOKEN_BUNDLE_BYTES:
            raise MaintenanceError("token_limit_exceeded")
        if files:
            bundle = json.dumps(
                {
                    "schema_version": 1,
                    "initial_days": 365,
                    "activity_limit": 100,
                    "files": files,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(bundle) > MAX_TOKEN_BUNDLE_BYTES:
                raise MaintenanceError("token_limit_exceeded")
            _validate_token_bundle(bundle)
            profile.token_bundle = bundle


def _validate_fernet_key(value: bytes, code: str) -> None:
    try:
        Fernet(value)
    except (TypeError, ValueError):
        raise MaintenanceError(code) from None


def _validate_token_bundle(bundle: bytes) -> None:
    try:
        payload = json.loads(bundle.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MaintenanceError("invalid_token_material") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise MaintenanceError("invalid_token_material")
    initial_days = payload.get("initial_days")
    activity_limit = payload.get("activity_limit")
    files = payload.get("files")
    if (
        isinstance(initial_days, bool)
        or not isinstance(initial_days, int)
        or not 1 <= initial_days <= 730
        or isinstance(activity_limit, bool)
        or not isinstance(activity_limit, int)
        or not 1 <= activity_limit <= 500
        or not isinstance(files, dict)
        or not files
        or len(files) > MAX_TOKEN_ENTRIES
    ):
        raise MaintenanceError("invalid_token_material")
    total = 0
    for name, encoded in files.items():
        if (
            not isinstance(name, str)
            or not name
            or name in {".", "..", "tokens.bundle"}
            or "/" in name
            or "\\" in name
            or not name.isprintable()
            or not isinstance(encoded, str)
        ):
            raise MaintenanceError("invalid_token_material")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise MaintenanceError("invalid_token_material") from None
        total += len(decoded)
        if total > MAX_TOKEN_BUNDLE_BYTES:
            raise MaintenanceError("token_limit_exceeded")


def _prepare_integration_material(
    inventory: Inventory, legacy_source_key: bytes | None
) -> None:
    if legacy_source_key is not None:
        # Private legacy credentials are preserved as opaque evidence rather
        # than decrypted by this maintenance boundary.
        raise MaintenanceError("legacy_key_not_supported")
    for profile in inventory.profiles:
        profile.credential_bundle = None
        if profile.legacy_credentials_ciphertext is not None:
            profile.credential_bundle = json.dumps(
                {
                    "schema_version": 1,
                    "kind": "legacy_fernet_ciphertext",
                    "ciphertext": base64.b64encode(
                        profile.legacy_credentials_ciphertext
                    ).decode("ascii"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        if profile.token_bundle is not None:
            _validate_token_bundle(profile.token_bundle)


def _validate_references(profiles: Sequence[LegacyProfile]) -> None:
    app_sessions: dict[str, UUID] = {}
    goals: dict[str, UUID] = {}
    collected: dict[str, set[UUID]] = defaultdict(set)
    for profile in profiles:
        for record in profile.app_sessions:
            app_sessions[record["id"]] = profile.identifier
        for record in profile.goal_events:
            goals[record["id"]] = profile.identifier
        for session in profile.sessions:
            collected[session.capture.source_key].add(profile.identifier)

    for profile in profiles:
        for plan in profile.plans:
            for revision in plan["content"]["revisions"]:
                snapshot = revision["plan"]
                for ref in snapshot["goal_events"]:
                    owner = goals.get(ref["goal_event_id"])
                    if owner is None:
                        profile.issues.append(Issue("reference", "missing_target"))
                    elif owner != profile.identifier:
                        profile.issues.append(Issue("isolation", "cross_profile_reference"))
                for planned in snapshot["planned_sessions"]:
                    for match in planned["matches"]:
                        if match.startswith("app:"):
                            owner = app_sessions.get(match)
                            if owner is None:
                                profile.issues.append(Issue("reference", "missing_target"))
                            elif owner != profile.identifier:
                                profile.issues.append(Issue("isolation", "cross_profile_reference"))
                        elif match.startswith("garmin:"):
                            owners = collected.get(match.removeprefix("garmin:"), set())
                            if profile.identifier not in owners:
                                profile.issues.append(
                                    Issue(
                                        "isolation" if owners else "reference",
                                        "cross_profile_reference" if owners else "missing_target",
                                    )
                                )
                        else:
                            profile.issues.append(Issue("reference", "invalid_identity"))


def _check_derived_projection(
    profile: LegacyProfile,
    raw_payloads: Mapping[tuple[str, str], Any],
    daily_payloads: Mapping[str, Mapping[str, Any]],
) -> None:
    """Validate derived caches when present; absence is safely regenerable."""
    mismatches = 0
    for iso, payloads in daily_payloads.items():
        actual = profile.derived_daily.get(iso)
        if actual is not None and _json_semantics(actual) != _json_semantics(
            _normalized_daily_record(iso, payloads)
        ):
            mismatches += 1

    timeline_by_date: dict[str, dict[str, Any]] = {}
    for row in profile.derived_timeline:
        key = row.get("date")
        if not isinstance(key, str) or key in timeline_by_date:
            profile.issues.append(Issue("projection", "duplicate_identity"))
            continue
        timeline_by_date[key] = row
    for iso in set(profile.derived_daily) & set(timeline_by_date):
        if _json_semantics(profile.derived_daily[iso]) != _json_semantics(
            timeline_by_date[iso]
        ):
            mismatches += 1

    index: dict[str, dict[str, Any]] = {}
    for item in profile.derived_activities:
        key = str(item.get("activity_id"))
        if key not in index:
            index[key] = item
    raw_summaries = {
        key: payload
        for (kind, key), payload in raw_payloads.items()
        if kind == "activity.summary" and isinstance(payload, Mapping)
    }
    for key, payload in raw_summaries.items():
        expected = _normalized_activity_record(key, payload)
        individual = profile.derived_activity_files.get(key)
        listed = index.get(key)
        if individual is not None and _json_semantics(individual) != _json_semantics(expected):
            mismatches += 1
        if listed is not None and _json_semantics(listed) != _json_semantics(expected):
            mismatches += 1
    for key in set(profile.derived_activity_files) & set(index):
        individual = profile.derived_activity_files[key]
        if _json_semantics(individual) != _json_semantics(index[key]):
            mismatches += 1
        if str(individual.get("activity_id")) != key:
            mismatches += 1

    profile_snapshots = sorted(
        key for (kind, key) in raw_payloads if kind.startswith("profile.")
    )
    if profile_snapshots and profile.derived_athlete is not None:
        expected_athlete = _normalized_athlete_record(
            raw_payloads, profile_snapshots[-1]
        )
        if _json_semantics(profile.derived_athlete) != _json_semantics(expected_athlete):
            mismatches += 1
    if mismatches:
        profile.issues.append(Issue("projection", "projection_mismatch", mismatches))


def _reconstruct_canonical_authority(
    profile: LegacyProfile,
    raw_payloads: Mapping[tuple[str, str], Any],
    daily_payloads: Mapping[str, Mapping[str, Any]],
) -> None:
    timeline_by_date = {
        str(row.get("date")): row
        for row in profile.derived_timeline
        if isinstance(row.get("date"), str)
    }
    for iso in sorted(set(profile.derived_daily) | set(timeline_by_date)):
        if iso in daily_payloads:
            continue
        record = profile.derived_daily.get(iso) or timeline_by_date[iso]
        try:
            local_date = date.fromisoformat(iso)
        except ValueError:
            profile.issues.append(Issue("observation", "invalid_identity"))
            continue
        pointer_kind = (
            "migration.derived_daily"
            if iso in profile.derived_daily
            else "migration.derived_timeline_day"
        )
        _add_capture(
            profile,
            pointer_kind,
            iso,
            record,
            _CANONICAL_CAPTURE_PROVENANCE,
        )
        for definition, field_name in _OBSERVATION_DERIVED_FIELDS.items():
            if field_name not in record:
                continue
            value_type, unit = _DERIVED_OBSERVATION_DEFINITIONS[definition]
            value = record[field_name]
            profile.observations.append(
                ObservationInput(
                    RecordPointerInput(pointer_kind, iso),
                    definition,
                    value_type,
                    unit,
                    "calendar_day",
                    "source_reported",
                    "missing" if value is None else "observed",
                    value,
                    local_date,
                    provenance={"provider": "legacy_canonical"},
                )
            )

    raw_activity_keys = {
        key for kind, key in raw_payloads if kind == "activity.summary"
    }
    index = {
        str(item.get("activity_id")): item
        for item in profile.derived_activities
        if item.get("activity_id") is not None
    }
    for key in sorted(set(profile.derived_activity_files) | set(index)):
        if key in raw_activity_keys:
            continue
        if key in profile.derived_activity_files:
            pointer = RecordPointerInput("migration.derived_activity", key)
            record = profile.derived_activity_files[key]
        else:
            pointer = RecordPointerInput("migration.derived_activity_index_item", key)
            record = index[key]
        _add_capture(
            profile,
            pointer.record_kind,
            pointer.source_key,
            record,
            _CANONICAL_CAPTURE_PROVENANCE,
        )
        session = _canonical_activity_session(pointer, record)
        if session is None:
            profile.issues.append(Issue("collected_session", "invalid_record"))
        else:
            profile.sessions.append(session)


def _add_capture(
    profile: LegacyProfile,
    record_kind: str,
    source_key: str,
    payload: Any,
    provenance: Mapping[str, Any],
) -> None:
    if not source_key:
        profile.issues.append(Issue("capture", "invalid_identity"))
        return
    if any(
        item.record_kind == record_kind and item.source_key == source_key
        for item in profile.captures
    ):
        profile.issues.append(Issue("capture", "duplicate_identity"))
        return
    profile.captures.append(
        CaptureInput(record_kind, source_key, payload, provenance=dict(provenance))
    )


def _normalized_daily_record(
    iso: str, payloads: Mapping[str, Any]
) -> dict[str, Any]:
    stats = payloads.get("stats") if isinstance(payloads.get("stats"), dict) else {}
    sleep = payloads.get("sleep") if isinstance(payloads.get("sleep"), dict) else {}
    sleep_dto = _nested(sleep, "dailySleepDTO") or {}
    hrv = payloads.get("hrv") if isinstance(payloads.get("hrv"), dict) else {}
    readiness = payloads.get("training_readiness")
    if isinstance(readiness, list):
        readiness = readiness[0] if readiness else {}
    readiness = readiness if isinstance(readiness, dict) else {}
    status = payloads.get("training_status") if isinstance(payloads.get("training_status"), dict) else {}
    record: dict[str, Any] = {
        "date": iso,
        "steps": stats.get("totalSteps"),
        "distance_m": stats.get("totalDistanceMeters"),
        "active_kcal": stats.get("activeKilocalories"),
        "resting_hr": stats.get("restingHeartRate"),
        "min_hr": stats.get("minHeartRate"),
        "max_hr": stats.get("maxHeartRate"),
        "avg_stress": stats.get("averageStressLevel"),
        "intensity_min_moderate": stats.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": stats.get("vigorousIntensityMinutes"),
        "floors_ascended": stats.get("floorsAscended"),
        "sleep_seconds": sleep_dto.get("sleepTimeSeconds"),
        "sleep_score": _nested(sleep_dto, "sleepScores", "overall", "value"),
        "deep_sleep_s": sleep_dto.get("deepSleepSeconds"),
        "light_sleep_s": sleep_dto.get("lightSleepSeconds"),
        "rem_sleep_s": sleep_dto.get("remSleepSeconds"),
        "awake_s": sleep_dto.get("awakeSleepSeconds"),
        "hrv_avg": _nested(hrv, "hrvSummary", "lastNightAvg"),
        "hrv_status": _nested(hrv, "hrvSummary", "status"),
        "training_readiness": readiness.get("score"),
        "training_readiness_level": readiness.get("level"),
        "training_status": _nested(status, "latestTrainingStatusData") is not None,
        "vo2max_running": _nested(status, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue"),
    }
    battery = payloads.get("body_battery")
    if isinstance(battery, list) and battery:
        first = battery[0]
        if isinstance(first, dict):
            record["body_battery_charged"] = first.get("charged")
            record["body_battery_drained"] = first.get("drained")
    return record


def _normalized_activity_record(key: str, summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "activity_id": int(key) if key.isdigit() else key,
        "name": summary.get("activityName"),
        "type": _nested(summary, "activityTypeDTO", "typeKey") or _nested(summary, "activityType", "typeKey"),
        "start_local": _nested(summary, "summaryDTO", "startTimeLocal") or summary.get("startTimeLocal"),
        "distance_m": _nested(summary, "summaryDTO", "distance") or summary.get("distance"),
        "duration_s": _nested(summary, "summaryDTO", "duration") or summary.get("duration"),
        "avg_hr": _nested(summary, "summaryDTO", "averageHR") or summary.get("averageHR"),
        "max_hr": _nested(summary, "summaryDTO", "maxHR") or summary.get("maxHR"),
        "elevation_gain_m": _nested(summary, "summaryDTO", "elevationGain") or summary.get("elevationGain"),
        "avg_speed_mps": _nested(summary, "summaryDTO", "averageSpeed") or summary.get("averageSpeed"),
        "calories": _nested(summary, "summaryDTO", "calories"),
        "training_effect_aerobic": _nested(summary, "summaryDTO", "trainingEffect"),
        "training_effect_anaerobic": _nested(summary, "summaryDTO", "anaerobicTrainingEffect"),
    }


def _normalized_athlete_record(
    raw_payloads: Mapping[tuple[str, str], Any], iso: str
) -> dict[str, Any]:
    profile = raw_payloads.get(("profile.user_profile", iso))
    profile = profile if isinstance(profile, Mapping) else {}
    user = profile.get("userData") if isinstance(profile.get("userData"), Mapping) else {}
    name = raw_payloads.get(("profile.full_name", iso))
    name = name if isinstance(name, Mapping) else {}
    personal = raw_payloads.get(("profile.personal_records", iso))
    return {
        "snapshot_date": iso,
        "full_name": name.get("fullName"),
        "user_profile_id": profile.get("id"),
        "gender": user.get("gender"),
        "birth_date": user.get("birthDate"),
        "weight_g": user.get("weight"),
        "height_cm": user.get("height"),
        "vo2max_running": user.get("vo2MaxRunning"),
        "vo2max_cycling": user.get("vo2MaxCycling"),
        "lactate_threshold_speed_mps": user.get("lactateThresholdSpeed"),
        "lactate_threshold_hr": user.get("lactateThresholdHeartRate"),
        "available_training_days": user.get("availableTrainingDays"),
        "preferred_long_training_days": user.get("preferredLongTrainingDays"),
        "personal_records_count": len(personal) if isinstance(personal, list) else None,
    }


def _canonical_activity_session(
    pointer: RecordPointerInput, record: Mapping[str, Any]
) -> TrainingSessionInput | None:
    start = record.get("start_local")
    if not isinstance(start, str):
        return None
    try:
        local_start = datetime.fromisoformat(start.replace(" ", "T"))
    except ValueError:
        return None
    activity_type = record.get("type")
    sport = {
        "running": "running",
        "trail_running": "running",
        "treadmill_running": "running",
        "track_running": "running",
        "virtual_running": "running",
    }.get(activity_type, str(activity_type or "unknown"))
    duration = record.get("duration_s")
    distance = record.get("distance_m")
    valid_duration = isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0
    valid_distance = isinstance(distance, (int, float)) and not isinstance(distance, bool) and distance > 0
    return TrainingSessionInput(
        pointer,
        local_start.date(),
        sport,
        "local_datetime",
        local_start,
        title=record.get("name") if isinstance(record.get("name"), str) else None,
        duration_value=duration if valid_duration else None,
        duration_unit="seconds" if valid_duration else None,
        duration_basis="source_reported" if valid_duration else None,
        distance_value=distance if valid_distance else None,
        distance_unit="metres" if valid_distance else None,
    )


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _json_semantics(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_semantics(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_json_semantics(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MaintenanceError("invalid_json_number")
        if value == 0:
            return 0
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MaintenanceError("invalid_json_number")
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def _import_profile(
    connection: psycopg.Connection,
    capture_store: CaptureStore,
    profile: LegacyProfile,
    clerk_issuer: str,
    encryption_key: bytes,
    ordinal: int,
    failure_hook: FailureHook | None,
) -> None:
    # Revision 0008 recognizes this owner-only transaction-local mode solely
    # in the command-time head/current-goal policy trigger. All FKs, immutable
    # history, revision, snapshot, fulfilment, and isolation checks stay live.
    connection.execute("SET LOCAL garmin_coach.maintenance_reconstruction = 'on'")
    _insert_profile(connection, profile, clerk_issuer)
    source_id = _source_id(profile.identifier)
    _insert_source(connection, profile, source_id, encryption_key)
    if failure_hook is not None:
        failure_hook("binding", ordinal)

    capture_store.ingest_graph(
        profile.identifier,
        source_id,
        idempotency_key=f"legacy-file-import:{_IMPORTER_VERSION}",
        captures=profile.captures,
        sessions=profile.sessions,
        observations=profile.observations,
        connection=connection,
    )
    if failure_hook is not None:
        failure_hook("collected", ordinal)

    for record in profile.app_sessions:
        _insert_app_session(connection, profile.identifier, record)
    for record in profile.goal_events:
        _insert_goal_event(connection, profile.identifier, record)
    for record in profile.plans:
        _insert_plan(connection, profile.identifier, record)
    _validate_reconstructed_plan_references(connection, profile)
    if failure_hook is not None:
        failure_hook("app_records", ordinal)

    _insert_operational_state(connection, profile, source_id)
    if failure_hook is not None:
        failure_hook("operational", ordinal)


def _insert_profile(
    connection: psycopg.Connection, profile: LegacyProfile, clerk_issuer: str
) -> None:
    row = connection.execute(
        "SELECT id, clerk_issuer, clerk_subject, display_name, created_at, revision "
        "FROM profiles WHERE id = %s OR (clerk_issuer = %s AND clerk_subject = %s)",
        (profile.identifier, clerk_issuer, profile.subject),
    ).fetchone()
    expected = (
        profile.identifier,
        clerk_issuer,
        profile.subject,
        profile.display_name,
        profile.created_at,
        0,
    )
    if row is None:
        connection.execute(
            """
            INSERT INTO profiles (
                id, clerk_issuer, clerk_subject, display_name, created_at, revision
            ) VALUES (%s, %s, %s, %s, %s, 0)
            """,
            expected[:-1],
        )
    elif row != expected:
        raise MaintenanceError("profile_conflict")


def _insert_source(
    connection: psycopg.Connection,
    profile: LegacyProfile,
    source_id: UUID,
    encryption_key: bytes,
) -> None:
    credential = (
        EncryptedBlob.encrypt(profile.credential_bundle, encryption_key).envelope
        if profile.credential_bundle is not None
        else None
    )
    token = (
        EncryptedBlob.encrypt(profile.token_bundle, encryption_key).envelope
        if profile.token_bundle is not None
        else None
    )
    has_material = credential is not None or token is not None
    requires_reconnect = profile.legacy_credentials_ciphertext is not None
    state = (
        "needs_reconnect"
        if requires_reconnect
        else "connected" if has_material else "disconnected"
    )
    safe_code = "authentication_required" if requires_reconnect else None
    reconnect_at = profile.created_at if requires_reconnect else None
    row = connection.execute(
        """
        SELECT provider, connection_key, state, state_revision,
               encrypted_credentials, encrypted_tokens, last_authenticated_at,
               reconnect_safe_code, reconnect_at, created_at, updated_at
        FROM source_connections WHERE profile_id = %s AND id = %s
        """,
        (profile.identifier, source_id),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO source_connections (
                profile_id, id, provider, connection_key, state,
                encrypted_credentials, encrypted_tokens, created_at, updated_at,
                reconnect_safe_code, reconnect_at
            ) VALUES (%s, %s, 'garmin', 'primary', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                profile.identifier,
                source_id,
                state,
                credential,
                token,
                profile.created_at,
                profile.created_at,
                safe_code,
                reconnect_at,
            ),
        )
    else:
        expected_metadata = (
            "garmin",
            "primary",
            state,
            0,
            None,
            safe_code,
            reconnect_at,
            profile.created_at,
            profile.created_at,
        )
        actual_metadata = (
            row[0], row[1], row[2], row[3], row[6], row[7], row[8], row[9], row[10]
        )
        if actual_metadata != expected_metadata:
            raise MaintenanceError("source_conflict")
        try:
            actual_credential = (
                EncryptedBlob.from_envelope(row[4], encryption_key).decrypt(encryption_key)
                if row[4] is not None
                else None
            )
            actual_token = (
                EncryptedBlob.from_envelope(row[5], encryption_key).decrypt(encryption_key)
                if row[5] is not None
                else None
            )
        except Exception:
            raise MaintenanceError("source_conflict") from None
        if actual_credential != profile.credential_bundle or actual_token != profile.token_bundle:
            raise MaintenanceError("source_conflict")


def _insert_app_session(
    connection: psycopg.Connection, profile_id: UUID, record: Mapping[str, Any]
) -> None:
    identifier = _app_uuid(record["id"])
    if connection.execute(
        "SELECT 1 FROM training_sessions WHERE profile_id = %s AND id = %s",
        (profile_id, identifier),
    ).fetchone() is not None:
        raise MaintenanceError("app_identity_conflict")
    content = record["content"]
    duration = content.get("duration")
    distance = content.get("distance")
    connection.execute(
        """
        INSERT INTO training_sessions (
            profile_id, id, ownership, app_revision, local_date, local_start,
            timing_precision, time_zone, utc_offset, sport, session_type, title,
            session_rpe, duration_value, duration_unit, duration_basis,
            distance_value, distance_unit, notes, created_at, updated_at
        ) VALUES (
            %s, %s, 'app', %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            profile_id,
            identifier,
            record["revision"],
            content["local_date"],
            _local_timestamp(content.get("local_start")),
            content["timing_precision"],
            content.get("time_zone"),
            content.get("utc_offset"),
            content["sport"],
            content.get("session_type"),
            content.get("title"),
            content.get("session_rpe"),
            duration.get("value") if duration else None,
            duration.get("unit") if duration else None,
            duration.get("basis") if duration else None,
            distance.get("value") if distance else None,
            distance.get("unit") if distance else None,
            content.get("notes"),
            _timestamp(record["created_at"]),
            _timestamp(record["updated_at"]),
        ),
    )
    for load in content.get("loads") or ():
        load_id = uuid5(NAMESPACE_URL, f"legacy-load:{profile_id.hex}:{identifier.hex}:{load['method']}:{load['unit']}:{load['source']}")
        connection.execute(
            """
            INSERT INTO session_loads (
                profile_id, id, training_session_id, ownership,
                method, unit, value, source
            ) VALUES (%s, %s, %s, 'app', %s, %s, %s, %s)
            """,
            (
                profile_id,
                load_id,
                identifier,
                load["method"],
                load["unit"],
                load["value"],
                load["source"],
            ),
        )


def _insert_goal_event(
    connection: psycopg.Connection, profile_id: UUID, record: Mapping[str, Any]
) -> None:
    identifier = _app_uuid(record["id"])
    if connection.execute(
        "SELECT 1 FROM goal_events WHERE profile_id = %s AND id = %s",
        (profile_id, identifier),
    ).fetchone() is not None:
        raise MaintenanceError("app_identity_conflict")
    content = record["content"]
    distance = content.get("distance")
    goal = content.get("goal")
    target = goal.get("target_duration") if goal else None
    outcome = content.get("outcome")
    actual = outcome.get("actual_duration") if outcome else None
    connection.execute(
        """
        INSERT INTO goal_events (
            profile_id, id, revision, local_date, local_start, timing_precision,
            time_zone, utc_offset, sport, name, priority, status,
            distance_value, distance_unit, goal_target_value, goal_target_unit,
            goal_statement, outcome_actual_value, outcome_actual_unit,
            outcome_statement, notes, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            profile_id,
            identifier,
            record["revision"],
            content["local_date"],
            _local_timestamp(content.get("local_start")),
            content["timing_precision"],
            content.get("time_zone"),
            content.get("utc_offset"),
            content["sport"],
            content["name"],
            content["priority"],
            content["status"],
            distance.get("value") if distance else None,
            distance.get("unit") if distance else None,
            target.get("value") if target else None,
            target.get("unit") if target else None,
            goal.get("statement") if goal else None,
            actual.get("value") if actual else None,
            actual.get("unit") if actual else None,
            outcome.get("statement") if outcome else None,
            content.get("notes"),
            _timestamp(record["created_at"]),
            _timestamp(record["updated_at"]),
        ),
    )


def _insert_plan(
    connection: psycopg.Connection, profile_id: UUID, record: Mapping[str, Any]
) -> None:
    plan_id = _app_uuid(record["id"])
    if connection.execute(
        "SELECT 1 FROM training_plans WHERE profile_id = %s AND id = %s",
        (profile_id, plan_id),
    ).fetchone() is not None:
        raise MaintenanceError("app_identity_conflict")
    revisions = record["content"]["revisions"]
    head = revisions[-1]["plan"]
    connection.execute(
        """
        INSERT INTO training_plans (
            profile_id, id, current_revision, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            profile_id,
            plan_id,
            record["revision"],
            head["status"],
            _timestamp(record["created_at"]),
            _timestamp(record["updated_at"]),
        ),
    )
    first_seen: dict[UUID, int] = {}
    for entry in revisions:
        for item in entry["plan"]["planned_sessions"]:
            first_seen.setdefault(UUID(hex=item["id"]), entry["revision"])
    for planned_id, created_revision in first_seen.items():
        connection.execute(
            "INSERT INTO planned_sessions (profile_id, plan_id, id, created_revision) "
            "VALUES (%s, %s, %s, %s)",
            (profile_id, plan_id, planned_id, created_revision),
        )
    for entry in revisions:
        snapshot = entry["plan"]
        revision = entry["revision"]
        connection.execute(
            """
            INSERT INTO plan_revisions (
                profile_id, plan_id, revision, kind, recorded_at, recorded_date,
                reason, effective_from, name, starts_on, ends_on, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                profile_id,
                plan_id,
                revision,
                entry["kind"],
                _timestamp(entry["recorded_at"]),
                entry["recorded_date"],
                entry["reason"],
                entry["effective_from"],
                snapshot["name"],
                snapshot["starts_on"],
                snapshot["ends_on"],
                snapshot["status"],
            ),
        )
        for position, ref in enumerate(snapshot["goal_events"]):
            connection.execute(
                """
                INSERT INTO plan_revision_goal_events (
                    profile_id, plan_id, revision, position,
                    goal_event_id, goal_event_revision
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    plan_id,
                    revision,
                    position,
                    _app_uuid(ref["goal_event_id"]),
                    ref["goal_event_revision"],
                ),
            )
        for position, statement in enumerate(snapshot["constraints"]):
            connection.execute(
                "INSERT INTO plan_revision_constraints "
                "(profile_id, plan_id, revision, position, statement) "
                "VALUES (%s, %s, %s, %s, %s)",
                (profile_id, plan_id, revision, position, statement),
            )
        for position, planned in enumerate(snapshot["planned_sessions"]):
            planned_id = UUID(hex=planned["id"])
            connection.execute(
                """
                INSERT INTO plan_revision_planned_sessions (
                    profile_id, plan_id, revision, planned_session_id, position,
                    scheduled_date, sport, session_type, prescription,
                    target_duration_seconds, target_distance_meters,
                    effort_guidance, disposition, fulfilment_note
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    profile_id,
                    plan_id,
                    revision,
                    planned_id,
                    position,
                    planned["scheduled_date"],
                    planned["sport"],
                    planned["session_type"],
                    planned["prescription"],
                    planned["target_duration_seconds"],
                    planned["target_distance_meters"],
                    planned["effort_guidance"],
                    planned["disposition"],
                    planned["fulfilment_note"],
                ),
            )
            for match_position, match in enumerate(planned["matches"]):
                match_id = _resolve_match(connection, profile_id, match)
                connection.execute(
                    """
                    INSERT INTO plan_revision_matches (
                        profile_id, plan_id, revision, planned_session_id,
                        position, training_session_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        profile_id,
                        plan_id,
                        revision,
                        planned_id,
                        match_position,
                        match_id,
                    ),
                )
    for planned in head["planned_sessions"]:
        planned_id = UUID(hex=planned["id"])
        for position, match in enumerate(planned["matches"]):
            connection.execute(
                """
                INSERT INTO planned_session_current_matches (
                    profile_id, plan_id, planned_session_id,
                    training_session_id, position
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    plan_id,
                    planned_id,
                    _resolve_match(connection, profile_id, match),
                    position,
                ),
            )


def _validate_reconstructed_plan_references(
    connection: psycopg.Connection, profile: LegacyProfile
) -> None:
    """Validate historical references after the owner-only policy bypass."""
    expected: set[tuple[UUID, int, int, UUID, int]] = set()
    for plan in profile.plans:
        plan_id = _app_uuid(plan["id"])
        for revision in plan["content"]["revisions"]:
            for position, reference in enumerate(revision["plan"]["goal_events"]):
                expected.add(
                    (
                        plan_id,
                        revision["revision"],
                        position,
                        _app_uuid(reference["goal_event_id"]),
                        reference["goal_event_revision"],
                    )
                )
    actual = set(
        connection.execute(
            """
            SELECT r.plan_id, r.revision, r.position,
                   r.goal_event_id, r.goal_event_revision
            FROM plan_revision_goal_events r
            WHERE r.profile_id = %s
            """,
            (profile.identifier,),
        ).fetchall()
    )
    if actual != expected:
        raise MaintenanceError("reference_validation_failed")
    invalid = connection.execute(
        """
        SELECT count(*)
        FROM plan_revision_goal_events r
        LEFT JOIN goal_events g
          ON g.profile_id = r.profile_id AND g.id = r.goal_event_id
        WHERE r.profile_id = %s
          AND (g.id IS NULL OR r.goal_event_revision < 1
               OR r.goal_event_revision > g.revision)
        """,
        (profile.identifier,),
    ).fetchone()[0]
    if invalid:
        raise MaintenanceError("reference_validation_failed")
    if any(
        not _plan_matches_source(
            connection, profile.identifier, _app_uuid(plan["id"]), plan
        )
        for plan in profile.plans
    ):
        raise MaintenanceError("reference_validation_failed")


def _insert_operational_state(
    connection: psycopg.Connection, profile: LegacyProfile, source_id: UUID
) -> None:
    job_ids: list[UUID] = []
    jobs = list(profile.jobs)
    if not jobs and profile.health is not None:
        jobs.append(_health_job(profile))
    for index, job in enumerate(jobs):
        job_id = uuid5(
            NAMESPACE_URL,
            f"legacy-job:{profile.identifier.hex}:{index}:{job.request_key}",
        )
        run_id = uuid5(NAMESPACE_URL, f"legacy-run:{job_id.hex}")
        attempt_id = uuid5(NAMESPACE_URL, f"legacy-attempt:{job_id.hex}")
        revision = 1 if job.state == "running" else 2
        diagnostics = Jsonb(
            {
                "attempt": 1,
                "cached_tokens": profile.token_bundle is not None,
            }
        )
        connection.execute(
            """
            INSERT INTO collection_jobs (
                profile_id, id, source_connection_id, request_key, kind, state,
                revision, safe_code, diagnostics, created_at, started_at, finished_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                profile.identifier,
                job_id,
                source_id,
                job.request_key,
                job.kind,
                job.state,
                revision,
                job.safe_code,
                diagnostics,
                job.created_at,
                job.started_at,
                job.finished_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO collection_runs (
                profile_id, id, job_id, run_number, state, safe_code,
                diagnostics, started_at, finished_at
            ) VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s)
            """,
            (
                profile.identifier,
                run_id,
                job_id,
                job.state,
                job.safe_code,
                diagnostics,
                job.started_at,
                job.finished_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO collection_attempts (
                profile_id, id, run_id, attempt_number, state, safe_code,
                diagnostics, started_at, finished_at
            ) VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s)
            """,
            (
                profile.identifier,
                attempt_id,
                run_id,
                job.state,
                job.safe_code,
                diagnostics,
                job.started_at,
                job.finished_at,
            ),
        )
        job_ids.append(job_id)

    health_times = _health_times(profile.health)
    domain = _operational_domain(profile)
    if profile.checkpoint is not None and health_times[2] is not None:
        connection.execute(
            """
            INSERT INTO collection_checkpoints (
                profile_id, source_connection_id, domain, cursor,
                revision, updated_at, last_success_at
            ) VALUES (%s, %s, %s, %s, 0, %s, %s)
            """,
            (
                profile.identifier,
                source_id,
                domain,
                Jsonb(profile.checkpoint),
                health_times[1] or health_times[2],
                health_times[2],
            ),
        )
    if profile.health is not None and job_ids:
        outcome = _health_outcome(profile.health)
        safe_code = None if outcome == "successful" else "external_failure"
        connection.execute(
            """
            INSERT INTO collection_health (
                profile_id, source_connection_id, domain, collection_job_id,
                outcome, attempted_at, finished_at, last_success_at,
                safe_code, diagnostics
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                profile.identifier,
                source_id,
                domain,
                job_ids[-1],
                outcome,
                health_times[0],
                health_times[1],
                health_times[2],
                safe_code,
                Jsonb({"attempt": 1, "cached_tokens": profile.token_bundle is not None}),
            ),
        )


def _reconcile_global_roots(
    connection: psycopg.Connection,
    inventory: Inventory,
    clerk_issuer: str,
) -> tuple[list[Issue], Counter[str]]:
    issues: list[Issue] = []
    counts: Counter[str] = Counter()
    expected_profiles = {profile.identifier for profile in inventory.profiles}
    actual_profiles = {
        row[0] for row in connection.execute("SELECT id FROM profiles").fetchall()
    }
    _identity_diff(issues, "profile", expected_profiles, actual_profiles)

    expected_sources = {
        (profile.identifier, _source_id(profile.identifier))
        for profile in inventory.profiles
    }
    actual_sources = set(
        connection.execute("SELECT profile_id, id FROM source_connections").fetchall()
    )
    _identity_diff(issues, "source", expected_sources, actual_sources)

    actual_units = {
        row[0] for row in connection.execute("SELECT profile_id FROM file_import_units").fetchall()
    }
    _identity_diff(issues, "import", expected_profiles, actual_units)

    queries = {
        "ingest_batches": "SELECT count(*) FROM ingest_batches",
        "collected_records": "SELECT count(*) FROM collected_records",
        "observation_heads": "SELECT count(*) FROM observation_projection_heads",
        "collected_session_loads": (
            "SELECT count(*) FROM session_loads WHERE ownership = 'collected'"
        ),
        "app_session_loads": (
            "SELECT count(*) FROM session_loads WHERE ownership = 'app'"
        ),
        "planned_sessions": "SELECT count(*) FROM planned_sessions",
        "plan_goal_references": "SELECT count(*) FROM plan_revision_goal_events",
        "plan_constraints": "SELECT count(*) FROM plan_revision_constraints",
        "plan_session_snapshots": (
            "SELECT count(*) FROM plan_revision_planned_sessions"
        ),
        "plan_revision_matches": "SELECT count(*) FROM plan_revision_matches",
        "current_matches": "SELECT count(*) FROM planned_session_current_matches",
        "import_units": "SELECT count(*) FROM file_import_units",
    }
    for category, query in queries.items():
        counts[category] = connection.execute(query).fetchone()[0]
    return issues, counts


def _reconcile_profile(
    connection: psycopg.Connection,
    profile: LegacyProfile,
    clerk_issuer: str,
    encryption_key: bytes | None = None,
) -> tuple[list[Issue], Counter[str]]:
    issues: list[Issue] = []
    counts: Counter[str] = Counter()
    binding = connection.execute(
        """
        SELECT clerk_issuer, clerk_subject, display_name, revision
        FROM profiles WHERE id = %s
        """,
        (profile.identifier,),
    ).fetchone()
    if binding is None:
        issues.append(Issue("profile", "gap"))
        return issues, counts
    counts["profiles"] += 1
    if binding != (clerk_issuer, profile.subject, profile.display_name, 0):
        issues.append(Issue("profile", "identity_mismatch"))

    source_id = _source_id(profile.identifier)
    source = connection.execute(
        """
        SELECT provider, connection_key,
               encrypted_credentials IS NOT NULL, encrypted_tokens IS NOT NULL
        FROM source_connections WHERE profile_id = %s AND id = %s
        """,
        (profile.identifier, source_id),
    ).fetchone()
    if source is None:
        issues.append(Issue("source", "gap"))
    else:
        counts["sources"] += 1
        if source != (
            "garmin",
            "primary",
            profile.credential_bundle is not None,
            profile.token_bundle is not None,
        ):
            issues.append(Issue("source", "identity_mismatch"))
        counts["credential_bundles"] += int(source[2])
        counts["token_bundles"] += int(source[3])

    expected_captures = {
        (item.record_kind, item.source_key, _payload_hash(item.payload))
        for item in profile.captures
    }
    actual_capture_rows = connection.execute(
        """
        SELECT r.record_kind, r.source_key, c.content_hash
        FROM collected_records r
        JOIN collected_record_captures c
          ON c.profile_id = r.profile_id AND c.collected_record_id = r.id
        WHERE r.profile_id = %s AND r.source_connection_id = %s
        """,
        (profile.identifier, source_id),
    ).fetchall()
    actual_captures = set(actual_capture_rows)
    counts["captures"] += len(actual_capture_rows)
    _identity_diff(issues, "capture", expected_captures, actual_captures)

    expected_session_keys = {item.capture.source_key for item in profile.sessions}
    actual_session_rows = connection.execute(
        """
        SELECT r.source_key, s.local_date, s.local_start, s.sport,
               s.duration_value, s.distance_value, s.title
        FROM training_sessions s
        JOIN collected_records r
          ON r.profile_id = s.profile_id AND r.id = s.collected_record_id
        WHERE s.profile_id = %s AND s.ownership = 'collected'
          AND r.source_connection_id = %s AND r.record_kind = 'activity.summary'
        """,
        (profile.identifier, source_id),
    ).fetchall()
    actual_session_keys = {row[0] for row in actual_session_rows}
    counts["collected_sessions"] += len(actual_session_rows)
    _identity_diff(issues, "collected_session", expected_session_keys, actual_session_keys)
    expected_sessions = {item.capture.source_key: item for item in profile.sessions}
    mismatches = 0
    for row in actual_session_rows:
        expected = expected_sessions.get(row[0])
        if expected is None:
            continue
        actual = (
            row[1],
            row[2],
            row[3],
            _json_number(row[4]),
            _json_number(row[5]),
            row[6],
        )
        wanted = (
            expected.local_date,
            expected.local_start,
            expected.sport,
            _json_number(expected.duration_value),
            _json_number(expected.distance_value),
            expected.title,
        )
        mismatches += int(actual != wanted)
    if mismatches:
        issues.append(Issue("collected_session", "projection_mismatch", mismatches))

    expected_observations = {
        (item.capture.record_kind, item.capture.source_key, item.definition): (
            item.status,
            _json_number(item.value),
        )
        for item in profile.observations
    }
    actual_observation_rows = connection.execute(
        """
        SELECT r.record_kind, r.source_key, o.definition_key, o.status,
               COALESCE(to_jsonb(o.decimal_value), to_jsonb(o.integer_value),
                        to_jsonb(o.text_value), to_jsonb(o.boolean_value))
        FROM controlled_observations o
        JOIN collected_records r
          ON r.profile_id = o.profile_id AND r.id = o.collected_record_id
        WHERE o.profile_id = %s AND r.source_connection_id = %s
        """,
        (profile.identifier, source_id),
    ).fetchall()
    actual_observations = {
        (row[0], row[1], row[2]): (row[3], _json_number(row[4]))
        for row in actual_observation_rows
    }
    counts["observations"] += len(actual_observation_rows)
    _identity_diff(
        issues,
        "observation",
        set(expected_observations),
        set(actual_observations),
    )
    observation_mismatches = sum(
        1
        for identity in set(expected_observations) & set(actual_observations)
        if expected_observations[identity] != actual_observations[identity]
    )
    if observation_mismatches:
        issues.append(Issue("observation", "projection_mismatch", observation_mismatches))

    _reconcile_app_records(connection, profile, issues, counts)
    checkpoint_rows = connection.execute(
        """
        SELECT domain, cursor, revision, updated_at, last_success_at
        FROM collection_checkpoints WHERE profile_id = %s
        """,
        (profile.identifier,),
    ).fetchall()
    counts["checkpoints"] += len(checkpoint_rows)
    expected_health_times = _health_times(profile.health)
    expected_checkpoint = (
        "incremental",
        profile.checkpoint,
        0,
        expected_health_times[1] or expected_health_times[2],
        expected_health_times[2],
    )
    if profile.checkpoint is not None and checkpoint_rows != [expected_checkpoint]:
        issues.append(Issue("checkpoint", "import_drift"))

    job_rows = connection.execute(
        """
        SELECT request_key, kind, state, safe_code, created_at, started_at, finished_at
        FROM collection_jobs WHERE profile_id = %s ORDER BY created_at, id
        """,
        (profile.identifier,),
    ).fetchall()
    counts["jobs"] += len(job_rows)
    counts["job_runs"] += connection.execute(
        "SELECT count(*) FROM collection_runs WHERE profile_id = %s",
        (profile.identifier,),
    ).fetchone()[0]
    counts["job_attempts"] += connection.execute(
        "SELECT count(*) FROM collection_attempts WHERE profile_id = %s",
        (profile.identifier,),
    ).fetchone()[0]
    expected_jobs = list(profile.jobs)
    if not expected_jobs and profile.health is not None:
        expected_jobs.append(_health_job(profile))
    expected_job_rows = [
        (
            job.request_key,
            job.kind,
            job.state,
            job.safe_code,
            job.created_at,
            job.started_at,
            job.finished_at,
        )
        for job in expected_jobs
    ]
    if job_rows != expected_job_rows:
        issues.append(Issue("job", "import_drift"))

    health_rows = connection.execute(
        """
        SELECT domain, outcome, attempted_at, finished_at, last_success_at, safe_code
        FROM collection_health WHERE profile_id = %s
        """,
        (profile.identifier,),
    ).fetchall()
    counts["health"] += len(health_rows)
    if profile.health is not None:
        outcome = _health_outcome(profile.health)
        expected_health = [
            (
                "incremental",
                outcome,
                expected_health_times[0],
                expected_health_times[1],
                expected_health_times[2],
                None if outcome == "successful" else "external_failure",
            )
        ]
        if health_rows != expected_health:
            issues.append(Issue("health", "import_drift"))

    marker = connection.execute(
        "SELECT source_manifest_hash, importer_version FROM file_import_units "
        "WHERE profile_id = %s",
        (profile.identifier,),
    ).fetchone()
    if marker is None:
        issues.append(Issue("import", "missing_evidence"))
    elif marker != (profile.manifest_hash, _IMPORTER_VERSION):
        issues.append(Issue("import", "import_drift"))

    isolation_count = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT r.profile_id FROM collected_records r
            JOIN source_connections s ON s.id = r.source_connection_id
            WHERE r.profile_id = %s AND s.profile_id <> r.profile_id
            UNION ALL
            SELECT p.profile_id FROM plan_revision_goal_events p
            JOIN goal_events g ON g.id = p.goal_event_id
            WHERE p.profile_id = %s AND g.profile_id <> p.profile_id
            UNION ALL
            SELECT m.profile_id FROM plan_revision_matches m
            JOIN training_sessions s ON s.id = m.training_session_id
            WHERE m.profile_id = %s AND s.profile_id <> m.profile_id
        ) violations
        """,
        (profile.identifier, profile.identifier, profile.identifier),
    ).fetchone()[0]
    if isolation_count:
        issues.append(Issue("isolation", "cross_profile_reference", isolation_count))
    return issues, counts


def _reconcile_app_records(
    connection: psycopg.Connection,
    profile: LegacyProfile,
    issues: list[Issue],
    counts: Counter[str],
) -> None:
    session_records = {_app_uuid(item["id"]): item for item in profile.app_sessions}
    expected_sessions = {
        identifier: item["revision"] for identifier, item in session_records.items()
    }
    actual_sessions = dict(
        connection.execute(
            "SELECT id, app_revision FROM training_sessions "
            "WHERE profile_id = %s AND ownership = 'app'",
            (profile.identifier,),
        ).fetchall()
    )
    counts["app_sessions"] += len(actual_sessions)
    _identity_diff(issues, "app_session", set(expected_sessions), set(actual_sessions))
    _revision_diff(issues, expected_sessions, actual_sessions)
    session_mismatches = sum(
        1
        for identifier in set(session_records) & set(actual_sessions)
        if _expected_app_session_signature(session_records[identifier])
        != _actual_app_session_signature(connection, profile.identifier, identifier)
    )
    if session_mismatches:
        issues.append(
            Issue("app_session", "projection_mismatch", session_mismatches)
        )

    goal_records = {_app_uuid(item["id"]): item for item in profile.goal_events}
    expected_goals = {
        identifier: item["revision"] for identifier, item in goal_records.items()
    }
    actual_goals = dict(
        connection.execute(
            "SELECT id, revision FROM goal_events WHERE profile_id = %s",
            (profile.identifier,),
        ).fetchall()
    )
    counts["goal_events"] += len(actual_goals)
    _identity_diff(issues, "goal_event", set(expected_goals), set(actual_goals))
    _revision_diff(issues, expected_goals, actual_goals)
    goal_mismatches = sum(
        1
        for identifier in set(goal_records) & set(actual_goals)
        if _expected_goal_signature(goal_records[identifier])
        != _actual_goal_signature(connection, profile.identifier, identifier)
    )
    if goal_mismatches:
        issues.append(Issue("goal_event", "projection_mismatch", goal_mismatches))

    plan_records = {_app_uuid(item["id"]): item for item in profile.plans}
    expected_plans = {
        identifier: item["revision"] for identifier, item in plan_records.items()
    }
    actual_plans = dict(
        connection.execute(
            "SELECT id, current_revision FROM training_plans WHERE profile_id = %s",
            (profile.identifier,),
        ).fetchall()
    )
    counts["training_plans"] += len(actual_plans)
    _identity_diff(issues, "training_plan", set(expected_plans), set(actual_plans))
    _revision_diff(issues, expected_plans, actual_plans)
    plan_mismatches = sum(
        1
        for identifier in set(plan_records) & set(actual_plans)
        if not _plan_matches_source(
            connection, profile.identifier, identifier, plan_records[identifier]
        )
    )
    if plan_mismatches:
        issues.append(
            Issue("training_plan", "projection_mismatch", plan_mismatches)
        )
    counts["plan_revisions"] += connection.execute(
        "SELECT count(*) FROM plan_revisions WHERE profile_id = %s",
        (profile.identifier,),
    ).fetchone()[0]

    expected_revisions = {
        (_app_uuid(plan["id"]), revision)
        for plan in profile.plans
        for revision in range(1, plan["revision"] + 1)
    }
    actual_revisions = set(
        connection.execute(
            "SELECT plan_id, revision FROM plan_revisions WHERE profile_id = %s",
            (profile.identifier,),
        ).fetchall()
    )
    _identity_diff(issues, "plan_revision", expected_revisions, actual_revisions)


def _expected_app_session_signature(record: Mapping[str, Any]) -> tuple[Any, ...]:
    content = record["content"]
    duration = content.get("duration")
    distance = content.get("distance")
    loads = tuple(
        sorted(
            (
                load["method"],
                load["unit"],
                load["source"],
                _json_number(load["value"]),
            )
            for load in content.get("loads") or ()
        )
    )
    return (
        record["revision"],
        date.fromisoformat(content["local_date"]),
        _local_timestamp(content.get("local_start")),
        content["timing_precision"],
        content.get("time_zone"),
        content.get("utc_offset"),
        content["sport"],
        content.get("session_type"),
        content.get("title"),
        content.get("session_rpe"),
        _json_number(duration.get("value")) if duration else None,
        duration.get("unit") if duration else None,
        duration.get("basis") if duration else None,
        _json_number(distance.get("value")) if distance else None,
        distance.get("unit") if distance else None,
        content.get("notes"),
        _timestamp(record["created_at"]),
        _timestamp(record["updated_at"]),
        loads,
    )


def _actual_app_session_signature(
    connection: psycopg.Connection, profile_id: UUID, identifier: UUID
) -> tuple[Any, ...]:
    row = connection.execute(
        """
        SELECT app_revision, local_date, local_start, timing_precision,
               time_zone, utc_offset, sport, session_type, title, session_rpe,
               duration_value, duration_unit, duration_basis, distance_value,
               distance_unit, notes, created_at, updated_at
        FROM training_sessions
        WHERE profile_id = %s AND id = %s AND ownership = 'app'
        """,
        (profile_id, identifier),
    ).fetchone()
    assert row is not None
    loads = tuple(
        (
            load[0],
            load[1],
            load[2],
            _json_number(load[3]),
        )
        for load in connection.execute(
            """
            SELECT method, unit, source, value FROM session_loads
            WHERE profile_id = %s AND training_session_id = %s
            ORDER BY method, unit, source
            """,
            (profile_id, identifier),
        ).fetchall()
    )
    normalized = list(row)
    normalized[10] = _json_number(normalized[10])
    normalized[13] = _json_number(normalized[13])
    return (*normalized, loads)


def _expected_goal_signature(record: Mapping[str, Any]) -> tuple[Any, ...]:
    content = record["content"]
    distance = content.get("distance")
    goal = content.get("goal")
    target = goal.get("target_duration") if goal else None
    outcome = content.get("outcome")
    actual = outcome.get("actual_duration") if outcome else None
    return (
        record["revision"],
        date.fromisoformat(content["local_date"]),
        _local_timestamp(content.get("local_start")),
        content["timing_precision"],
        content.get("time_zone"),
        content.get("utc_offset"),
        content["sport"],
        content["name"],
        content["priority"],
        content["status"],
        _json_number(distance.get("value")) if distance else None,
        distance.get("unit") if distance else None,
        _json_number(target.get("value")) if target else None,
        target.get("unit") if target else None,
        goal.get("statement") if goal else None,
        _json_number(actual.get("value")) if actual else None,
        actual.get("unit") if actual else None,
        outcome.get("statement") if outcome else None,
        content.get("notes"),
        _timestamp(record["created_at"]),
        _timestamp(record["updated_at"]),
    )


def _actual_goal_signature(
    connection: psycopg.Connection, profile_id: UUID, identifier: UUID
) -> tuple[Any, ...]:
    row = connection.execute(
        """
        SELECT revision, local_date, local_start, timing_precision,
               time_zone, utc_offset, sport, name, priority, status,
               distance_value, distance_unit, goal_target_value,
               goal_target_unit, goal_statement, outcome_actual_value,
               outcome_actual_unit, outcome_statement, notes,
               created_at, updated_at
        FROM goal_events WHERE profile_id = %s AND id = %s
        """,
        (profile_id, identifier),
    ).fetchone()
    assert row is not None
    normalized = list(row)
    for index in (10, 12, 15):
        normalized[index] = _json_number(normalized[index])
    return tuple(normalized)


def _plan_matches_source(
    connection: psycopg.Connection,
    profile_id: UUID,
    plan_id: UUID,
    record: Mapping[str, Any],
) -> bool:
    root = connection.execute(
        """
        SELECT current_revision, status, created_at, updated_at
        FROM training_plans WHERE profile_id = %s AND id = %s
        """,
        (profile_id, plan_id),
    ).fetchone()
    revisions = record["content"]["revisions"]
    head = revisions[-1]["plan"]
    if root != (
        record["revision"],
        head["status"],
        _timestamp(record["created_at"]),
        _timestamp(record["updated_at"]),
    ):
        return False
    actual_metadata = connection.execute(
        """
        SELECT revision, kind, recorded_at, recorded_date, reason, effective_from
        FROM plan_revisions
        WHERE profile_id = %s AND plan_id = %s ORDER BY revision
        """,
        (profile_id, plan_id),
    ).fetchall()
    expected_metadata = [
        (
            entry["revision"],
            entry["kind"],
            _timestamp(entry["recorded_at"]),
            date.fromisoformat(entry["recorded_date"]),
            entry["reason"],
            date.fromisoformat(entry["effective_from"])
            if entry["effective_from"] is not None
            else None,
        )
        for entry in revisions
    ]
    if actual_metadata != expected_metadata:
        return False
    for entry in revisions:
        expected_snapshot = json.loads(json.dumps(entry["plan"]))
        for planned in expected_snapshot["planned_sessions"]:
            planned["matches"] = [
                _target_match_public_id(connection, profile_id, value)
                for value in planned["matches"]
            ]
        actual_snapshot = AppRecordStore._snapshot(
            connection, profile_id, plan_id, entry["revision"]
        )
        if actual_snapshot != expected_snapshot:
            return False
    return True


def _target_match_public_id(
    connection: psycopg.Connection, profile_id: UUID, value: str
) -> str:
    identifier = _resolve_match(connection, profile_id, value)
    ownership = connection.execute(
        "SELECT ownership FROM training_sessions WHERE profile_id = %s AND id = %s",
        (profile_id, identifier),
    ).fetchone()
    if ownership is None:
        raise MaintenanceError("missing_reference")
    prefix = "app" if ownership[0] == "app" else "session"
    return f"{prefix}:{identifier.hex}"


def _identity_diff(
    issues: list[Issue], category: str, expected: set[Any], actual: set[Any]
) -> None:
    missing = len(expected - actual)
    extra = len(actual - expected)
    if missing:
        issues.append(Issue(category, "gap", missing))
    if extra:
        issues.append(Issue(category, "import_drift", extra))


def _revision_diff(
    issues: list[Issue], expected: Mapping[Any, int], actual: Mapping[Any, int]
) -> None:
    stale = sum(
        1
        for identifier in set(expected) & set(actual)
        if expected[identifier] != actual[identifier]
    )
    if stale:
        issues.append(Issue("app_record", "stale_revision", stale))


def _raw_identity(relative: Path) -> tuple[str, str]:
    parts = relative.parts
    stem = relative.stem
    if len(parts) == 5 and parts[0] == "daily":
        return f"daily.{stem}", parts[3]
    if len(parts) == 5 and parts[0] == "activities":
        return f"activity.{stem}", parts[3]
    if len(parts) == 4 and parts[0] == "activities" and parts[1] == "_":
        return f"activity.{stem}", parts[2]
    if len(parts) == 3 and parts[0] in {"profile", "plans"}:
        prefix = "profile" if parts[0] == "profile" else "plan"
        return f"{prefix}.{stem}", parts[1]
    if len(parts) == 3 and parts[0] == "body":
        return f"body.{parts[1]}", stem
    safe_kind = re.sub(r"[^a-z0-9_]+", "_", parts[0].lower()) if parts else "unknown"
    return f"legacy.{safe_kind}", relative.as_posix()


def _session_input(item: Any) -> TrainingSessionInput:
    return TrainingSessionInput(
        RecordPointerInput(item.capture.record_kind, item.capture.source_key),
        item.local_date,
        item.sport,
        item.timing_precision,
        item.local_start,
        item.time_zone,
        item.utc_offset,
        item.session_type,
        item.title,
        item.session_rpe,
        item.duration_value,
        item.duration_unit,
        item.duration_basis,
        item.distance_value,
        item.distance_unit,
        item.notes,
        tuple(
            SessionLoadInput(load.method, load.unit, load.value, load.source)
            for load in item.loads
        ),
    )


def _observation_input(item: Any) -> ObservationInput:
    return ObservationInput(
        RecordPointerInput(item.capture.record_kind, item.capture.source_key),
        item.definition,
        item.value_type,
        item.unit,
        item.window_kind,
        item.method,
        item.status,
        item.value,
        item.local_date,
        item.observed_at,
        item.window_start,
        item.window_end,
        item.provenance,
    )


def _resolve_match(
    connection: psycopg.Connection, profile_id: UUID, value: str
) -> UUID:
    match = _APP_RE.fullmatch(value)
    if match:
        identifier = UUID(hex=match.group(1))
        row = connection.execute(
            "SELECT 1 FROM training_sessions WHERE profile_id = %s AND id = %s AND ownership = 'app'",
            (profile_id, identifier),
        ).fetchone()
        if row is None:
            raise MaintenanceError("missing_reference")
        return identifier
    if value.startswith("garmin:"):
        row = connection.execute(
            """
            SELECT s.id FROM training_sessions s
            JOIN collected_records r
              ON r.profile_id = s.profile_id AND r.id = s.collected_record_id
            WHERE s.profile_id = %s AND s.ownership = 'collected'
              AND r.record_kind = 'activity.summary' AND r.source_key = %s
            """,
            (profile_id, value.removeprefix("garmin:")),
        ).fetchone()
        if row is None:
            raise MaintenanceError("missing_reference")
        return row[0]
    raise MaintenanceError("invalid_reference")


def _active_health_states(health: Mapping[str, Any]) -> int:
    def active(value: Any) -> int:
        if not isinstance(value, Mapping):
            return 0
        return int(
            value.get("outcome") == "attempted"
            or (
                value.get("attempted_at") is not None
                and value.get("finished_at") is None
            )
        )

    total = active(health.get("run"))
    domains = health.get("domains")
    if isinstance(domains, Mapping):
        for domain in domains.values():
            total += active(domain)
            if isinstance(domain, Mapping) and isinstance(domain.get("dates"), Mapping):
                total += sum(active(value) for value in domain["dates"].values())
    return total


def _operational_domain(profile: LegacyProfile) -> str:
    # File collector state represented one rolling incremental cursor. The raw
    # legacy payload remains preserved inside that cursor for future rebuilds.
    return "incremental"


def _health_job(profile: LegacyProfile) -> LegacyJob:
    attempted, finished, _last_success = _health_times(profile.health)
    attempted = attempted or profile.created_at
    outcome = _health_outcome(profile.health)
    succeeded = outcome == "successful"
    return LegacyJob(
        "succeeded" if succeeded else "failed",
        "incremental",
        attempted,
        attempted,
        finished or attempted,
        None if succeeded else "external_failure",
        "legacy-health-snapshot",
    )


def _health_times(
    health: Mapping[str, Any] | None,
) -> tuple[datetime | None, datetime | None, datetime | None]:
    if not isinstance(health, Mapping) or not isinstance(health.get("run"), Mapping):
        return None, None, None
    run = health["run"]
    return tuple(_optional_timestamp(run.get(key)) for key in ("attempted_at", "finished_at", "last_success_at"))  # type: ignore[return-value]


def _health_outcome(health: Mapping[str, Any] | None) -> str:
    if not isinstance(health, Mapping) or not isinstance(health.get("run"), Mapping):
        return "not_attempted"
    value = health["run"].get("outcome")
    return value if value in {"not_attempted", "successful", "degraded", "failed"} else "failed"


def _safe_job_code(value: Any) -> str:
    return {
        "invalid_request": "invalid_response",
        "collection_degraded": "external_failure",
        "collection_failed": "external_failure",
    }.get(value, "external_failure")


def _source_id(profile_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"garmin-coach:legacy-source:{profile_id.hex}")


def _app_uuid(value: str) -> UUID:
    match = _APP_RE.fullmatch(value)
    if not match:
        raise MaintenanceError("invalid_app_identity")
    return UUID(hex=match.group(1))


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise MaintenanceError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MaintenanceError("invalid_timestamp") from None
    if parsed.utcoffset() is None:
        raise MaintenanceError("ambiguous_timestamp")
    return parsed.astimezone(timezone.utc)


def _optional_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return _timestamp(value)
    except MaintenanceError:
        return None


def _local_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MaintenanceError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MaintenanceError("invalid_timestamp") from None
    return parsed.replace(tzinfo=None)


def _payload_hash(payload: Any) -> bytes:
    normalized = json.dumps(
        _json_semantics(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(normalized).digest()


def _json_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def _snapshot_source(source_root: Path) -> SourceSnapshot:
    """Read a bounded source tree without following links or reopening paths."""
    absolute_root = _validated_root(source_root)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    files: dict[PurePosixPath, bytes] = {}
    directories: set[PurePosixPath] = set()
    aggregate_bytes = 0

    def unchanged(before: os.stat_result, after: os.stat_result) -> bool:
        return (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        )

    def walk(directory_fd: int, relative: PurePosixPath) -> None:
        nonlocal aggregate_bytes
        before_directory = os.fstat(directory_fd)
        try:
            entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        except OSError:
            raise MaintenanceError("source_read_failed") from None
        for entry in entries:
            child = relative / entry.name if relative.parts else PurePosixPath(entry.name)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise MaintenanceError("source_read_failed") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise MaintenanceError("unsafe_source_entry")
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child_fd = os.open(
                        entry.name,
                        directory_flags | nofollow,
                        dir_fd=directory_fd,
                    )
                except OSError:
                    raise MaintenanceError("source_read_failed") from None
                try:
                    opened = os.fstat(child_fd)
                    if not stat.S_ISDIR(opened.st_mode) or not unchanged(metadata, opened):
                        raise MaintenanceError("source_changed_during_snapshot")
                    directories.add(child)
                    walk(child_fd, child)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise MaintenanceError("unsafe_source_entry")
            if len(files) >= MAX_FILES or metadata.st_size > MAX_FILE_BYTES:
                raise MaintenanceError("source_limit_exceeded")
            try:
                file_fd = os.open(entry.name, os.O_RDONLY | nofollow, dir_fd=directory_fd)
            except OSError:
                raise MaintenanceError("source_read_failed") from None
            try:
                opened = os.fstat(file_fd)
                if not stat.S_ISREG(opened.st_mode) or not unchanged(metadata, opened):
                    raise MaintenanceError("source_changed_during_snapshot")
                chunks: list[bytes] = []
                remaining = metadata.st_size
                while remaining:
                    chunk = os.read(file_fd, min(remaining, 1024 * 1024))
                    if not chunk:
                        raise MaintenanceError("source_changed_during_snapshot")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
                if not unchanged(opened, os.fstat(file_fd)):
                    raise MaintenanceError("source_changed_during_snapshot")
            except OSError:
                raise MaintenanceError("source_read_failed") from None
            finally:
                os.close(file_fd)
            aggregate_bytes += len(content)
            if aggregate_bytes > MAX_AGGREGATE_BYTES:
                raise MaintenanceError("source_limit_exceeded")
            files[child] = content
        if not unchanged(before_directory, os.fstat(directory_fd)):
            raise MaintenanceError("source_changed_during_snapshot")

    try:
        root_fd = os.open(absolute_root, directory_flags | nofollow)
    except OSError:
        raise MaintenanceError("source_unavailable") from None
    try:
        walk(root_fd, PurePosixPath())
    finally:
        os.close(root_fd)
    return SourceSnapshot(absolute_root, files, directories)


def _read_snapshot_json(
    snapshot: SourceSnapshot, relative: PurePosixPath, expected: type
) -> Any | None:
    try:
        value = _decode_json(snapshot.read(relative))
    except FileNotFoundError:
        return None
    if value is _INVALID or not isinstance(value, expected):
        return None
    return value


def _read_json(path: Path, expected: type) -> Any | None:
    try:
        value = _decode_json(_safe_regular_bytes(path))
    except FileNotFoundError:
        return None
    if value is _INVALID or not isinstance(value, expected):
        return None
    return value


_INVALID = object()


def _decode_json(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _INVALID


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise MaintenanceError("source_read_failed") from None


def _safe_regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError("unsafe_source_entry")
    return _read_bytes(path)


def _regular_files(root: Path, issues: list[Issue], category: str) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        issues.append(Issue(category, "unsafe_entry"))
        return []
    result: list[Path] = []
    try:
        entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    except OSError:
        raise MaintenanceError("source_read_failed") from None
    for path in entries:
        if path.is_symlink():
            issues.append(Issue(category, "unsafe_entry"))
        elif path.is_file():
            result.append(path)
    return result


def _validated_root(path: Path) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute():
        raise MaintenanceError("source_must_be_absolute")
    if path.is_symlink():
        raise MaintenanceError("source_unavailable")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise MaintenanceError("source_unavailable") from None
    if resolved.is_symlink() or not resolved.is_dir():
        raise MaintenanceError("source_unavailable")
    return resolved


def _require_schema(connection: psycopg.Connection) -> None:
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if row != (_REQUIRED_REVISION,):
        raise MaintenanceError("migration_required")


def _require_maintenance_owner(connection: psycopg.Connection) -> None:
    row = connection.execute(
        """
        SELECT current_user = pg_get_userbyid(c.relowner)
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'file_import_units'
        """
    ).fetchone()
    if row != (True,):
        raise MaintenanceError("maintenance_owner_required")


def _coalesce_issues(issues: Iterable[Issue]) -> list[Issue]:
    totals: Counter[tuple[str, str]] = Counter()
    for issue in issues:
        totals[(issue.category, issue.code)] += issue.count
    return [
        Issue(category, code, count)
        for (category, code), count in sorted(totals.items())
        if count
    ]


def _load_encryption_key(path: Path) -> bytes:
    if not path.is_absolute():
        raise MaintenanceError("key_path_must_be_absolute")
    return _safe_regular_bytes(path).strip()


def _inventory_report(inventory: Inventory) -> MaintenanceReport:
    return MaintenanceReport(
        "inventory",
        {key: (value, None) for key, value in inventory.counts().items()},
        inventory.all_issues(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inventory", "import", "shadow"))
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--clerk-issuer", required=True)
    parser.add_argument("--encryption-key-file")
    arguments = parser.parse_args(argv)
    try:
        source = Path(arguments.source_root)
        settings = DatabaseSettings.from_url(arguments.database_url)
        if arguments.operation == "inventory":
            # Connectivity and schema are intentionally checked even for the
            # read-only phase so every invocation has an explicit, compatible
            # source/target pair and cannot silently inventory one store for a
            # different eventual database.
            with psycopg.connect(settings.url) as connection:
                _require_schema(connection)
            report = _inventory_report(inventory_store(source, arguments.clerk_issuer))
        elif arguments.operation == "import":
            if not arguments.encryption_key_file:
                raise MaintenanceError("encryption_key_required")
            report = import_store(
                source,
                settings,
                arguments.clerk_issuer,
                _load_encryption_key(Path(arguments.encryption_key_file)),
            )
        else:
            report = reconcile_store(source, settings, arguments.clerk_issuer)
        print(json.dumps(report.public(), sort_keys=True, separators=(",", ":")))
        return 0 if report.ok else 1
    except MaintenanceError as exc:
        print(
            json.dumps(
                {"operation": arguments.operation, "ok": False, "code": exc.code},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        # Configuration, driver, and parser exceptions can retain a source
        # path, DSN, identifier, payload, or secret. Never echo them.
        print(
            json.dumps(
                {
                    "operation": arguments.operation,
                    "ok": False,
                    "code": "maintenance_failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
