# §18 Acceptance criteria — status

Definition of Done, bullet by bullet. "Test" = automated, runs in the suite
against the Neon test branch. "Operational" = needs the box/credentials;
blocked on §19 human decisions, not on code.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | §4.11 isolation; RLS FORCEd; runtime role can't UPDATE/DELETE events or DDL | ✅ Test | tests/test_isolation.py (incl. BYPASSRLS guard, grant probes) |
| 2 | Kill -9 mid-turn → requeue/error per policy; no lost chat; no duplicate writes | ✅ Test | tests/test_acceptance.py::test_dead_worker_mid_turn…; tests/test_turns.py (reaper, retry caps, per-turn idempotency keys) |
| 3 | 3 h outage across a due reminder → fires late, nothing dropped (clock injection) | ✅ Test | tests/test_scheduler.py::test_past_due_wakeup_fires_late_on_catch_up, catch-up occurrence tests (refresh/fire_due take `now=`) |
| 4 | Neon CU over 48 h soak consistent with timer-based behavior | ⏳ Operational | Run after deploy: Neon console → project CU graph over 48 h idle-ish use. Design basis: no polling loops except 60 s reconciles in split-role mode; in-process events otherwise |
| 5 | v1 iOS app works unmodified (manual + contract tests) | ✅ Test / ⏳ manual | api/v1_contract.py pinned byte-exact (429 body, SSE frames, /history); tests/test_api.py. Manual pass on a test vhost = MIGRATION.md §2 |
| 6 | Module-1/hw2: open child ⇒ clarifying question, not closure, end to end | ✅ Test | tests/test_acceptance.py::test_module1_hw2…; unit level in tests/test_commands.py |
| 7 | "Remind me in 10 weeks" → correct fire_at; time-travel fires it | ✅ Test | tests/test_acceptance.py::test_remind_me_in_10_weeks… |
| 8 | Backup + restore drill green; induced failure alerts | ✅ Test (stub-edge) / ⏳ live | tests/test_backup.py (pipeline, retention, checksum, drift, alerts). Live run needs S3 bucket + gpg key (§19.3) + RESTORE_DRILL_URL; then MIGRATION.md §6 |
| 9 | Rebuild-from-zero executed once | ⏳ Operational | Procedure: ops/RUNBOOK.md; execute against a scratch VPS at cutover (MIGRATION.md §6) |

## What the ⏳ items are waiting on (§19)

- S3-compatible bucket + credentials, and a GPG keypair (public key on box).
- Google OAuth client (only if calendar is wanted; system runs with it off).
- The production Neon decision (use project `inkling`'s default branch, or a
  fresh project) + `migrate` + `seed`.
- A VPS to deploy to, for the soak, iOS manual pass, and rebuild drill.

## Deliberately deferred beyond DoD (documented in code)

- Escalation per notify_policy (repeat/escalate_after_min) and
  `silent_summary`/day-start folding — both need day-start machinery that §13
  doesn't define concretely; acknowledged_at plumbing (§11) is in place.
- Rate-limit honest-degradation in inference/gateway.py (TODO): a 429 from
  the model API currently surfaces as a turn error rather than a "brain is
  busy" notice.
- google.py is live-API-untested until credentials exist (adapter logic is
  stub-tested).
