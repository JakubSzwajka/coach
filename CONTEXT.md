# Garmin Coach

Language shared by the collector, visualization, and external AI coaches that use the athlete's training record.

## Language

**Collected Record**:
A read-only record imported from Garmin or another external source. Garmin Coach preserves its source payload rather than mutating it.
_Avoid_: Garmin data, source-owned data

**App Record**:
A durable record authored inside Garmin Coach by the athlete or coach, such as a manual session, goal event, or training plan. It may be changed or deleted through coach tools.
_Avoid_: app-owned data, mutable data

**Derived Record**:
A regenerable projection computed from Collected Records for analysis or presentation. It is never the authority for mutable App Records.
_Avoid_: cache, normalized data

**Training Session**:
A completed bout of physical training, whether imported as a Derived Record or logged as an App Record. Planned work is not a Training Session until it is performed.
_Avoid_: Activity, workout, completed workout

**Sport**:
The physical discipline performed in a Training Session, such as running, bouldering, strength, or cycling.
_Avoid_: Activity type

**Session Type**:
An optional sport-specific description of a Training Session's format or coaching purpose, such as easy run, intervals, technique, or max strength. It remains unknown when the source does not establish it.
_Avoid_: Garmin type

**Session RPE**:
The athlete's optional 1–10 report of whole-session perceived exertion. It is the common cross-sport intensity signal, not a value inferred from device metrics.
_Avoid_: Intensity score

**Session Load**:
A source-reported training-load measurement identified by its method and unit. Loads with different methods are not treated as comparable, and an absent load remains unknown.
_Avoid_: Unified load score

**Session Annotation**:
An App Record that adds notes or reliability and duplicate flags to a Collected Record's Training Session without changing its source values.
_Avoid_: Correction, override

**Goal Event**:
An App Record for a named competition on an event-local calendar date that anchors training plans. It requires a Sport and Goal Event Priority; exact timing, distance, Goal Event Goal, and Goal Event Outcome may remain unknown, and recording an outcome never turns it into a Training Session.
_Avoid_: Race, calendar event

**Goal Event Priority**:
The explicit planning intent `primary`, `secondary`, or `practice`: a primary event may shape the plan, a secondary event is accommodated without undermining primary events, and a practice event is treated as rehearsal or training. It never triggers automatic periodization by itself.
_Avoid_: A race, B race, C race

**Goal Event Goal**:
An optional explicitly recorded desired result, expressed as a target duration and/or a statement. It is never inferred from training data or presented as a prediction.
_Avoid_: Prediction, prescription

**Goal Event Outcome**:
An optional observed result recorded after participation, expressed as an actual duration and/or a statement. Its absence means unknown, not unsuccessful.
_Avoid_: Race result

**Goal Event Status**:
The event's explicit planning lifecycle: `scheduled`, `completed`, or `cancelled`; time passing never changes it automatically. It does not represent registration, placement, or other race-management facts.
_Avoid_: Registration status

**Training Plan**:
A revisioned App Record for one athlete that holds an inclusive date range, explicit Plan Constraints, Goal Event references, and dated Planned Sessions. It is `draft`, `active`, or `archived`; at most one is active and archiving is terminal.
_Avoid_: Schedule, workout plan

**Plan Revision**:
An immutable, reasoned snapshot of a Training Plan's prescriptions and separately tracked fulfilment state, recorded from an effective date. Later revisions may adjust unfrozen prescriptions or audit fulfilment corrections but never rewrite earlier revisions.
_Avoid_: Edit, version

**Plan Constraint**:
An explicitly recorded boundary that a coach must consider when composing or revising a Training Plan, such as available days or maximum session duration. It is advisory context, not an executable scheduling rule.
_Avoid_: Validation rule

**Planned Session**:
A stable, dated prescription inside one Training Plan that remains distinct from any completed Training Session. Its prescription identity persists across Plan Revisions while its explicitly recorded fulfilment may link one or more completed Training Sessions.
_Avoid_: Workout, Training Session

**Planned Session Disposition**:
The explicit lifecycle state `scheduled`, `fulfilled`, `skipped`, or `cancelled`; time passing never changes it automatically. Fulfilment requires a current Training Session match, skipped means prescribed but not performed, and cancelled means explicitly withdrawn.
_Avoid_: Completion status, missed
