from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from coach.data import (
    CoachData,
    DataIntegrityError,
    InvalidRecord,
    NotFound,
    ReadOnlyRecord,
    ReferencedRecord,
    RevisionConflict,
)

TODAY = date.today()


def _d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


_TEMP_DIRS: list[str] = []


def tearDownModule() -> None:
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)
    _TEMP_DIRS.clear()


def _coach() -> tuple[CoachData, str]:
    tmp = tempfile.mkdtemp()
    _TEMP_DIRS.append(tmp)
    return CoachData(Path(tmp)), tmp


def _base_plan(coach: CoachData, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "name": "Base build",
        "starts_on": _d(0),
        "ends_on": _d(90),
        "reason": "Kick off the block",
    }
    kwargs.update(overrides)
    return coach.create_plan(**kwargs)  # type: ignore[arg-type]


def _goal_event(coach: CoachData) -> dict[str, object]:
    return coach.create_goal_event(
        {
            "name": "Autumn Marathon",
            "sport": "running",
            "local_date": _d(88),
            "priority": "primary",
        }
    )


class CreatePlanTest(unittest.TestCase):
    def test_create_projects_draft_head_with_identity(self) -> None:
        coach, tmp = _coach()
        created = _base_plan(
            coach,
            constraints=["No sessions on Mondays"],
            planned_sessions=[
                {
                    "scheduled_date": _d(3),
                    "sport": "running",
                    "prescription": "Easy 5k",
                }
            ],
        )

        self.assertTrue(created["id"].startswith("app:"))
        self.assertEqual(created["origin"], "app_record")
        self.assertEqual(created["provenance"], {"source": "manual"})
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["name"], "Base build")
        self.assertEqual(created["status"], "draft")
        self.assertEqual(created["constraints"], ["No sessions on Mondays"])
        self.assertEqual(created["goal_events"], [])
        self.assertFalse(created["requires_review"])
        self.assertEqual(created["created_at"], created["updated_at"])
        self.assertEqual(len(created["planned_sessions"]), 1)

        planned = created["planned_sessions"][0]
        self.assertTrue(planned["id"])
        self.assertEqual(planned["disposition"], "scheduled")
        self.assertEqual(planned["matches"], [])
        self.assertIsNone(planned["fulfilment_note"])
        self.assertEqual(planned["prescription"], "Easy 5k")

        stem = created["id"].split(":", 1)[1]
        self.assertTrue((Path(tmp) / "app" / "plans" / f"{stem}.json").exists())

    def test_caller_never_chooses_planned_session_identity(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "A"},
                {"scheduled_date": _d(3), "sport": "running", "prescription": "B"},
            ],
        )
        ids = {p["id"] for p in created["planned_sessions"]}
        self.assertEqual(len(ids), 2)  # same-date items stay distinct

    def test_create_stores_goal_reference_at_current_revision(self) -> None:
        coach, _ = _coach()
        event = _goal_event(coach)
        created = _base_plan(
            coach,
            goal_events=[
                {"goal_event_id": event["id"], "goal_event_revision": event["revision"]}
            ],
        )
        self.assertEqual(
            created["goal_events"],
            [
                {
                    "goal_event_id": event["id"],
                    "goal_event_revision": 1,
                    "stale": False,
                    "stale_reason": None,
                }
            ],
        )

    def test_create_rejects_invalid_input(self) -> None:
        coach, _ = _coach()
        with self.assertRaises(InvalidRecord):
            _base_plan(coach, name="   ")
        with self.assertRaises(InvalidRecord):
            _base_plan(coach, reason="")
        with self.assertRaises(InvalidRecord):
            _base_plan(coach, starts_on=_d(10), ends_on=_d(1))
        with self.assertRaises(InvalidRecord):
            _base_plan(coach, ends_on="2026-13-40")
        with self.assertRaises(InvalidRecord):
            _base_plan(
                coach,
                planned_sessions=[
                    {"scheduled_date": _d(200), "sport": "running", "prescription": "x"}
                ],
            )
        with self.assertRaises(InvalidRecord):
            _base_plan(
                coach,
                planned_sessions=[
                    {"scheduled_date": _d(3), "sport": "running", "prescription": "  "}
                ],
            )
        with self.assertRaises(InvalidRecord):
            _base_plan(coach, constraints=["ok", "   "])

    def test_create_rejects_goal_reference_that_is_not_current(self) -> None:
        coach, _ = _coach()
        event = _goal_event(coach)
        with self.assertRaises(InvalidRecord):
            _base_plan(
                coach,
                goal_events=[
                    {"goal_event_id": event["id"], "goal_event_revision": 2}
                ],
            )
        with self.assertRaises(InvalidRecord):
            _base_plan(
                coach,
                goal_events=[
                    {"goal_event_id": "app:" + "0" * 32, "goal_event_revision": 1}
                ],
            )

    def test_create_rejects_duplicate_goal_reference(self) -> None:
        coach, _ = _coach()
        event = _goal_event(coach)
        ref = {"goal_event_id": event["id"], "goal_event_revision": event["revision"]}
        with self.assertRaises(InvalidRecord):
            _base_plan(coach, goal_events=[ref, dict(ref)])


