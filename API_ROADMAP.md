# Nutriblend API — Sign Up / Sign In Design & Product Roadmap

Created 2026-09-24. Companion to `PROJECT_SETUP.md` (which records what *is* built).
This file records what to build **next**, the options at each step, and the decisions
that are still open.

> **Status: Part A is built** (2026-09-24). Decisions taken: flow shape **B**, fields **B**
> (name + mobile + optional unique email), enumeration **C**, rate limits **A** (Mongo),
> responses **C** (raw + coded errors), plus `/api/v1` versioning. Test suite (Decision 6)
> was *not* adopted — smoke tests remain throwaway, so there is still no regression safety
> on auth logic. Verified against the live Atlas database on 2026-09-24 (20 checks).
> Part B below is unchanged and is the plan from here.

---

## 0. The honest starting point

You asked for two working APIs: **Sign Up** and **Sign In**. Right now the server has
the *ingredients* for both, but neither endpoint exists as such. Here is the gap:

| What the mobile app screen needs | What exists today | Gap |
|---|---|---|
| "Sign Up" — name + mobile → OTP screen | `POST /api/users` then `POST /api/auth/request-otp` | Two calls, and the profile is created **before** the number is proven |
| "Sign In" — mobile → OTP screen | `POST /api/auth/request-otp` | Same endpoint as sign-up, so the app can't tell the two cases apart cleanly |
| "Enter OTP" → logged in | `POST /api/auth/verify-otp` | ✅ Works, returns access + refresh tokens |

Three specific problems with using what's there as-is:

1. **Number squatting.** `POST /api/users` writes a row for any 10-digit number with no
   proof the person owns it. I can register *your* number, and you can then never sign up.
2. **Junk rows.** Every abandoned sign-up leaves a permanent unverified user.
3. **Ambiguous errors.** The app can't distinguish "you already have an account, sign in"
   from "no account yet, sign up" without extra round-trips.

None of this is hard to fix — but the fix requires a decision about flow shape, which is
Step 1 below.

---

# PART A — The two APIs you want now

---

## Step 1. Choose the flow shape  ⟵ **DECISION 1**

### Option A — Keep the current split
`POST /api/users` → `POST /api/auth/request-otp` → `POST /api/auth/verify-otp`

- **Effort:** zero, it already works.
- **App-side cost:** 3 calls to sign up. The app must orchestrate, and handle the case
  where user-creation succeeds but OTP sending fails.
- **Carries all three problems above.**
- **Pick this if:** you want to ship something today and fix it later.

### Option B — Dedicated `signup` + `signin`, sharing one `verify-otp`  ⭐ *recommended*

```
POST /api/auth/signup   { name, mobile_number, email? }  → OTP sent
POST /api/auth/signin   { mobile_number }                → OTP sent
POST /api/auth/verify-otp { mobile_number, otp }         → access + refresh tokens
```

- **Effort:** ~half a session. Mostly reshuffling logic that already exists.
- **App-side cost:** 2 calls per flow. Maps 1:1 onto your two screens.
- **Fixes squatting:** the user row is created in a `pending` state and only becomes
  `active` on OTP verification. A pending row can be overwritten by a later sign-up
  attempt, so no number is ever permanently locked by an abandoned attempt.
- **Clear errors:** `signup` on an already-active number → `409` "account exists, sign in";
  `signin` on an unknown number → `404` "no account, sign up first".
- **`POST /api/users` stays** for admin/seeding, or is removed — your call (Decision 1b).

### Option C — OTP-first, profile afterwards (Swiggy / Zomato style)

```
POST /api/auth/otp        { mobile_number }        → OTP sent, works for everyone
POST /api/auth/verify-otp { mobile_number, otp }   → tokens + { is_new_user: true }
PATCH /api/users/me       { name, email? }         → completes the profile (authenticated)
```

- **Effort:** similar to B, but the app needs a third screen and more state.
- **Biggest UX win:** the user never hits a wall. One entry field, one button, always works.
- **Best privacy:** no endpoint reveals whether a number is registered (see Step 2).
- **Cost:** you have logged-in users with no name until they finish the profile screen.
  Every downstream feature must tolerate that, or you gate the app behind a
  "complete your profile" screen anyway.
- **Pick this if:** you expect sign-up friction to matter commercially, or you'll later
  add "continue with Google/Apple" and want one unified entry point.

**My recommendation: Option B now.** It's the clearest mapping to what you asked for,
it's the least app-side complexity, and Option C is a straightforward migration later
if you decide friction matters — `signup` and `signin` can both become thin wrappers
around a unified `otp` endpoint without breaking existing clients.

---

## Step 2. Decide how much to reveal about registered numbers  ⟵ **DECISION 2**

