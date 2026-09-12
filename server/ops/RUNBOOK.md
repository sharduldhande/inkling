# Inkling RUNBOOK

Operational notes for `inkling-server`. The prime directive (§0): the box is
expendable — everything below must be reconstructible from this file, the git
repos, the Neon database, and the S3 backups.

## Rebuild from zero (§15 — execute once as an acceptance test)

Fresh VPS (Debian/Ubuntu assumed) to a running service:

1. **Packages**
   ```sh
   sudo apt-get update && sudo apt-get install -y git curl gnupg postgresql-client caddy
   curl -LsSf https://astral.sh/uv/install.sh | sh   # uv (installs python too)
   ```
2. **Clone**
   ```sh
   mkdir -p ~/inkling/releases
   git clone https://github.com/sharduldhande/inkling-server.git ~/inkling/releases/bootstrap
   ln -sfn ~/inkling/releases/bootstrap ~/inkling/current
   git clone <inkling-data remote> ~/inkling-data   # markdown export repo
   ```
3. **Secrets restore** (§16 — from the password manager / offline copy; never in git)
   ```sh
   mkdir -m 700 ~/.inkling
   # Restore, each chmod 600:
   #   ~/.inkling/env                  (from .env.example, fill real values)
   #   ~/.inkling/chat-tokens.json     {"<user>": "<bearer>"}
   #   ~/.inkling/apns-AuthKey_<ID>.p8 APNs provider key (Apple Developer → Keys;
   #                                   downloadable ONCE — the password manager copy is canonical)
   #   ~/.inkling/claude-<user>/       per-user CLAUDE_CONFIG_DIR (subscription auth)
   #   ~/.inkling/anthropic-<user>.key per-user API keys (metered auth)
   #   ~/.inkling/google-<user>.json   calendar OAuth (only if calendar on)
   #   ~/.inkling/backup-gpg/          gpg PUBLIC key; import: gpg --import
   # The gpg PRIVATE key lives offline only — the box encrypts, never decrypts.
   ```
4. **Database**
   - Existing Neon project: nothing to do — state lives there, not on the box.
   - True disaster (Neon gone): create project + roles, then restore the
     latest dump (see "Restore from backup" below), then `python -m inkling
     migrate` to bring the schema current.
5. **Deploy**
   ```sh
   cd ~/inkling/current && bash deploy/deploy.sh
   ```
6. **Service + vhost**
   ```sh
   mkdir -p ~/.config/systemd/user && cp deploy/inkling.service ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now inkling
   loginctl enable-linger $USER
   # Merge deploy/Caddyfile.snippet into /etc/caddy/Caddyfile; systemctl reload caddy
   ```
7. **Verify**: `curl https://<vhost>/health` → `{"status":"ok","busy":false}`;
   send a chat message from the app; `journalctl --user -u inkling -f`.

## Daily operations

- **Logs**: `journalctl --user -u inkling -f` (JSON lines when LOG_FORMAT=json).
  Per turn: token counts (`assembled context`), accept/reject (`applied N
  command(s)`), roll-up (`turn N done: ... duration=`).
- **Health**: `GET /health`. Busy=true for >90 s means a stuck lease; the
  reaper clears it on the next worker tick — investigate only if it repeats.
- **Alerts** land on the OPERATOR_NTFY topic: dead-lettered deliveries,
  backup failures, restore-drill failures.
- **Dead letters**: `SELECT * FROM deliveries WHERE status='dead_letter'` (as
  owner). At-least-once is the contract; a dead letter means every retry
  failed — usually APNs config (key/id/topic, VPS clock skew →
  InvalidProviderToken), a user with no live `devices` row (app not opened
  since install, or every token revoked), or the calendar credential.
- **Devices**: `SELECT user_id, environment, left(token,8), revoked_at,
  revoke_reason FROM devices` (as owner). A revoked row revives when the app
  next launches. If a phone moves to another user's bearer token its POST is
  refused (409) and the OLD user's row keeps receiving until you
  `UPDATE devices SET revoked_at=now(), revoke_reason='operator'` it.

## Backup & restore (§13.2/§13.3)

- Nightly 03:10 server time: `pg_dump -Fc` → gpg (recipient §16) → S3
  `daily/` (+`weekly/` Sundays, `monthly/` on the 1st), retention 7/8/12,
  then markdown export commit+push to EXPORT_GIT_DIR. Success writes an
  `audit_log` row (`backup_ok`) with size + sha256.
- **Restore drill** runs monthly (1st, 04:10) against RESTORE_DRILL_URL — a
  standing scratch Neon branch. Create it once:
  `neonctl branches create --project-id <id> --name restore-drill`.
- **Restore from backup** (the real thing):
  ```sh
  # fetch newest daily from S3, then:
  gpg --decrypt inkling-<stamp>.dump.gpg > inkling.dump   # needs OFFLINE private key
  pg_restore --clean --if-exists --no-owner --no-privileges -d "$DATABASE_URL_MIGRATE" inkling.dump
  ```

## Token / credential rotation (§16)

- Bearer token: edit chat-tokens.json + the iOS app setting; restart.
- Anthropic key / Claude config dir: replace the file/dir; per-turn pickup, no restart.
- Google OAuth: re-run the consent flow into google-<user>.json.
- Neon passwords: rotate in Neon console, update ~/.inkling/env, restart.

## Known sharp edges

- Never point DATABASE_URL at an owner role: Neon owners carry BYPASSRLS and
  the whole isolation model (§3) rests on runtime_role. A guard test exists,
  but the env file is the first line of defense.
- The test suite DROPS the public schema of whatever TEST_DATABASE_URL points
  at (it refuses URLs without "test" in host/db — don't get creative).
- Roles may run split (`api`/`worker`/`scheduler`/`delivery`/`calendar`/
  `jobs`), but then in-process nudges are gone: everything degrades to each
  loop's 60 s reconcile scan. That's designed, just slower.
- systemd/cron read no rc files: PATH comes from the unit, env from
  EnvironmentFile. No crontab entries, ever — timers live in the process.