class GetListHistoryTest(unittest.TestCase):
    def test_get_round_trips_created_plan(self) -> None:
        coach, _ = _coach()
        created = _base_plan(coach)
        self.assertEqual(coach.get_plan(created["id"]), created)

    def test_get_missing_plan_raises_not_found(self) -> None:
        coach, _ = _coach()
        with self.assertRaises(NotFound):
            coach.get_plan("app:" + "0" * 32)

    def test_list_sorts_by_start_and_filters_by_status(self) -> None:
        coach, _ = _coach()
        later = _base_plan(coach, name="Later", starts_on=_d(40), ends_on=_d(90))
        earlier = _base_plan(coach, name="Earlier", starts_on=_d(1), ends_on=_d(30))
        coach.activate_plan(earlier["id"], earlier["revision"], "start")

        listed = coach.list_plans()
        self.assertEqual([p["id"] for p in listed], [earlier["id"], later["id"]])

        active = coach.list_plans(status="active")
        self.assertEqual([p["id"] for p in active], [earlier["id"]])

    def test_history_returns_every_revision_oldest_first(self) -> None:
        coach, _ = _coach()
        created = _base_plan(coach)
        activated = coach.activate_plan(created["id"], created["revision"], "go")
        history = coach.get_plan_history(created["id"])
        self.assertEqual(history["id"], created["id"])
        self.assertEqual(
            [(r["revision"], r["kind"], r["reason"]) for r in history["revisions"]],
            [(1, "create", "Kick off the block"), (2, "lifecycle", "go")],
        )
        self.assertEqual(activated["revision"], 2)


