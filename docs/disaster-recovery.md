# Disaster Recovery Plan — lap-backend (T186)

Companion to `docs/error-budget.md` (steady-state SLOs) and
`docs/secret-rotation.md` (credential lifecycle). This plan covers
the rare but high-impact events: data loss, region failure, and
recovery from compromised state.

## Recovery objectives

| Class | RTO (max time to restore service) | RPO (max acceptable data loss) |
|-------|-----------------------------------|---------------------------------|
| **Postgres data loss / corruption** | 4 hours | 24 hours (last successful nightly pg_dump) |
| **R2 backup-bucket loss** | 24 hours | n/a (bucket is the recovery artifact, not the source of truth) |
| **R2 telemetry-blob loss** | 24 hours | varies (driver-local re-upload possible) |
| **Redis loss** | 1 hour | 0 (cache + ephemeral state — no durable data) |
| **Application/deploy failure** | 30 minutes | 0 (stateless redeploy) |
| **Single-region cloud outage** | 4 hours (manual cutover) | 24 hours (cross-region async copy) |
| **DNS / TLS / CDN** | 1 hour (provider escalation) | 0 |

The 24-hour RPO is the budget that buys us the simplicity of
"daily pg_dump → R2". Tightening to ≤1h would require continuous
WAL archiving (out of scope until M2 — see a future M2 cross-region ADR).

## Failure-mode playbooks

### 1. Postgres data loss / corruption

**Detection.**
- App-layer 5xx spike with SQLState 40000 / 25P01 / 53400 in
  Sentry.
- `/healthz` returns 200 but `/readyz` 503 (DB probe in
  `app/main.py:readyz` — currently returns hardcoded 200; see
  TODO below).
- Manual report: driver sees their session disappear or a
  prescription gets rolled back.

**Containment.**
1. **Put the API in read-only mode.** Set
   `LAP_API_READ_ONLY=true` env var if implemented; otherwise
   roll the deploy back to a healthy revision and gate writes
   in app config (PR-level — out of scope for the runbook to
   pre-build, but the deploy mechanism is documented in
   `docs/deploy.md` when that exists).
2. **Snapshot the broken state.** Take a manual `pg_dump` of
   the corrupt DB into a side bucket (`pg-emergency/`) before
   any restore — destroys evidence otherwise.
3. **Notify users.** Status page + in-app banner: "Sessions
   may be temporarily inaccessible — investigating." Do not
   promise recovery time until step 4 completes.

**Recovery.**
1. **Identify the most recent clean dump.** R2 path:
   `pg-backups/YYYY-MM-DD/lap-backend.dump`. If yesterday's
   dump is the corruption itself (e.g., bad migration
   bundled), step back to the day before. The drill script
   (`scripts/restore_drill.py`, T185) verifies these dumps
   are restorable; if the drill went green within the last 7
   days, proceed.
2. **Run the restore.**
   ```sh
   python scripts/restore_db.py \
     --key pg-backups/YYYY-MM-DD/lap-backend.dump \
     --target $RECOVERY_DATABASE_URL
   ```
   `RECOVERY_DATABASE_URL` is a fresh Postgres instance, not
   the corrupt one. Restore time: ~15–30 minutes for our v1
   data volumes.
3. **Cut traffic over.** Update the `DATABASE_URL` env var on
   the running app to point at the restored DB. Rolling
   restart.
4. **Communicate the data-loss window.** Users with sessions
   uploaded between the dump time and the corruption window
   will not see those sessions. Send an email to affected
   accounts (run a query: `SELECT user_id FROM sessions WHERE
   created_at BETWEEN dump_time AND corruption_time` — but
   that query runs against the corrupt DB; alternatively,
   announce the window publicly and let drivers re-upload).

**Post-incident.**
- Postmortem within 5 business days (linked from this doc's git
  log per `docs/error-budget.md`).
- Confirm the root cause is fixed before deleting the
  emergency `pg-emergency/` snapshot — keep it 30 days.

### 2. R2 backup-bucket loss

The backup bucket is single-region. If Cloudflare loses the
bucket (provider incident or accidental deletion), our DR
mechanism for Postgres is gone until backups resume.

**Containment.**
1. **Pause the daily backup task** so it doesn't write into
   a recreated bucket and create false confidence in stale
   data.
2. **Stop the drill** (T185) — it'll start failing loudly,
   which is fine, but avoid alert fatigue by suppressing
   pages.

**Recovery.**
1. **Recreate the R2 bucket** with the same name and apply
   the lifecycle policy (retention pruning) from
   `docs/r2-config.md` (TODO: doc not yet written; pull
   policy from current bucket via `aws s3api
   get-bucket-lifecycle-configuration`).
2. **Trigger an immediate dump.** Manually run the daily
   backup task once so we have a fresh artifact rather than
   waiting up to 24h for the next scheduled run.
3. **Resume normal cadence.** Unsuppress the daily task and
   the drill.

The asymmetry: this scenario has no RPO because the bucket
is the *recovery* artifact, not the *source* of truth. We
still have prod Postgres — the issue is we have no fallback
if Postgres later fails.

### 3. R2 telemetry-blob loss

Telemetry blobs (`r2_bucket` — separate from backup bucket)
hold raw session uploads referenced by `telemetry_artifacts`
rows. If the blob bucket is lost:
- Already-processed coaching insights, weakness episodes, and
  prescriptions are intact (they live in Postgres, not R2).
- The user-facing "view raw session" / "re-run analysis"
  features 404 until re-upload.

