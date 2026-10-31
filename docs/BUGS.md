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
| intent eval (SGD) | "Yes, can you please make me an appointment on the 1st?" was read as a confirmation: any message starting with "yes" counted. A yes must now be short and unhedged, and the same check guards approvals | `AffirmationTest`, intent-eval gate (zero confirm false positives) |
| intent eval (CLINC150) | "i would like to book a place to stay in phoenix" started a home-service booking; 41% of out-of-scope requests became actions | intent-eval gate (out-of-scope false actions ≤ 5%) |
| intent eval (handwritten) | "nahi, abhi mat karo" (no, don't do it now) became a date change because "abhi" was read as today | `RegressionTest.test_abhi_mat_karo_is_a_decline` |
| hard probes (E8) | **Safety:** "yes, unless it's Sharma" with Sharma's approval card open booked Sharma. The code-level yes check, the backstop when the model misreads, had no conditional or exception words. Added unless, except, only, provided, as long as, assuming, otherwise, warna, sirf, jab tak, "na ho" | `ApprovalGuardTest` (every hard-probe trap and positive control) |
| hard-probe setup | The agent says 'Say "no preference" to skip', but the offline parser read "no preference" as a decline ("Nothing is waiting for your approval") and the search didn't start. Present since v1; no scenario typed it | `RegressionTest.test_no_preference_skips_optional_fields_not_a_decline` |
| intent eval (synthetic) | "day after tomorrow, around 3pm" became "after 3pm" | `RegressionTest.test_day_after_tomorrow_is_not_an_after_bound` |
| hard probes (E28) | An earliest-time preference arriving as `after:10:00` (a valid time in another format) was rejected and not saved. Values are now normalised (`after:10:00`, `10`, `10am`, `10 a.m.` all become `10:00`); anything unreadable is refused out loud and traced as `memory_rejected` | S39 |
| hard probes (E14, H15) | When one message held two requests ("cancel my AC servicing and get an electrician the same day"), only one was carried forward and the other was lost. The intent now carries `also_requested`; the agent queues it, says so, and offers it as soon as the first request is done | S37; probes E14, H15, E15, H16, E25, H32 |
| hard probes (E1, E6) | "Not Arctic Breeze" or "anyone but BrightSpark" had nowhere to go in the intent, so a ruled-out provider could still be offered. The intent now carries `exclude_providers`; offers skip them, and ruling out the provider on an open approval card voids it | S36; probes E32, H40, E33, H41 |
| hard probes (E26) | **Data loss:** "forget everything except my area" also forgot the area, because forget-all deleted immediately and could not keep anything. It now lists exactly what will be removed, keeps what the user names (`memory_keep`), and waits for a clear yes | S38 |
| hard probes (E17, H19) | "The same guy as my AC service, but for the fan" was treated as an AC job: the service followed the provider reference instead of the thing to be fixed. The interpreter guidance now says the object decides the service and a provider of another service is not carried over; the offline parser reads "but for the fan" and "pankhe ke liye" | probes E17, H19 |
| hard probes (E2) | A misspelt service word ("plumbr") could be taken as a provider name. The interpreter guidance now limits provider names to businesses and people | probe E2 |
| hard probes (H35) | The Hinglish "10 baje se pehle kabhi mat bhejna" (never send anyone before 10) was not recognised as a standing preference | probe H35; offline parser and interpreter guidance |
| hard probes (E3) | A deadline ("fixed before he comes on Thursday") could only be expressed as a single day, so it became Thursday itself. Dates now support `before:weekday:thu` and `before:date:YYYY-MM-DD`, resolved to today through the day before | S40; probes E3, E34, H42 |
| hard-probe judge | In the evaluation itself: a substring check flagged `day_after_tomorrow` as the forbidden `tomorrow`, reporting two correct answers as misses. Symbolic fields are now compared exactly | probes E1, H1 |
