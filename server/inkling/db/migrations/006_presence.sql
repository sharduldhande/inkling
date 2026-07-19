-- Presence (life-coach heartbeat): per-user pacing guardrails for scheduler-
-- synthesized check-ins. min_gap is the floor (no presence fire within this of
-- the last finished turn); max_gap is the dead-man ceiling (if nothing else is
-- due to fire within it during waking hours, the scheduler checks in). NULL
-- max_gap = presence off — per-user opt-in.
-- No new grants: runtime_role already has SELECT on users, and presence
-- triggers ride the existing triggers INSERT grant.

ALTER TABLE users ADD COLUMN presence_min_gap interval NOT NULL DEFAULT '45 minutes';
ALTER TABLE users ADD COLUMN presence_max_gap interval;