class ActivateArchiveDeleteTest(unittest.TestCase):
    def test_activate_advances_revision_and_sets_active(self) -> None:
        coach, _ = _coach()
        created = _base_plan(coach)
        activated = coach.activate_plan(created["id"], created["revision"], "go")
        self.assertEqual(activated["status"], "active")
        self.assertEqual(activated["revision"], 2)

    def test_only_one_plan_may_be_active(self) -> None:
        coach, _ = _coach()
        first = _base_plan(coach, name="First")
        second = _base_plan(coach, name="Second")
        coach.activate_plan(first["id"], first["revision"], "go")
        with self.assertRaises(InvalidRecord):
            coach.activate_plan(second["id"], second["revision"], "go too")

    def test_activation_rejects_scheduled_prescription_before_today(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            starts_on=_d(-5),
            ends_on=_d(30),
            planned_sessions=[
                {"scheduled_date": _d(-2), "sport": "running", "prescription": "old"}
            ],
        )
        with self.assertRaises(InvalidRecord):
            coach.activate_plan(created["id"], created["revision"], "go")

    def test_activation_requires_expected_revision(self) -> None:
        coach, _ = _coach()
        created = _base_plan(coach)
        with self.assertRaises(RevisionConflict):
            coach.activate_plan(created["id"], 99, "go")

    def test_non_draft_cannot_be_activated(self) -> None:
        coach, _ = _coach()
        created = _base_plan(coach)
        activated = coach.activate_plan(created["id"], created["revision"], "go")
        with self.assertRaises(InvalidRecord):
            coach.activate_plan(created["id"], activated["revision"], "again")

    def test_archive_is_terminal_from_draft_or_active(self) -> None:
        coach, _ = _coach()
        draft = _base_plan(coach, name="Draft")
        archived = coach.archive_plan(draft["id"], draft["revision"], "abandon")
        self.assertEqual(archived["status"], "archived")
        with self.assertRaises(InvalidRecord):
            coach.archive_plan(draft["id"], archived["revision"], "again")

        active = _base_plan(coach, name="Active")
        active = coach.activate_plan(active["id"], active["revision"], "go")
        done = coach.archive_plan(active["id"], active["revision"], "season over")
        self.assertEqual(done["status"], "archived")

    def test_delete_only_removes_never_active_draft_without_fulfilment(self) -> None:
        coach, tmp = _coach()
        draft = _base_plan(coach)
        stem = draft["id"].split(":", 1)[1]
        path = Path(tmp) / "app" / "plans" / f"{stem}.json"
        self.assertTrue(path.exists())
        result = coach.delete_plan(draft["id"], draft["revision"])
        self.assertTrue(result["deleted"])
        self.assertFalse(path.exists())

    def test_ever_active_plan_cannot_be_deleted(self) -> None:
        coach, _ = _coach()
        created = _base_plan(coach)
        active = coach.activate_plan(created["id"], created["revision"], "go")
        archived = coach.archive_plan(created["id"], active["revision"], "done")
        with self.assertRaises(InvalidRecord):
            coach.delete_plan(created["id"], archived["revision"])

    def test_plan_with_fulfilment_history_cannot_be_deleted(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "run"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        corrected = coach.set_planned_session_fulfilment(
            created["id"], created["revision"], ps_id, "skipped", "sick"
        )
        with self.assertRaises(InvalidRecord):
            coach.delete_plan(created["id"], corrected["revision"])

    def test_plan_mutations_reject_garmin_ids(self) -> None:
        coach, _ = _coach()
        with self.assertRaises(ReadOnlyRecord):
            coach.activate_plan("garmin:1", 1, "go")
        with self.assertRaises(ReadOnlyRecord):
            coach.delete_plan("garmin:1", 1)


class AdjustPlanTest(unittest.TestCase):
    def _plan_with_two(self) -> tuple[CoachData, dict[str, object], str, str]:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(2), "sport": "running", "prescription": "early"},
                {"scheduled_date": _d(40), "sport": "running", "prescription": "late"},
            ],
        )
        early = next(
            p["id"] for p in created["planned_sessions"] if p["scheduled_date"] == _d(2)
        )
        late = next(
            p["id"] for p in created["planned_sessions"] if p["scheduled_date"] == _d(40)
        )
        return coach, created, early, late

    def test_adjust_reschedules_future_session(self) -> None:
        coach, plan, _early, late = self._plan_with_two()
        adjusted = coach.adjust_plan(
            plan["id"],
            plan["revision"],
            "shift long run",
            _d(1),
            [
                {
                    "op": "update",
                    "planned_session_id": late,
                    "scheduled_date": _d(45),
                    "sport": "running",
                    "prescription": "long run 22k",
                }
            ],
        )
        self.assertEqual(adjusted["revision"], 2)
        moved = next(p for p in adjusted["planned_sessions"] if p["id"] == late)
        self.assertEqual(moved["scheduled_date"], _d(45))
        self.assertEqual(moved["prescription"], "long run 22k")

    def test_adjust_adds_and_cancels_future_sessions(self) -> None:
        coach, plan, _early, late = self._plan_with_two()
        adjusted = coach.adjust_plan(
            plan["id"],
            plan["revision"],
            "restructure future",
            _d(1),
            [
                {
                    "op": "add",
                    "scheduled_date": _d(50),
                    "sport": "running",
                    "prescription": "new tempo",
                },
                {"op": "cancel", "planned_session_id": late},
            ],
        )
        cancelled = next(p for p in adjusted["planned_sessions"] if p["id"] == late)
        self.assertEqual(cancelled["disposition"], "cancelled")
        self.assertEqual(len(adjusted["planned_sessions"]), 3)  # nothing erased

    def test_adjust_cannot_touch_session_before_effective_from(self) -> None:
        coach, plan, early, _late = self._plan_with_two()
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(
                plan["id"],
                plan["revision"],
                "edit the past",
                _d(30),
                [
                    {
                        "op": "update",
                        "planned_session_id": early,
                        "scheduled_date": _d(2),
                        "sport": "running",
                        "prescription": "rewritten",
                    }
                ],
            )

    def test_adjust_cannot_add_before_effective_from(self) -> None:
        coach, plan, _early, _late = self._plan_with_two()
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(
                plan["id"],
                plan["revision"],
                "sneak a past add",
                _d(30),
                [
                    {
                        "op": "add",
                        "scheduled_date": _d(20),
                        "sport": "running",
                        "prescription": "backdated",
                    }
                ],
            )

    def test_frozen_prescription_survives_later_revision_byte_for_byte(self) -> None:
        coach, plan, early, late = self._plan_with_two()
        before = next(p for p in plan["planned_sessions"] if p["id"] == early)
        adjusted = coach.adjust_plan(
            plan["id"],
            plan["revision"],
            "future only",
            _d(30),
            [
                {
                    "op": "update",
                    "planned_session_id": late,
                    "scheduled_date": _d(41),
                    "sport": "running",
                    "prescription": "late tweaked",
                }
            ],
        )
        after = next(p for p in adjusted["planned_sessions"] if p["id"] == early)
        self.assertEqual(after, before)

    def test_effective_from_must_not_precede_today_or_prior_boundary(self) -> None:
        coach, plan, _early, late = self._plan_with_two()
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(plan["id"], plan["revision"], "past eff", _d(-1), [])
        adjusted = coach.adjust_plan(
            plan["id"], plan["revision"], "first", _d(30), []
        )
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(
                adjusted["id"], adjusted["revision"], "regress", _d(10), []
            )

    def test_effective_from_must_lie_within_plan_range(self) -> None:
        coach, plan, _early, _late = self._plan_with_two()
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(plan["id"], plan["revision"], "oob", _d(500), [])

    def test_range_change_rejected_when_it_would_orphan_a_session(self) -> None:
        coach, plan, _early, _late = self._plan_with_two()
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(
                plan["id"],
                plan["revision"],
                "shorten",
                _d(1),
                [],
                ends_on=_d(20),  # would drop the day-40 session
            )

    def test_adjust_requires_expected_revision(self) -> None:
        coach, plan, _early, _late = self._plan_with_two()
        with self.assertRaises(RevisionConflict):
            coach.adjust_plan(plan["id"], 99, "stale", _d(1), [])

    def test_archived_plan_rejects_prescription_adjustment(self) -> None:
        coach, plan, _early, _late = self._plan_with_two()
        archived = coach.archive_plan(plan["id"], plan["revision"], "stop")
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(
                plan["id"], archived["revision"], "too late", _d(1), []
            )


