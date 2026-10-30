# Three real bugs, and how they are guarded now

All three were found by the eval suite or the seeded fuzz while building, not by reading code. Each entry gives
what broke, why, the fix and the regression test. The commit history shows the fixes landing
(`git log --oneline`).

## 1. A crashed turn's number was reused, so the trace credited its approval to the next turn

**What broke.** S22 and S23 (restart mid-booking) failed invariant I7: "approval granted on non-affirmative input
`''`". The approval had been granted by the user's "yes"; the trace said it was granted during the recovery
turn, which has no user text.

**Why.** `state.turn` was incremented in memory and saved only at the end of the turn. The injected crash ended
the turn before the save, so the recovery turn reloaded the old number and reused it. Two different turns
shared one `(run_id, turn)`, and so did the `UserTurnRef` that authorises approvals and memory writes.

**Fix.** Persist the incremented turn number before doing any work (`AgentService.handle`). A crashed turn
still consumes its number.

**Regression tests.** S22, S23 and invariant I7, which runs after every scenario and fuzz seed.

**Lesson.** Anything that identifies a unit of work must be durable before the work starts, or a crash makes
two units indistinguishable.

## 2. One taken 09:00 slot removed every provider's 09:00 offer

**What broke.** S12 (slot taken at booking time) failed "alternatives offered": after the provider said
`slot_taken`, the agent replied "No plumbers have free slots that match", with three other plumbers free.

**Why.** Failed slots were remembered in `excluded_slots` as bare start times. Every plumber in the seeded world
had a 09:00 opening, so excluding `2026-11-02T09:00` excluded all of them; and each offer only carried its
first fitting slot, so the providers vanished instead of moving to their next slot.

**Fix.** Exclusions are keyed by provider and time (`policy.slot_key`), and applied inside
`policy.build_offers` when choosing each provider's first slot, so a provider falls back to its next free slot.

**Regression tests.** `tests/test_core.py::PolicyTest::test_regression_excluding_a_slot_is_per_provider` and S12.

**Lesson.** A key that identifies something must include every dimension that makes it unique; tests with
realistic, overlapping data found what a hand-written example would not.

## 3. Forgetting a fact in one session left it alive in another open session

**What broke.** The seeded fuzz (300 seeds) failed invariant I5 on four seeds. The user said "forget my area" in
one session; another session that had already pre-filled the area from memory kept using it.

**Why.** "Forget" tombstoned the fact and cleared slots in the *current* session's state only. Other sessions
store their own snapshot of state, including slots that came from memory, and nothing re-checked them.

**Fix.** At the start of every turn, `AgentService._drop_forgotten` removes any slot whose source fact is no longer
active, voids a pending approval that relied on it, and clears the search it fed. I5 was extended to check every
saved session state, not only memory-use events.

**Regression tests.** S33 ("forget in one session; another open session drops it too"), I5, and the fuzz.

**Lesson.** A cached copy of a fact needs a revalidation point; "forgotten facts are never reused" has to hold
for every reader, not just the one that heard the request.

## Also found and fixed

| Found by | Bug | Guard |
| --- | --- | --- |
| S27 | `emit("error", type=...)` overwrote the event's own `type`, so errors were written as `"type": "ProviderFailure"` and the trace looked clean | the tracer now rejects envelope field names; `TracingTest.test_regression_fields_cannot_overwrite_the_envelope` |
| S16 | a second "yes, book it" fell through to task detection and started a new booking | `RulesTest.test_regression_repeated_yes_is_not_a_new_booking`, S16 |
| fuzz | seeded id generators restarted from the same seed after a restart and reissued approval ids (`IntegrityError`) | a persisted boot counter is mixed into the seed; fuzz |
| fuzz | a budget change during a reschedule read `selection["price"]`, which only booking selections have | fuzz (it now also fails on any hidden exception) |
| unit test | "cancel my plumber booking" was routed to "list my bookings" | `RulesTest.test_regression_cancel_is_not_list_bookings` |