This is a real security/UX trade-off, not a formality. If `signin` returns `404` for an
unknown number, anyone can discover which mobile numbers have Nutriblend accounts by
trying them one at a time. This is called **user enumeration**.

### Option A — Explicit errors (`404` / `409`)
- Best UX by a wide margin. The app can say exactly what's wrong and offer the right button.
- Leaks account existence to anyone willing to make requests.

### Option B — Always answer "OTP sent"
- Leaks nothing.
- Bad UX (user waits for an SMS that never comes), and **you pay for wasted SMS** on every
  probe. Perversely, it makes the abuse *more* expensive for you, not less.

### Option C — Explicit errors + strict rate limiting  ⭐ *recommended*
- Keep the good UX, but make enumeration slow and expensive via Step 6's limits.
- For a consumer nutrition app, knowing someone has an account is low-sensitivity —
  this is the trade-off almost every Indian consumer app makes.

**Note:** whichever you pick, never let the *error message* differ in timing or wording in
a way that leaks more than the status code already does.

---

## Step 3. Decide the sign-up field set  ⟵ **DECISION 3**

### Option A — Minimal: `name`, `mobile_number`  ⭐ *recommended to start*
Fastest sign-up. Everything else collected later, in-context, when it's actually needed.

### Option B — Standard: `+ email`
Email is genuinely useful (order receipts, account recovery if a number changes, marketing).
Keep it **optional** at sign-up. Decision: should email be **unique**? Recommend yes-if-present
(a sparse unique index), so two accounts can't claim the same inbox.

### Option C — Nutrition profile at sign-up: `+ date_of_birth, gender, height_cm, weight_kg, goal`
Only if the app is useless without them. Every extra required field measurably drops
completion. For a nutrition product these are valuable — but they belong on a
**post-sign-up onboarding screen** backed by `PATCH /api/users/me`, not the sign-up call.

**My recommendation: A or B now, C as a separate onboarding step in Phase 2.**

---

## Step 4. Data model changes

Whichever flow you pick, the `users` document needs a few additions:

```python
{
  "mobile_number": "9876543210",     # unique index (exists)
  "name": "Boss",
  "email": None,                      # sparse unique index (new, if Decision 3 = B)
  "status": "pending",                # NEW: pending | active | blocked
  "is_verified": False,               # kept for compatibility; mirrors status == "active"
  "created_at": ...,
  "updated_at": ...,                  # NEW
  "verified_at": None,                # NEW — when they first proved the number
  "last_login_at": None,              # NEW — useful for retention metrics later
}
```

Why `status` rather than just `is_verified`:
- `pending` — signed up, never verified. **Overwritable** by a new sign-up attempt.
- `active` — verified, can sign in.
- `blocked` — you need this eventually (abuse, refunds, deletion requests). Cheaper to
  add the field now than to migrate later.

**Options for the pending-row lifecycle:**
- **A:** Leave pending rows forever — simple, but they accumulate.
- **B:** TTL index that deletes pending rows after 24h  ⭐ *recommended* — self-cleaning,
  and a re-attempt just creates a fresh one.
- **C:** Delete on next sign-up attempt only — no background cleanup, rows linger.

---

## Step 5. Edge cases and failure modes — the "all aspects" list

These are the things that turn a demo into something you can actually ship. Each one needs
a deliberate answer; my proposed answer is on the right.

| # | Situation | Proposed handling |
|---|---|---|
| 1 | Sign up with a number that's already **active** | `409` — "Account exists. Please sign in." |
| 2 | Sign up with a number that's **pending** | Allow. Overwrite the name, send a fresh OTP. Never lock a number because of an abandoned attempt. |
| 3 | Sign up with a number that's **blocked** | `403`, generic message. Don't explain why. |
| 4 | Sign in with an **unknown** number | `404` — "No account found. Please sign up." (per Decision 2) |
| 5 | Sign in with a **pending** number | Treat as sign-in, send OTP; on verify, activate. Avoids a dead end for someone who abandoned sign-up and later taps "Sign In". |
| 6 | Two sign-ups for the same number **at the same time** | The unique index makes one lose with `DuplicateKeyError` — catch it and retry the read path rather than 500. |
| 7 | **Resend OTP** | Dedicated `POST /api/auth/resend-otp`, or just re-call signup/signin. Needs its own cooldown (Step 6) — this is the #1 abused endpoint in any OTP system. |
| 8 | OTP requested, then user signs up again before verifying | New OTP replaces the old. The old one must stop working — the current upsert already does this correctly. |
| 9 | OTP correct but **expired** | `400` — "OTP expired, request a new one." Already handled. |
| 10 | Too many wrong OTP guesses | `429` + invalidate the OTP entirely, forcing a resend. Currently locks at 5 attempts but leaves the OTP alive until expiry — worth tightening. |
| 11 | Mobile number formatting: `+91 98765 43210`, `09876543210` | **Normalize on input** — strip spaces, dashes, `+91`, leading `0` — before validating and storing. Currently only a bare 10-digit string is accepted, which will cause real support tickets. |
| 12 | Email already used by another account | `409` if Decision 3 = B with unique emails. |
| 13 | Name validation | Trim; require 1–60 chars; reject strings that are only digits or symbols. Don't over-restrict — people have apostrophes, hyphens and single-word names. |
| 14 | SMS gateway down during sign-up | Currently: `502`, pending OTP deleted. For **sign-up** specifically, also decide whether the `pending` user row survives. Recommend yes — they can retry without losing the name. |
| 15 | User verifies OTP, then the token response fails to reach the app | They're verified server-side but have no token. Signing in again must work cleanly — it does, via the sign-in path. |
| 16 | Clock skew between server and Atlas | Already handled: all timestamps are UTC-aware, TTL indexes are server-side. |
| 17 | Account deletion (DPDP Act / Play Store requirement) | Not urgent for v1, but **Google Play now requires an in-app account-deletion path** for apps with accounts. Note it for Phase 9. |
| 18 | Same user on multiple devices | Already works — each login starts its own refresh-token family. Consider storing a device label so the user can see "logged-in devices". |