class FulfilmentMatchingTest(unittest.TestCase):
    def _plan_and_session(self) -> tuple[CoachData, dict[str, object], str, dict[str, object]]:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "tempo"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        session = coach.create_session(
            {"sport": "running", "local_date": _d(3)}
        )
        return coach, created, ps_id, session

    def test_fulfilled_requires_a_match(self) -> None:
        coach, plan, ps_id, _s = self._plan_and_session()
        with self.assertRaises(InvalidRecord):
            coach.set_planned_session_fulfilment(
                plan["id"], plan["revision"], ps_id, "fulfilled", "done"
            )

    def test_non_fulfilled_rejects_matches(self) -> None:
        coach, plan, ps_id, session = self._plan_and_session()
        with self.assertRaises(InvalidRecord):
            coach.set_planned_session_fulfilment(
                plan["id"],
                plan["revision"],
                ps_id,
                "skipped",
                "should be empty",
                matches=[session["id"]],
            )

    def test_split_fulfilment_links_multiple_sessions(self) -> None:
        coach, plan, ps_id, first = self._plan_and_session()
        second = coach.create_session({"sport": "running", "local_date": _d(3)})
        corrected = coach.set_planned_session_fulfilment(
            plan["id"],
            plan["revision"],
            ps_id,
            "fulfilled",
            "split into two recordings",
            matches=[first["id"], second["id"]],
            fulfilment_note="watch died mid-run",
        )
        planned = corrected["planned_sessions"][0]
        self.assertEqual(planned["disposition"], "fulfilled")
        self.assertEqual(set(planned["matches"]), {first["id"], second["id"]})
        self.assertEqual(planned["fulfilment_note"], "watch died mid-run")

    def test_a_training_session_fulfils_at_most_one_planned_session(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "a"},
                {"scheduled_date": _d(4), "sport": "running", "prescription": "b"},
            ],
        )
        first, second = (p["id"] for p in created["planned_sessions"])
        session = coach.create_session({"sport": "running", "local_date": _d(3)})
        step = coach.set_planned_session_fulfilment(
            created["id"], created["revision"], first, "fulfilled", "done", [session["id"]]
        )
        with self.assertRaises(InvalidRecord):
            coach.set_planned_session_fulfilment(
                created["id"], step["revision"], second, "fulfilled", "reuse", [session["id"]]
            )

    def test_unlink_frees_the_training_session_for_reuse(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "a"},
                {"scheduled_date": _d(4), "sport": "running", "prescription": "b"},
            ],
        )
        first, second = (p["id"] for p in created["planned_sessions"])
        session = coach.create_session({"sport": "running", "local_date": _d(3)})
        step = coach.set_planned_session_fulfilment(
            created["id"], created["revision"], first, "fulfilled", "done", [session["id"]]
        )
        step = coach.set_planned_session_fulfilment(
            created["id"], step["revision"], first, "skipped", "actually a mismatch"
        )
        reassigned = coach.set_planned_session_fulfilment(
            created["id"], step["revision"], second, "fulfilled", "correct target", [session["id"]]
        )
        target = next(p for p in reassigned["planned_sessions"] if p["id"] == second)
        self.assertEqual(target["matches"], [session["id"]])

    def test_match_must_reference_an_existing_session(self) -> None:
        coach, plan, ps_id, _s = self._plan_and_session()
        with self.assertRaises(InvalidRecord):
            coach.set_planned_session_fulfilment(
                plan["id"], plan["revision"], ps_id, "fulfilled", "ghost", ["app:" + "0" * 32]
            )

    def test_duplicate_match_ids_are_rejected(self) -> None:
        coach, plan, ps_id, session = self._plan_and_session()
        with self.assertRaises(InvalidRecord):
            coach.set_planned_session_fulfilment(
                plan["id"], plan["revision"], ps_id, "fulfilled", "dup", [session["id"], session["id"]]
            )

    def test_correction_preserves_prescription_fields(self) -> None:
        coach, plan, ps_id, session = self._plan_and_session()
        before = plan["planned_sessions"][0]
        corrected = coach.set_planned_session_fulfilment(
            plan["id"], plan["revision"], ps_id, "fulfilled", "done", [session["id"]]
        )
        after = corrected["planned_sessions"][0]
        for field in ("scheduled_date", "sport", "prescription", "session_type"):
            self.assertEqual(after[field], before[field])

    def test_correction_allowed_on_archived_plan(self) -> None:
        coach, plan, ps_id, session = self._plan_and_session()
        active = coach.activate_plan(plan["id"], plan["revision"], "go")
        archived = coach.archive_plan(plan["id"], active["revision"], "season done")
        corrected = coach.set_planned_session_fulfilment(
            plan["id"], archived["revision"], ps_id, "fulfilled", "late match", [session["id"]]
        )
        self.assertEqual(corrected["status"], "archived")
        self.assertEqual(corrected["planned_sessions"][0]["disposition"], "fulfilled")

    def test_unknown_planned_session_raises_not_found(self) -> None:
        coach, plan, _ps, _s = self._plan_and_session()
        with self.assertRaises(NotFound):
            coach.set_planned_session_fulfilment(
                plan["id"], plan["revision"], "missing", "skipped", "no such session"
            )

    def test_correction_requires_expected_revision(self) -> None:
        coach, plan, ps_id, _s = self._plan_and_session()
        with self.assertRaises(RevisionConflict):
            coach.set_planned_session_fulfilment(
                plan["id"], 99, ps_id, "skipped", "stale"
            )


