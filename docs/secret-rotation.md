# Secret Rotation Runbook (T183)

Operational guide for rotating the four external-service secrets
that lap-backend depends on. Run any rotation only when at least
one engineer can monitor `/healthz` and the Sentry feed during the
window. None of these rotations require downtime if executed in
the order below.

## When to rotate

- **Routine.** Every 90 days for API keys, every 180 days for
  webhook signing secrets. Add a calendar entry.
- **On suspicion.** Any time a secret may have leaked: a stray
  paste in chat, a leaked screenshot, a former engineer's laptop
  not yet wiped, a third-party SaaS breach disclosure.
- **On personnel change.** When an engineer with deploy access
  leaves, rotate every secret they could have read in the last
  90 days.
- **On dependency upgrade.** If a Stripe / Clerk SDK upgrade
  reports a key-format migration, rotate the affected secret as
  part of the upgrade PR rather than after.

## General principles

1. **No `--no-verify`.** Commit the env-var update through normal
   PR review even when rotating is urgent. The 90-second saved by
   skipping CI doesn't justify the post-incident review.
2. **Overlap, never swap.** For every rotation, the new secret
   should be live alongside the old for at least one full request
   cycle (~5 minutes for synchronous endpoints; up to 1 hour for
   webhook signature verification on retried events). Never delete
   the old secret in the same deploy as introducing the new one.
3. **Verify with a synthetic.** Each section below names a
   synthetic call you can make against staging to confirm the new
   secret took effect before you cut prod traffic over.
4. **Audit log on the way out.** After retiring an old secret,
   write a one-line note to `docs/secret-rotation-log.md` (create
   if missing) recording the date, secret name, and reason. This
   becomes evidence in any future security review.

## Stripe — `STRIPE_SECRET` (API key)

**Format.** `sk_live_...` (production) or `sk_test_...` (test mode).
**Where it's read.** `app/config.py:Config.stripe_secret`. Every
endpoint that calls `stripe.checkout.Session.create`,
`stripe.billing_portal.Session.create`, `stripe.PromotionCode.list`,
`stripe.Subscription.list`, or `stripe.Refund.create`.

**Steps.**
1. **Stripe Dashboard → Developers → API keys → Roll key.**
   Stripe creates a new `sk_live_*` and continues to accept the
   previous key for 12 hours. *Copy the new key now — it's only
   shown once.*
2. **Update the staging deploy first** (env var
   `STRIPE_SECRET=sk_test_...` if you keep staging on test mode,
   or your staging-specific live key). Wait for the rolling
   restart to settle (~2 minutes).
3. **Synthetic check on staging:** `curl -s
   https://staging.lap-coach.racing/healthz` then exercise one
   billing endpoint that hits Stripe (e.g.
   `POST /api/v1/billing/checkout` with a test JWT). Confirm
   200 + non-empty `session_id`.
4. **Update prod deploy.** Same env-var change, rolling restart.
5. **Watch Sentry for 5 minutes.** Any
   `stripe.error.AuthenticationError` indicates a stale key still
   in use somewhere — investigate before continuing.
6. **Stripe Dashboard → reveal old key → Delete.** Do this only
   after the 5-minute soak; Stripe's deletion is immediate. If
   you delete during the soak and a stale pod still has it, that
   pod's customers see 5xx until restart.

**Rollback.** If the new key is misconfigured, redeploy with the
old key (Stripe still accepts it for 12 hours after roll). After
12 hours the old key is fully revoked — you must re-roll.

## Stripe — `STRIPE_WEBHOOK_SECRET` (signing secret)

**Format.** `whsec_...`.
**Where it's read.** `app/routers/billing.py:receive_stripe_webhook`
via `stripe.Webhook.construct_event(secret=...)`.

Stripe webhook signing is per-endpoint, not per-account. If you
have one webhook endpoint configured (`/api/v1/billing/webhooks`),
you have one `whsec_*` to rotate.

**Steps.**
1. **Stripe Dashboard → Developers → Webhooks → select endpoint
   → "Roll signing secret".** Stripe accepts both old and new
   secrets simultaneously for 24 hours.
2. **Update prod env var** `STRIPE_WEBHOOK_SECRET=whsec_...`.
   Rolling restart.
3. **Synthetic check:** Stripe Dashboard → Webhooks → "Send test
   webhook" with a `customer.subscription.created` event. Verify
   the request lands as 200 in our logs and the new event_id
   appears in `stripe_webhook_received`.
4. **Watch for 1 hour.** Stripe retries failed webhooks for up
   to 3 days; the 1-hour soak catches the retried-with-old-secret
   scenario early.
5. **Stripe Dashboard → "Expire signing secret".** Removes the
   old `whsec_*`.