---

## Step 6. Rate limiting and abuse control  ⟵ **DECISION 4**

This is the one genuinely missing security control. Without it, `signup`/`signin`/`resend`
can be hit in a loop — which, once SMS is live, is a **direct bill** and someone else's
phone buzzing all night.

### Where to store the counters

- **Option A — MongoDB collection with a TTL index**  ⭐ *recommended*
  No new infrastructure, works across multiple server processes, survives restarts.
  Costs one extra round-trip per request — irrelevant at your scale.
- **Option B — In-process memory (`slowapi`, or a dict)**
  Fastest and simplest, but **silently breaks** the moment you run more than one worker
  or deploy a second instance — each process has its own counters. Fine for local dev only.
- **Option C — Redis**
  The right answer at scale, and what you'd move to eventually. Adds a service to run,
  pay for, and monitor. Premature today.

### Proposed limits (all configurable via `.env`)

| Scope | Limit | Rationale |
|---|---|---|
| Per mobile number | 1 OTP per 60 seconds | Stops resend-hammering |
| Per mobile number | 5 OTPs per hour | Normal users need 1–2 |
| Per mobile number | 10 OTPs per day | Hard ceiling on cost per number |
| Per IP address | 20 OTPs per hour | Stops one machine enumerating many numbers |
| Per IP address | 100 requests per hour on `verify-otp` | Stops brute-forcing OTPs across accounts |

Return `429` with a `Retry-After` header so the app can show "Resend in 47s" — which is
also the single best thing you can do to *reduce* legitimate retry traffic.

**Caveat on per-IP limits:** behind a proxy or load balancer you must read the real client
IP from `X-Forwarded-For`, and only trust that header from your own proxy. Getting this
wrong either limits everyone as one IP, or lets anyone spoof their way around the limit.
I'll wire it correctly when we deploy.

---

## Step 7. Response format  ⟵ **DECISION 5**

Worth settling **now**, because changing it later means touching every endpoint and
every screen in the app.

- **Option A — Raw bodies + FastAPI's default errors** (what we have)
  `{"detail": "Incorrect OTP"}`. Idiomatic, auto-documented in Swagger, least code.
- **Option B — Envelope everything**
  `{"success": false, "data": null, "error": {"code": "OTP_INCORRECT", "message": "..."}}`.
  Many mobile devs prefer one parsing path for every response.
- **Option C — Raw success bodies + a standardised error body**  ⭐ *recommended*
  Keep clean success responses; make every error look like
  `{"error": {"code": "OTP_INCORRECT", "message": "Incorrect OTP"}}`.
  The **machine-readable `code` is the important part** — it lets the app branch on
  `ACCOUNT_EXISTS` vs `ACCOUNT_NOT_FOUND` without string-matching English text, and lets
  you reword messages (or localise to Hindi/Marathi) without shipping an app update.

---

## Step 8. Testing

Current state: the smoke test runs against an in-memory mock Mongo and is **deleted after
each run** — nothing is committed to the repo. That was right while the shape was churning.
It isn't right any more.

- **Option A — Keep throwaway smoke tests.** Zero maintenance, zero regression safety.
- **Option B — Commit a `tests/` suite (pytest + mongomock)**  ⭐ *recommended*
  Run in a second with no database. Catches regressions the moment auth logic changes —
  and auth logic is exactly the code where a silent regression is most expensive.