class GoalLinkageTest(unittest.TestCase):
    def test_plan_reference_goes_stale_when_goal_event_advances(self) -> None:
        coach, _ = _coach()
        event = _goal_event(coach)
        plan = _base_plan(
            coach,
            goal_events=[
                {"goal_event_id": event["id"], "goal_event_revision": event["revision"]}
            ],
        )
        coach.replace_goal_event(
            event["id"],
            event["revision"],
            {
                "name": "Autumn Marathon",
                "sport": "running",
                "local_date": _d(88),
                "priority": "secondary",
            },
        )
        reread = coach.get_plan(plan["id"])
        self.assertTrue(reread["requires_review"])
        self.assertEqual(reread["goal_events"][0]["stale_reason"], "revision_advanced")

    def test_activation_blocked_until_stale_reference_reviewed(self) -> None:
        coach, _ = _coach()
        event = _goal_event(coach)
        plan = _base_plan(
            coach,
            goal_events=[
                {"goal_event_id": event["id"], "goal_event_revision": event["revision"]}
            ],
        )
        updated_event = coach.replace_goal_event(
            event["id"],
            event["revision"],
            {
                "name": "Autumn Marathon",
                "sport": "running",
                "local_date": _d(88),
                "priority": "secondary",
            },
        )
        with self.assertRaises(InvalidRecord):
            coach.activate_plan(plan["id"], plan["revision"], "go")

        refreshed = coach.adjust_plan(
            plan["id"],
            plan["revision"],
            "review goal reference",
            _d(1),
            [],
            goal_events=[
                {
                    "goal_event_id": event["id"],
                    "goal_event_revision": updated_event["revision"],
                }
            ],
        )
        self.assertFalse(refreshed["requires_review"])
        activated = coach.activate_plan(plan["id"], refreshed["revision"], "go")
        self.assertEqual(activated["status"], "active")