**Recovery.**
1. **Recreate the bucket** with the same name + lifecycle.
2. **Mark all `telemetry_artifacts` as `status='lost'`** (or
   add a column if needed) so the UI surfaces a "re-upload
   to view" prompt.
3. **Notify drivers** — they retain a local copy of the last
   N sessions on-device per ADR-010 (offline-first contract:
   uploads are queued and dispatched when network returns),
   so re-upload is straightforward in most cases.

### 4. Redis loss

Redis holds: rate-limit buckets, pairing tokens, idempotency
cache, suggest cache, Celery broker state. None is durable —
all are reconstructable.

**Recovery.**
1. **Provision a fresh Redis instance.** Update `REDIS_URL`.
   Rolling restart.
2. **Accept transient effects:**
   - Active pairing flows (~5 min window) need to be retried
     by the user.
   - Rate-limit counters reset to zero — a brief abuse window
     until traffic re-fills the buckets.
   - Celery in-flight tasks may double-execute; idempotency
     keys protect the ingest path (`Idempotency-Key` header,
     T098) and most webhooks (Stripe + Clerk webhooks are
     event-id deduped per provider).

No data is lost; this is a transient quality-of-service event.

### 5. Application / deploy failure

Bad code in production (5xx spike on the latest deploy).

**Recovery.**
1. **Roll back via the deploy platform's revision history.**
   The standard rollback button on the hosting provider
   (Fly.io / Railway / whatever ships at GA) returns to the
   previous working revision in <2 minutes.
2. **Verify `/readyz` and `/healthz` on each pod** before
   declaring resolved.
3. **Open a hotfix branch.** Fix forward only after the
   rollback has stuck for 15 minutes.

### 6. Single-region cloud outage

One AZ / region of our hosting provider becomes unavailable.

For M1 we are single-region. The provider's own multi-AZ
guarantees apply (typically 99.99% intra-region), but a full
region outage means we are down for the duration of the
provider incident.

**Mitigation (read-only).**
1. Spin up a read replica from the latest pg_backups dump in
   a different region (manual; no automation today).
2. Point a `read.lap-coach.racing` subdomain at the
   read-replica deployment. Drivers can view their existing
   data; uploads queue locally until prod recovers.

This is **manual and slow** (4-hour RTO) — accepted at M1.
M2 plans cross-region active-passive (see a future M2 cross-region ADR).

### 7. DNS / TLS / CDN

The platform DNS provider, the certificate authority, or the
CDN (Cloudflare) goes down.

**Recovery.**
1. Check provider status pages first — most outages are
   public + measurable.
2. If our DNS records are misconfigured (vs. provider
   outage), update via the registrar's web UI. TTL is
   typically 5 min.
3. If TLS cert renewal failed, fall back to the previous
   cert via the platform's cert manager. Re-run renewal
   after the immediate failure mode is identified.

Generally out of our control; communicate transparently and
wait.

## Recovery validation

After ANY restore, run this smoke test before declaring "all
clear":

1. **Health probes.** `GET /healthz` and `GET /readyz` return
   200 from every pod.
2. **Auth roundtrip.** A test JWT against `GET /api/v1/me`
   returns 200 with a valid user_id.
3. **Read path.** `GET /api/v1/coaching/rules` (cheap, no
   user-data dependency) returns 200 and a non-empty rules
   list.
4. **Write path.** `POST /api/v1/insights/{id}/flag` with a
   known-good insight_id returns 200 + idempotent on retry.
5. **Ingest path.** Submit a small synthetic session via
   `POST /api/v1/sessions/ingest` and confirm the artifact
   reaches R2 and the session row appears in Postgres
   within 60 seconds.
6. **Webhook path.** Stripe Dashboard → "Send test webhook"
   for `customer.subscription.created`. Confirm 200 +
   `stripe_webhook_received` log event.
7. **Sentry**: zero new error spikes in the past 5 minutes.

If any step fails, treat as an extended incident — do not
declare resolved.

## Communication plan

| Audience | Channel | When |
|----------|---------|------|
| **Engineering on-call** | Pager (PagerDuty / equivalent) | Within 5 minutes of detection |
| **Engineering team** | Slack #incidents | Within 15 minutes |
| **End users (in-app)** | Banner on `app.lap-coach.racing` | Within 30 minutes if incident is user-visible |
| **End users (email)** | Resend campaign (when wired — see secret-rotation.md for status) | Within 4 hours if data-loss window > 0 |
| **Status page** | status.lap-coach.racing (TODO: stand up) | Continuous updates every 30 minutes during incident |
| **Stakeholders / investors** | Email digest from CTO | Post-resolution + postmortem link |

## Pre-disaster checklist (review monthly)

- [ ] T185 drill script executed within last 7 days; latest
      run green
- [ ] R2 backup bucket has at least 30 days of dumps
- [ ] Redis URL is documented in `docs/secret-rotation.md`
      and rotatable
- [ ] At least 2 engineers know how to roll back a deploy
- [ ] Sentry alerts are routed to the on-call pager
- [ ] On-call rotation is current and documented

## What this doc does NOT cover

- Region failover automation (manual at M1)
- Multi-cloud failover (single-cloud at M1; a future M2 cross-region ADR plans M2)
- Data-protection regulatory response (GDPR/CCPA breach
  notification — see legal runbook when authored)
- Customer-facing SLA guarantees (we publish a 99.5% target
  per `error-budget.md`; legal SLA is contract-by-contract
  for B2B customers)
- Forensic investigation procedure (out of engineering scope)
