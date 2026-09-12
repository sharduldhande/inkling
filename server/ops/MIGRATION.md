# v1 → v2 migration (§17) — execute in order

Working checklist for cutting over from habit-enforcer-server (v1) to
inkling-server (v2). Status reflects what the build has already covered;
unchecked boxes need the operator (hardware, credentials, or judgment).

## 1. Prereqs
- [ ] `dmesg | grep -i oom` on the box — if there's memory pressure history,
      do the 2 GB resize BEFORE the parallel run (§15).
- [x] Repos created (inkling-server, inkling-data).
- [ ] **Production Neon project**: the `inkling` project
      (example-project-123456, us-east-1) exists with the `inkling-test`
      branch used by CI. Decide whether its default branch is production or a
      fresh project is; then `python -m inkling migrate` and seed users:
      `python -m inkling seed --user shardul --name ... --tz America/New_York
      --ntfy-topic ... --inference-profile '{...}'`.

## 2. Build with calendar off; verify against the iOS app
- [x] §5–§11 built; 429 body, bearer auth, SSE frames, /history pinned
      byte-exact in `api/v1_contract.py` (copied from the live v1 server.js —
      §19.6 satisfied; contract tests in tests/test_api.py).
- [ ] Point the iOS app at a test vhost and exercise it manually.

## 3. Import v1 data (`scripts/import_v1.py`, owner URL, per user)
- [ ] `... transcript --user shardul --file transcript.jsonl`
- [ ] `... docs --user shardul --goals goals.md --plan daily_plan.md`
- [ ] `... parse --file log.md --out proposed.json` (defaults to last 3
      months — §19.5), then **review by hand** (fix kinds/habit ids, drop
      noise; an LLM pass over the JSON is fine as a drafting aid), then
      `... load --user shardul --file reviewed.json`.
- [ ] Initial working_state + commitments: NOT scripted, by design. Draft
      working_state from log.md + the recent session, then REVIEW MANUALLY —
      every open loop and promise must land (§17.3). Seed via a chat turn or
      a docs INSERT as 'human'.
- [ ] Archive the rest of log.md in the repo (import more later if wanted).

## 4. Parallel run ≥ 1 week
- [ ] ~~v2 notifications on a SECOND ntfy topic~~ (superseded: v2 pushes via APNs to the app; v1 keeps ntfy).
- [ ] Daily compare: did v2 surface everything v1 knew? Is consolidation
      output sane? (Nightly export diffs in inkling-data make this a
      `git log -p` read.)

## 5. Cutover
- [ ] `crontab -l > crontab-backup.txt` (capture BEFORE editing).
- [ ] Remove ONLY legacy Inkling entries from crontab (v2 has none — §15).
- [ ] Switch the Caddy vhost to v2 (deploy/Caddyfile.snippet), reload.
- [ ] ~~Point users.ntfy_topic at the real topics.~~ (superseded by APNs; column is legacy)
- [ ] Stop v1 server.js; archive its session dir.

## 6. Prove expendability
- [ ] Run ops/RUNBOOK.md rebuild-from-zero against a scratch environment once.
- [ ] Run the restore drill once by hand:
      set RESTORE_DRILL_URL, then trigger via the jobs role (or wait for the
      monthly run) and check audit_log for `restore_drill_ok`.