class ReferentialIntegrityTest(unittest.TestCase):
    def test_referenced_goal_event_cannot_be_deleted(self) -> None:
        coach, _ = _coach()
        event = _goal_event(coach)
        _base_plan(
            coach,
            goal_events=[
                {"goal_event_id": event["id"], "goal_event_revision": event["revision"]}
            ],
        )
        with self.assertRaises(ReferencedRecord):
            coach.delete_goal_event(event["id"], event["revision"])

    def test_matched_session_cannot_be_deleted(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "run"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        session = coach.create_session({"sport": "running", "local_date": _d(3)})
        coach.set_planned_session_fulfilment(
            created["id"], created["revision"], ps_id, "fulfilled", "done", [session["id"]]
        )
        with self.assertRaises(ReferencedRecord):
            coach.delete_session(session["id"], session["revision"])

    def test_historical_match_still_blocks_deletion(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "run"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        session = coach.create_session({"sport": "running", "local_date": _d(3)})
        step = coach.set_planned_session_fulfilment(
            created["id"], created["revision"], ps_id, "fulfilled", "done", [session["id"]]
        )
        # Unlink at the head, but a prior revision still points at the session.
        coach.set_planned_session_fulfilment(
            created["id"], step["revision"], ps_id, "skipped", "mismatch"
        )
        with self.assertRaises(ReferencedRecord):
            coach.delete_session(session["id"], session["revision"])

    def test_unreferenced_records_remain_deletable(self) -> None:
        coach, _ = _coach()
        event = _goal_event(coach)
        session = coach.create_session({"sport": "running", "local_date": _d(3)})
        self.assertTrue(coach.delete_goal_event(event["id"], event["revision"])["deleted"])
        self.assertTrue(coach.delete_session(session["id"], session["revision"])["deleted"])


class StoredIntegrityTest(unittest.TestCase):
    def test_malformed_stored_plan_is_reported(self) -> None:
        coach, tmp = _coach()
        created = _base_plan(coach)
        stem = created["id"].split(":", 1)[1]
        path = Path(tmp) / "app" / "plans" / f"{stem}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["content"]["revisions"][0]["plan"]["status"] = "bogus"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(DataIntegrityError):
            coach.get_plan(created["id"])

    def test_revision_count_must_match_envelope(self) -> None:
        coach, tmp = _coach()
        created = _base_plan(coach)
        stem = created["id"].split(":", 1)[1]
        path = Path(tmp) / "app" / "plans" / f"{stem}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["revision"] = 5  # disagrees with the single stored revision
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(DataIntegrityError):
            coach.get_plan(created["id"])

    def test_missing_snapshot_field_is_reported(self) -> None:
        coach, tmp = _coach()
        created = _base_plan(coach)
        stem = created["id"].split(":", 1)[1]
        path = Path(tmp) / "app" / "plans" / f"{stem}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        del record["content"]["revisions"][0]["plan"]["name"]
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(DataIntegrityError):
            coach.get_plan(created["id"])

    def test_malformed_planned_session_is_reported(self) -> None:
        coach, tmp = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "run"}
            ],
        )
        stem = created["id"].split(":", 1)[1]
        path = Path(tmp) / "app" / "plans" / f"{stem}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        planned = record["content"]["revisions"][0]["plan"]["planned_sessions"][0]
        del planned["prescription"]  # required field gone
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(DataIntegrityError):
            coach.get_plan(created["id"])