**Rollback.** Redeploy with the previous `whsec_*` value (still
valid in Stripe's 24-hour grace). After 24 hours the old secret
is revoked — re-roll.

## Clerk — `CLERK_SECRET` (API key / Backend SDK)

**Format.** `sk_live_...` (production) or `sk_test_...` (test).
**Where it's read.** `app/config.py:Config.clerk_secret`. Used by
the JWKS client (`infra/clerk.py`) and any backend SDK calls.

**Steps.**
1. **Clerk Dashboard → API Keys → Backend API keys → "Create new
   secret key".** Clerk supports having multiple active backend
   keys simultaneously — there is no automatic overlap window,
   but you can keep both keys live for as long as you need.
2. **Update prod env var** `CLERK_SECRET=sk_live_...`. Rolling
   restart.
3. **Synthetic check:** issue a test JWT (anything that hits
   `Depends(get_current_user)`), e.g.
   `GET /api/v1/me` with a valid bearer. Confirm 200 + user_id
   in the response.
4. **Watch Sentry for 10 minutes.** Any spike in 401s on
   authenticated endpoints (or in `jwt.*` exceptions raised from
   `app/deps.py:get_current_claims`) indicates the new key isn't
   reaching the JWKS endpoint correctly.
5. **Clerk Dashboard → revoke the old backend key.** Immediate;
   no grace period after revocation.

**Rollback.** Redeploy with the previous backend key. Clerk keeps
both active until you revoke explicitly, so the old key is still
usable up until step 5.

## Clerk — `CLERK_WEBHOOK_SECRET` (svix signing secret)

**Format.** Base64-ish opaque string from Clerk's webhook config.
**Where it's read.**
`infra/clerk_webhooks.py:verify_svix_signature` via the secret
passed in by `app/routers/webhooks.py:verify_clerk_webhook_signature`.

Clerk webhooks use svix under the hood. svix supports key
rotation natively with overlap.

**Steps.**
1. **Clerk Dashboard → Webhooks → select endpoint → "Rotate
   signing secret".** svix issues a new secret AND continues
   honoring the old one for the next 24 hours.
2. **Update prod env var** `CLERK_WEBHOOK_SECRET=...`. Rolling
   restart.
3. **Synthetic check:** Clerk Dashboard → Webhooks → "Send test
   event" with a `user.created` event. Verify the request lands
   as 200 in our logs.
4. **Watch for 1 hour.** Same retry-window concern as Stripe.
5. **Clerk Dashboard → revoke the old signing secret.**

**Rollback.** Redeploy with the previous secret (still valid for
24 hours).

## Resend — `RESEND_API_KEY` (planned, not yet wired)

> **Status.** As of 2026-04-27 the email-delivery path is not yet
> wired in lap-backend (search of the repo for `resend` returns
> no hits). When email send is added (likely under the org-invite
> flow or the payment-failed notification queue at
> `infra/notifications.py`), this section becomes operational.

**Format.** `re_...`.
**Where it will be read.** Expected in `app/config.py` as
`resend_api_key: SecretStr`.

**Steps (anticipated, will mirror Stripe API-key rotation).**
1. **Resend Dashboard → API Keys → "Create API Key".** Resend
   supports multiple simultaneous keys.
2. **Update prod env var** `RESEND_API_KEY=re_...`. Rolling
   restart.
3. **Synthetic check:** trigger an email send (e.g. an org-invite
   to a test address). Confirm the email lands in the test
   inbox + the request_id appears in our log under
   `email_sent` (or whatever event the email-send wrapper
   emits).
4. **Watch Sentry for 10 minutes** for any 401/403 responses
   from `api.resend.com`.
5. **Resend Dashboard → revoke the old key.**

**Rollback.** Redeploy with the previous key (still valid until
revocation).

## R2 — `R2_ACCESS_KEY_ID` + `R2_SECRET_ACCESS_KEY`

**Format.** S3-compatible access-key + secret-key pair.
**Where they're read.** `app/config.py:Config.r2_access_key_id`,
`r2_secret_access_key`. Wired through `app/deps.py:get_blob_store`
into `infra/storage.py:R2BlobStore` for telemetry blob upload /
download.

R2 access keys are paired — you rotate them together as one unit.
Cloudflare's R2 dashboard supports multiple simultaneous access
keys per bucket, so overlap is straightforward.

**Steps.**
1. **Cloudflare Dashboard → R2 → Manage R2 API tokens → "Create
   API token".** Scope it: read + write to the lap-backend
   bucket only (no admin / list-buckets). Copy `Access Key ID`
   and `Secret Access Key` immediately — the secret is only
   shown once.
2. **Update prod env vars** `R2_ACCESS_KEY_ID=...` and
   `R2_SECRET_ACCESS_KEY=...`. Rolling restart.
3. **Synthetic check:** trigger a session ingest with a small
   test payload (e.g.
   `POST /api/v1/sessions/ingest` with a 1KB stub). Verify the
   resulting blob appears in the R2 bucket with the new
   credentials and the artifact id is reachable via
   `GET /api/v1/sessions/.../next_actions`.
4. **Watch Sentry for 10 minutes** for any
   `botocore.exceptions.ClientError` with `InvalidAccessKeyId`
   or `SignatureDoesNotMatch`.
5. **Cloudflare Dashboard → revoke the old API token.** Immediate;
   no grace period.

**Rollback.** Redeploy with the previous access-key pair (still
valid until you revoke at step 5).

## Internal — `COACHING_BUNDLE_SIGNING_SECRET`

Not an external-service secret, but worth noting. This is the
HMAC key used by `GET /api/v1/coaching/rules/bundle` to sign the
compiled rule bundle clients verify before applying. Rotating
requires updating BOTH the server config AND every shipped
client's verification key. Out of scope for this runbook — see
the (future) `client-coordination.md` for the deploy ordering
when that day comes.

## Verification checklist (use after every rotation)

- [ ] `/healthz` returns 200 from prod
- [ ] No new `*_authentication_*` or `*_signature_*` events in
      Sentry over the soak window
- [ ] One synthetic call against the rotated secret returns
      expected 2xx
- [ ] Old secret revoked at the provider
- [ ] Audit-log entry appended to `docs/secret-rotation-log.md`

## What this runbook does NOT cover

- DB password rotation (managed by the Postgres provider; see
  `docs/db-rotation.md` when that exists)
- Internal service-to-service tokens (none in v1; if added later,
  document in their own runbook)
- TLS / cert renewal (handled by the platform via Let's Encrypt
  or the WAF cert manager)
- Sentry DSN, PostHog API key (low-risk identifiers, not auth
  secrets — rotate annually as hygiene only)
- Customer-facing API keys (we don't issue any in v1)