- **Option C — B, plus integration tests against a real test database**
  Catches what mongomock can't (real unique-index enforcement, TTL behaviour, actual
  Atlas errors). Recommend a small number of these, pointed at a **separate** Atlas
  database — never the one holding real users.

~~One thing to verify regardless: the full flow has still never been run against real Atlas.~~
**Done 2026-09-24** — `scripts/verify_atlas.py` passes 20 checks against the live database,
including the real unique-index and TTL guarantees. Re-run it after any change to the data
model or indexes; it cleans up after itself.

---

## Step 9. Implementation order for Part A

Assuming Options B / C / A-or-B / A / C from the decisions above:

1. Normalise + validate mobile numbers centrally (Step 5, #11) — everything depends on it.
2. Extend the user model with `status`, `updated_at`, `verified_at`, `last_login_at`.
3. Add the TTL index for pending users.
4. Build `POST /api/auth/signup`.
5. Build `POST /api/auth/signin`.
6. Update `verify-otp` to activate pending users and stamp `verified_at` / `last_login_at`.
7. Add `POST /api/auth/resend-otp` with its own cooldown.
8. Add the rate-limit layer and apply it to all three OTP-sending endpoints.
9. Standardise error bodies with machine-readable codes.
10. Decide the fate of `POST /api/users` (keep as admin-only, or remove).
11. Commit a `tests/` suite covering every row in the Step 5 table.
12. Run the whole thing against real Atlas.
13. Update `PROJECT_SETUP.md`.

---

# PART B — Product roadmap after auth

Sketched so the auth decisions above are made with the destination in view. Each phase
lists the main fork in the road rather than a full design.

### Phase 1 — Auth complete *(Part A above)* — ✅ **done**, except a committed test suite.

### Phase 2 — User profile & onboarding
`GET/PATCH /api/users/me`, profile photo, the nutrition fields deferred from Step 3,
addresses if you'll ship physical product.
*Fork:* store images as **S3/Cloudinary URLs** (recommended — cheap, CDN-backed) vs
**GridFS in Mongo** (no extra service, but your database now serves image bytes).

### Phase 3 — Catalog
Products, categories, variants, pricing, availability, search.
*Fork:* **Mongo text index** (free, adequate to a few thousand products) vs
**Atlas Search** (typo tolerance, faceting, relevance — worth it once browsing is the
main way users find things).

### Phase 4 — Cart & orders
Cart, checkout, order lifecycle, history.
*Fork:* **server-side cart** (recommended — survives reinstalls, works across devices,
and lets you see abandoned carts) vs **client-side cart** (simpler, but you lose all of that).
*Critical:* price and stock must be re-validated **server-side at checkout**, never trusted
from the client.

### Phase 5 — Payments
*Fork:* **Razorpay** (best fit for India — UPI, cards, wallets, netbanking in one) vs
**Stripe** (better API and docs, weaker Indian payment-method coverage) vs
**Cash on delivery only** (zero integration, real operational cost).
*Non-negotiable:* verify the payment **webhook signature** server-side. Never mark an order
paid based on what the app tells you.

### Phase 6 — Nutrition & personalisation
The part that makes this Nutriblend rather than a generic store — blend recommendations,
goal tracking, intake logs. Worth designing properly rather than bolting on.

### Phase 7 — Notifications
Push (FCM), transactional SMS/email, order updates.

### Phase 8 — Admin
Product management, order management, user support tooling.
*Fork:* **admin endpoints in this API with a role field** (recommended — one codebase) vs
**a separate admin service** (cleaner blast radius, more to run).

### Phase 9 — Production hardening
CORS lock-down, structured logging, error tracking (Sentry), health/readiness probes,
backups, account deletion (Play Store requirement), API versioning (`/api/v1/...` —
**cheapest to adopt before you have live clients, painful after**), deployment and CI.

---

# PART C — Open decisions

| # | Decision | Options | My recommendation |
|---|---|---|---|
| 1 | Sign up / sign in flow shape | A: keep current · B: dedicated endpoints · C: OTP-first | **B** |
| 1b | Keep `POST /api/users`? | Keep as admin-only · Remove | **Keep, admin-only** |
| 2 | Reveal whether a number is registered? | A: explicit · B: opaque · C: explicit + rate limits | **C** |
| 3 | Sign-up fields | A: name+mobile · B: +email · C: +nutrition profile | **A or B** |
| 4 | Rate-limit storage | A: Mongo · B: in-memory · C: Redis | **A** |
| 5 | Response format | A: raw · B: envelope · C: raw + coded errors | **C** |
| 6 | Commit a test suite? | A: throwaway · B: pytest+mongomock · C: + integration | **B now, C at Phase 9** — *not adopted yet; still open* |