class ReviewHardeningTest(unittest.TestCase):
    def test_fulfilment_correction_cannot_resurrect_a_cancelled_session(
        self,
    ) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(30), "sport": "running", "prescription": "x"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        cancelled = coach.adjust_plan(
            created["id"],
            created["revision"],
            "withdraw future work",
            _d(1),
            [{"op": "cancel", "planned_session_id": ps_id}],
        )
        self.assertEqual(cancelled["planned_sessions"][0]["disposition"], "cancelled")
        session = coach.create_session({"sport": "running", "local_date": _d(30)})
        with self.assertRaises(InvalidRecord):
            coach.set_planned_session_fulfilment(
                created["id"],
                cancelled["revision"],
                ps_id,
                "fulfilled",
                "try to revive it",
                [session["id"]],
            )

    def test_fulfilment_correction_revision_blocks_delete(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "run"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        # A scheduled+note correction still counts as fulfilment history.
        corrected = coach.set_planned_session_fulfilment(
            created["id"],
            created["revision"],
            ps_id,
            "scheduled",
            "note only",
            fulfilment_note="athlete asked about pacing",
        )
        with self.assertRaises(InvalidRecord):
            coach.delete_plan(created["id"], corrected["revision"])

    def test_matched_garmin_session_reports_read_only_not_referenced(self) -> None:
        coach, tmp = _coach()
        activities = [
            {
                "activity_id": "fixture-run",
                "start_local": f"{_d(3)} 07:00:00",
                "type": "running",
                "duration_s": 1800,
            }
        ]
        path = Path(tmp) / "derived" / "activities.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(activities), encoding="utf-8")
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(3), "sport": "running", "prescription": "run"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        coach.set_planned_session_fulfilment(
            created["id"],
            created["revision"],
            ps_id,
            "fulfilled",
            "garmin recorded it",
            ["garmin:fixture-run"],
        )
        # Read-only rejection takes precedence over the reference guard.
        with self.assertRaises(ReadOnlyRecord):
            coach.delete_session("garmin:fixture-run", 1)

    def test_malformed_adjustment_operations_are_rejected(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(30), "sport": "running", "prescription": "x"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        # Non-hashable op must raise InvalidRecord, not an unhandled TypeError.
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(
                created["id"], created["revision"], "bad op", _d(1), [{"op": []}]
            )
        # Cancel silently ignoring unknown fields is rejected like add/update.
        with self.assertRaises(InvalidRecord):
            coach.adjust_plan(
                created["id"],
                created["revision"],
                "extra field",
                _d(1),
                [{"op": "cancel", "planned_session_id": ps_id, "sport": "running"}],
            )

    def test_earlier_revision_snapshots_are_immutable(self) -> None:
        coach, _ = _coach()
        created = _base_plan(
            coach,
            planned_sessions=[
                {"scheduled_date": _d(40), "sport": "running", "prescription": "late"}
            ],
        )
        ps_id = created["planned_sessions"][0]["id"]
        original = coach.get_plan_history(created["id"])["revisions"][0]
        coach.adjust_plan(
            created["id"],
            created["revision"],
            "move it later",
            _d(1),
            [
                {
                    "op": "update",
                    "planned_session_id": ps_id,
                    "scheduled_date": _d(50),
                    "sport": "running",
                    "prescription": "late tweaked",
                }
            ],
        )
        history = coach.get_plan_history(created["id"])
        self.assertEqual(history["revisions"][0], original)
        self.assertEqual(
            history["revisions"][0]["plan"]["planned_sessions"][0]["scheduled_date"],
            _d(40),
        )
        self.assertEqual(
            history["revisions"][1]["plan"]["planned_sessions"][0]["scheduled_date"],
            _d(50),
        )


if __name__ == "__main__":
    unittest.main()
