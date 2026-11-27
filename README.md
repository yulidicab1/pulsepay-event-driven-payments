# PulsePay

PulsePay is a portfolio project that models an event-driven payment processing workflow, combining synchronous API operations with asynchronous background processing, explicit state transitions, and operational visibility.

The project is intentionally portfolio-focused, the codebase is small enough to review quickly, but structured like a real system with clear boundaries between API, persistence, queueing, worker orchestration, and frontend operations tooling.

## Highlights

- Payments are created synchronously and processed asynchronously
- Status transitions are explicit and observable: `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`
- Queueing is hidden behind an internal abstraction designed for future AWS SQS migration
- Retries, deterministic failure rules, and worker behavior are covered by tests
- The frontend explains system state in a way that is understandable to technical and non-technical reviewers

## Stack

- Backend: FastAPI + SQLAlchemy
- Database: PostgreSQL
- Async processing: internal queue abstraction with a PostgreSQL-backed implementation
- Frontend: React + TypeScript + Vite
- Testing: pytest
- Local orchestration: Docker Compose

## What it includes

- FastAPI backend with modular service, repository, and queue layers
- PostgreSQL-backed persistence for payments and internal queue messages
- Background worker for asynchronous payment processing
- Deterministic simulation rules plus optional random transient failures
- React + TypeScript dashboard with live metrics, payment creation, filtering, and detail inspection
- Docker Compose setup for running the full stack locally
- Pytest coverage for API validation, payment creation, queue processing, retry behavior, and processing rules

## Demo flow

1. Create a payment from the dashboard or `POST /api/v1/payments`
2. The API validates the payload, stores the payment, and emits a `payment.created` queue event
3. The worker claims available events and moves the payment into `PROCESSING`
4. The processor decides whether the payment should complete, retry, or fail
5. The dashboard updates with summary metrics, table status, failure information, and queue history

Useful demo inputs:

- Use `retry` in `merchant_reference` to simulate a transient failure on the first attempt
- Use `fail` in `merchant_reference` to force deterministic failure
- Use an amount `>= 5000.00` to force deterministic failure

## Architecture

### High-level components

- `frontend/`: friendly operations dashboard built with React + TypeScript
- `backend/app/api`: FastAPI routes and dependency wiring
- `backend/app/services`: application logic for payment creation, processing, and worker orchestration
- `backend/app/queue`: internal queue contracts plus PostgreSQL-backed implementation
- `backend/app/models`: SQLAlchemy models for payments and queue messages
- `backend/app/schemas`: request and response models for API boundaries
- `backend/app/worker.py`: standalone worker entrypoint used by Docker Compose

### System flow

```text
React Dashboard
      |
      v
FastAPI API  --->  PostgreSQL (payments)
      |
      +----> Internal Queue Abstraction ---> PostgreSQL (payment_events)
                                                |
                                                v
                                           Worker Service
                                                |
                                                v
                                       Payment Processor Rules
                                                |
                                                v
                                     Updated payment state + logs
```

### State transitions

- `PENDING`: payment has been created and is waiting to be processed
- `PROCESSING`: worker has claimed the queue event and is actively handling it
- `COMPLETED`: processing succeeded
- `FAILED`: processing reached a terminal failure state

For retryable failures, the payment returns to `PENDING` and the queue message is re-scheduled for a future attempt.

## Design decisions

### Queue abstraction first, infrastructure second

The queue is represented as a publisher/consumer contract rather than being hard-coded into the payment service. Right now the implementation stores events in PostgreSQL, which keeps the MVP self-contained. The domain layer does not need to know whether the queue is Postgres-backed or SQS-backed, which makes later migration much less disruptive.

### Worker logic separated from processor logic

`PaymentWorkerService` handles queue claiming, retries, commits, and payment state updates. `PaymentProcessor` only decides what should happen to a payment. That split keeps orchestration concerns separate from business rules and makes processing logic easy to unit test.

### Operational visibility is part of the product

The frontend is not just a thin list view. It exposes payment metrics, filterable payment states, queue event history, retry counts, and failure explanations because payment systems are operational products as much as transactional ones.

### Deterministic behavior for demos and testing

The processor includes deterministic rules so the system can be demonstrated reliably in an interview or portfolio setting. Optional random transient failure support exists to show how the design handles real-world uncertainty, but the deterministic rules keep behavior reproducible.

## API surface

### Core endpoints

- `GET /api/v1/health`
- `POST /api/v1/payments`
- `GET /api/v1/payments`
- `GET /api/v1/payments/{payment_id}`

### Operations endpoints

- `GET /api/v1/payments/metrics`
- `GET /api/v1/payments/{payment_id}/processing`
- `GET /api/v1/payments/{payment_id}/events`

## Repository layout

```text
.
├── backend
│   ├── app
│   │   ├── api
│   │   ├── core
│   │   ├── db
│   │   ├── models
│   │   ├── queue
│   │   ├── schemas
│   │   └── services
│   ├── tests
│   ├── Dockerfile
│   └── requirements.txt
├── frontend
│   ├── src
│   ├── Dockerfile
│   ├── package.json
│   └── tsconfig.json
├── docker-compose.yml
└── pytest.ini
```

## Quick start

### Prerequisites

- Docker Desktop
- Python 3.11+
- Node.js 20+

### Run the full system

Create credential files outside the repository and set the paths listed in
`.env.example`. The database URL file contains the connection string for the
backend, using the PostgreSQL service hostname. The password file is consumed
by the official PostgreSQL image. Example values in documentation are
placeholders and must be replaced in those private files.

For a new database, run the explicit setup steps:

```bash
docker compose up -d postgres
docker compose run --rm --no-deps backend alembic upgrade head
docker compose run --rm --no-deps backend python -m app.manage_users create --new-merchant "My merchant" --email owner@example.com
docker compose up -d backend worker frontend
```

The user command prompts privately for a password of 15 to 128 characters.
Then open the dashboard at http://localhost:5173 and sign in.

For an existing database, first approve and verify a backup and recovery plan.
The ownership migration preserves historical rows with no merchant assigned.
Those rows are excluded from merchant APIs and worker processing until an
explicit ownership decision. Migration and seeding are operator actions.
Starting the application does not perform either action.

The base Compose file uses built images and external secret files. Rebuild
after code changes. The existing PostgreSQL volume is retained. Changing a
password file does not rotate the password already stored by PostgreSQL.

### Run backend locally without Docker

Use Python 3.11 or newer and configure an external connection URL or a
`DATABASE_URL_FILE` pointing to one. For direct host execution, use the
appropriate local PostgreSQL hostname in that private URL file.

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
python -m app.manage_users create --new-merchant "My merchant" --email owner@example.com
uvicorn app.main:app --reload
```

Use migrations only on a new database or after explicit approval for existing
data. Development bootstrap additionally requires `APP_ENV=development`,
`SEED_DEV_ENABLED=true`, `DEV_USER_EMAIL`, and an external
`DEV_USER_PASSWORD_FILE`. Production rejects this bootstrap even when enabled.

### Run worker locally without Docker

In a second terminal:

```bash
cd backend
source .venv/bin/activate
python -m app.worker
```

### Run frontend locally without Docker

```bash
cd frontend
npm install
npm run dev
```

### Run tests

From the repository root:

```bash
backend/.venv/bin/python -m pytest
```

The main backend suite uses SQLite. The concurrency tests require a separate,
disposable PostgreSQL database whose name ends in `_test`. Export
`TEST_POSTGRES_URL` with that database's connection URL using the
`postgresql+psycopg` driver, then run the same command. The test role must be
able to create a schema in the database. Each test creates and removes its own
schema.

With `TEST_POSTGRES_URL` unset, the PostgreSQL tests are skipped. An invalid or
unreachable configuration fails those tests without displaying the URL. Use a
dedicated test database rather than the database from the development Compose
stack.

The backup/restore test additionally requires `TEST_BACKUP_POSTGRES_URL` for a
different disposable database ending in `_test`, plus `pg_dump` and `pg_restore`
on PATH or in `TEST_POSTGRES_BIN`. It restores and removes only the random
schema created by the test; it never overwrites an existing schema.

## Configuration

The defaults are tuned for local Docker Compose usage.

### Backend environment variables

- `DATABASE_URL`
- `DATABASE_URL_FILE`
- `PUBLIC_ORIGIN`
- `API_V1_PREFIX`
- `APP_ENV`
- `PAYMENT_QUEUE_NAME`
- `PAYMENT_MAX_RETRIES`
- `PAYMENT_RETRY_DELAY_SECONDS`
- `PAYMENT_RANDOM_FAILURE_RATE`
- `WORKER_POLL_INTERVAL_SECONDS`
- `WORKER_BATCH_SIZE`

### Database migrations

Schema changes are managed with Alembic (`backend/migrations`). Run migrations
explicitly before starting the API, on a new database or after approval of a
backup and recovery plan for existing data. Application startup never applies
migrations. Common commands from `backend/`:

```bash
alembic upgrade head                          # apply all migrations
alembic revision --autogenerate -m "message"  # generate a migration from model changes
alembic downgrade -1                           # roll back the last migration
```

### Docker Compose behavior

Docker Compose starts:

- PostgreSQL
- FastAPI backend
- Worker process
- React frontend

The backend exposes a healthcheck, and the frontend and worker wait for the backend or database as appropriate so the local stack comes up in a more predictable order.

## Testing strategy

The backend test suite is focused on the parts of the system that matter most in a payment workflow:

- request validation and API responses
- successful payment creation and queue publication
- duplicate merchant reference handling
- processing completion, retry scheduling, and terminal failure behavior
- payment processor rule coverage
- processing inspection endpoints used by the frontend

The tests intentionally favor behavior over implementation details, so they act as both a regression suite and executable documentation.

## Screenshots


## Future improvements

If this project were taken beyond MVP, the next steps would be:

- Replace the PostgreSQL queue implementation with AWS SQS
- Introduce webhook delivery for downstream payment status notifications
- Add observability integrations such as OpenTelemetry traces and metrics
- Expand the dashboard with charts, search, pagination, and audit drill-down
- Add contract tests and end-to-end browser tests
- Support multiple event types beyond `payment.created`

## Suggested review entry points

If you are reviewing this repo quickly, the most representative files are:

- `backend/app/services/payment_service.py`
- `backend/app/services/worker_service.py`
- `backend/app/queue/base.py`
- `backend/app/services/payment_processor.py`
- `frontend/src/App.tsx`
- `frontend/src/components/PaymentDetailPanel.tsx`

## Publishing notes

Before publishing, make sure the repository does not include local-only artifacts such as virtual environments, build outputs, cached Python bytecode, database files, or environment files. The `.gitignore` files in the repo are set up to exclude those by default.


## Dashboard authentication

Dashboard users belong to one merchant. Passwords are hashed with Argon2id.
FastAPI manages server-side PostgreSQL sessions with hashed random identifiers.
The browser receives a host-only HttpOnly cookie. HTTPS cookies use the
`__Host-` prefix and Secure. Production configuration requires PostgreSQL and an HTTPS
`PUBLIC_ORIGIN`. The frontend sends relative `/api` requests through its
same-origin deployment; Vite provides the development proxy using the
server-side `API_PROXY_TARGET`.

A short pre-session and CSRF token are created before login. Successful login
replaces that session and its CSRF token. State-changing cookie-authenticated
requests require both an allowed Origin and the session's CSRF token.
Responses containing API data use Cache-Control no-store. Session expiry is
enforced server-side, with an eight-hour absolute limit and thirty-minute
request inactivity limit by default. Polling runs only while authenticated.

Dashboard users are provisioned explicitly using `app.manage_users`.
Its reset-password and disable commands revoke all sessions for that user.
Login attempt limits are stored in PostgreSQL and shared across API instances.
Session and password material must be excluded from logs.

Argon2id uses the library's recommended parameters (64 MiB, three passes and
four lanes with the declared dependencies), with a random salt per password.
Tests enforce an Argon2id work factor floor. Benchmark authentication capacity
on deployment hardware before changing the work factor.

CORS allows only the configured public origin, GET/POST and the headers used by
this API. The HTTPS scheme is normalized before determining cookie protection.
Deploy the production dashboard and `/api` behind the same HTTPS origin. The
reverse proxy must enforce TLS and discard client-supplied forwarding headers;
configure Uvicorn to trust only that proxy. Source attempt limits otherwise see
the proxy or NAT address. Cookies stolen from an active browser remain usable
until server-side expiry or revocation; HttpOnly does not prevent XSS from
making requests in that browser. Revocation blocks subsequent authentication,
but an operation already authorized may finish.

Merchant API keys remain available for server integrations. The administrative
merchant creation API issues a key only when `issue_api_key=true` is requested.
It requires an external `ADMIN_API_TOKEN` or `ADMIN_API_TOKEN_FILE`.
A panel session cannot access this administrative endpoint. Requests combining
a dashboard cookie with Bearer credentials are rejected.

## Historical ownership and recovery

The additive expansion introduces nullable merchant ownership for historical
payments and idempotency records. New API-created payments require an owner.
Partial unique indexes preserve historical references, and database constraints
keep idempotency records associated with their payment's merchant.

The previously unpublished merchant revision is represented as a safe
expansion in this candidate. A following compatibility revision also accepts
an already-expanded database with mandatory merchant columns. Applied revision
state must be checked before planning any persistent upgrade. The tests run
these paths exclusively on disposable databases.

Downgrades reject removal of merchants or dashboard users that contain data.
Keep backups encrypted outside the repository and verify restores separately.
Historical ownership assignment is a separate, explicitly approved operation.
The migration never guesses a merchant from an email or reference.

## Required validation for subsequent features

The Stripe commit must preserve a logical payment's idempotency across retries
and reconcile uncertain provider responses against the existing PaymentIntent.
Real payments must remain disabled until those behaviors are verified.

The webhook commit must integrate authenticated encryption of signing secrets,
prevent SSRF including private destinations and DNS/redirect bypasses, and
verify isolation between merchants. These features are outside this candidate.

## Credential handling

Older published configuration contains a known development database password.
Rotate any database that reused it through an explicitly approved database and
application credential change. Replacing configuration does not erase prior
Git history or change an existing database password.

Private connection URLs, passwords, integration keys, and encryption key files
belong outside source directories and build contexts. The example file records
paths and fictitious identifiers only. Dashboard authentication uses sessions.


## Stripe test gateway

The reusable gateway uses `stripe==15.3.1`, `StripeClient.v1.payment_intents`
and API version `2026-06-24.dahlia`. It is not wired to the API, worker or
frontend; the simulator and dashboard authentication continue to run as before.
Setting a key does not activate payments. Worker execution requires the explicit
opt-in documented below; API-created payments remain simulator payments.

`STRIPE_MODE` accepts only `test`. Supply either `STRIPE_SECRET_KEY_FILE`
(preferred, outside source/build contexts) or a server-only `STRIPE_SECRET_KEY`,
never both. Only secret/restricted test-key prefixes are accepted; live and
publishable keys are rejected before I/O. Keys use SecretStr and errors never
include their values. `STRIPE_TIMEOUT_SECONDS` is finite, positive and at most
30 seconds (default 10). TLS verification remains enabled. SDK verbose logging
(`STRIPE_LOG` or `stripe.log`) is rejected, and its raw INFO/DEBUG logger is
suppressed; application code should log only normalized error codes.

`create_payment` creates and confirms a PaymentIntent using EUR, USD or GBP.
Amounts must be Decimal, exactly representable in cents, positive and within
Stripe's eight-digit minor-unit limit. Trailing zeroes are accepted; rounding
and floating-point inputs are rejected. Currency is deterministically encoded
in lowercase. Only payment-method references are accepted, never card data.
Optional metadata is limited to UUID `merchant_id`, `payment_id` and
`operation_id` values. The caller's metadata is copied, not mutated.

Every create requires a caller-supplied idempotency key of 1–255 visible ASCII
characters, preserved exactly. The gateway neither generates keys nor retries
automatically. Identical calls produce identical payment parameters. The caller
must persist the key and immutable request, bound retries to Stripe's retention
window, and reconcile uncertain results before another operation. That durable
workflow belongs to later activities; this adapter alone does not implement it.
API v1 keys may be pruned after 24 hours; an unresolved operation must then be
reconciled instead of creating another Intent with either an old or a new key.
`retrieve_payment` only issues GET and never creates or confirms a payment.

Results contain a normalized status, selected monetary evidence, provider ID,
allowlisted UUID metadata and a fixed error code. They exclude client_secret,
customer/payment details, raw responses and exception messages. Live-mode,
malformed or inconsistent responses never produce success. Intermediate states
(including action/confirmation/capture requirements) remain PENDING; network,
timeout, 409, 429 and 5xx errors are UNKNOWN, not evidence that no charge exists.
A card error without verifiable Intent evidence is also UNKNOWN; any extracted
ID is only a candidate for later reconciliation. Configuration errors are not
card declines.

Unit tests inject a fake HTTP transport into the real pinned SDK and block
network sockets. No account, credential or Stripe service is accessed. Even an
external test-mode verification requires separate approval. Durable operations
and opt-in worker reconciliation are described below. Public provider DTOs and
the dashboard provider view are documented in the activity 5/6 section below.


## Durable provider operations (Stripe activity 2)

Each new Payment fixes its processing backend at creation. Existing API calls
still create simulator payments; Stripe is disabled by default. Internal service
callers may prepare a Stripe test operation with an explicit account and test
PaymentMethod reference, without any credentials or external requests.

The payment UUID is allocated before insert. Payment, provider idempotency key,
canonical JSON request/hash, immutable account/test context and queue event are
committed atomically. The client HTTP Idempotency-Key remains separate; a replay
returns the original stored provider/context even if server configuration changes.
No customer email, description, API key or client_secret enters the provider
snapshot. A scoped repository lookup requires both merchant and expected account.

Revision `s001_stripe_payment_operations` follows `a001_dashboard_sessions` and
adds nullable columns without a server default or historical backfill. Provider
reference/index logic is recovered from the unpublished 0bb migration, retaining
an already-present compatible column and its values. Unknown revision histories
require a separate approved recovery plan; no revision is stamped or rewritten.
Owned and unowned historical payments keep IDs, owners, references and statuses,
with processing_backend=NULL. They remain paused for explicit classification.
Only explicit simulator rows are claimed by the default worker. Prepared Stripe
rows need the opt-in worker described below. Changing an environment variable cannot reroute a payment.

PostgreSQL checks validate the request SHA-256 and its amount/currency/UUID/account
binding. Unique constraints protect provider keys and provider references per
account/test context. A trigger rejects changes to operation identity, monetary
parameters, provider request/key/account or an established provider reference;
once recorded, the first-attempt/retry deadline cannot move. Stripe operations
cannot be deleted without an explicit recovery plan; TRUNCATE is also blocked.
Provider references must have the expected format for new Stripe operations.
The retry window is fixed
at 23 hours after first emission; preparation leaves emission timestamps empty.
This activity performs no emissions or reconciliation transitions.

JSON is stored as canonical text so Python and PostgreSQL hash the same bytes,
without an extension. These guarantees require the Alembic PostgreSQL schema;
SQLite and metadata.create_all are fast-test conveniences, not equivalent
operation-immutability enforcement. Do not run a persistent upgrade without a
verified backup, schema assessment and explicit authorization. Downgrade refuses
to remove classified payments/provider evidence. Empty/legacy-only downgrade
removes the additive columns; if a provider-reference column predated this
revision, that schema detail must also be reviewed before any downgrade.

The following section adds leases/fencing and opt-in execution/reconciliation.
The public provider contract and dashboard are documented below. No production account or credential
is supported. Real external test-mode calls still need separate approval.


## Recoverable test-mode Stripe worker (activities 3 and 4)

Execution remains disabled (`STRIPE_WORKER_ENABLED=false`). Opt-in requires an
explicit `STRIPE_ACCOUNT_ID`, a valid server-side test key/file, and a lease longer
than the configured transport budget. Keep the private file outside the repository
and Docker context, mounted only into the worker. No secret is needed by React.
This code has been verified with mocks only; external Stripe testing still needs
separate authorization. This activity does not activate Stripe in Compose or API
payment creation, implement Connect, or introduce card/SCA collection.

The worker verifies the current key's account through SDK v1
`accounts.retrieve_current()` before any PaymentIntent request. A different
account/key fails safely. The repository selects only test operations bound to
the configured account. Account selection never comes from an HTTP body or queue
payload. A future account rotation cannot rewrite a stored operation's context.

Revision `s002_stripe_execution_leases` follows s001 without altering published
migrations. Four fields on Payment hold execution token, lease deadline, attempt
counter and next reconciliation time. Historical data is not classified or sent.
Row locking with SKIP LOCKED grants one active lease per payment, even with
duplicate messages. Claims and first-attempt/retry deadlines commit before I/O.
The processor receives a detached immutable contract, not a live ORM transaction.
Applying a result locks the row and requires the same merchant/account/token and
an unexpired lease. Stale results do not overwrite current evidence. New workers
recover expired PROCESSING operations with a fresh token and the original key,
parameters and retry deadline. Late duplicate messages are acknowledged without
another provider call. Leases do not renew the provider's idempotency window.

Known PaymentIntent references always use GET, including after the retry window.
With no reference, identical POST replay is permitted only inside the conservative
23-hour window. A monotonic budget anchored to database time is checked before
I/O and again before SDK POST after account lookup. An expired/unsafe window
requires review instead of a new key or charge. Network errors, timeouts, 5xx and
incomplete responses are UNKNOWN, not proof of a decline. Reconciliation repeats
with the configured delay, without applying the simulator's maximum retry rule.

COMPLETED requires coherent succeeded evidence: reference, exact amount received,
currency, verified account, merchant/payment metadata and livemode=false. Verified
cancellation/decline maps to FAILED. Intermediate states keep PROCESSING with
pending/requires_action provider state; unknown and review_required also keep
PROCESSING publicly. No automatic confirmation/capture or manual-review action is
added. Final succeeded/failed evidence is frozen by a PostgreSQL trigger. The
dashboard exposes sanitized provider state as documented below; no credentials,
snapshot or idempotency key is added to HTTP responses here.

A lease fences database writes; it cannot cancel a provider request already in
flight during a process pause. Overlapping transport attempts use the same
persisted key/parameters, so provider idempotency is the external deduplication
boundary. Tests demonstrate one simulated provider operation after lost responses,
concurrent attempts and restarts, not a distributed exactly-once transaction.
Production deployment also requires a trustworthy synchronized database clock,
minimal DB privileges and an approved recovery procedure. Live mode is forbidden.
Downgrade refuses to erase execution evidence. Synthetic backup/restore tests
include active tokens/leases; persistent migrations are not authorized here.


## Dashboard provider contract and validation (activities 5 and 6)

The existing payment list, form, summaries and detail panel now show sanitized
Stripe references and provider states. UNKNOWN and REVIEW_REQUIRED remain distinct
from a confirmed FAILED payment. Their financial status stays PROCESSING; the
panel explains uncertainty, paused automatic processing and required review.
Known intermediate states never imply a completed charge. Historical unclassified
payments keep their original data and remain paused. New dashboard payments still
use the simulator; this UI does not activate Stripe, collect cards or expose a
client_secret. No Stripe browser SDK or merchant API key is added.

Public payment/list/processing DTOs add only processing_backend, provider_state,
provider_status and a format-checked reference for explicitly classified Stripe
operations. They never include provider account IDs, idempotency keys, request
snapshots/hashes, execution tokens or leases. Historical references stay stored
but are not disclosed as verified Stripe references before classification.
Processing notes use an explicit safe allowlist; unknown internal messages are
replaced with a generic note. Client integrations must treat failure_reason and
queue notes as display text, not parse them as stable machine error codes.

`usePayments` owns read lifecycle and cancellation. With the current unpaginated
list contract, the panel derives summaries and selected record data from the same
response, polling only the list and selected event history. Polls schedule after
settlement, skip hidden documents and abort on selection/session changes. A
generation guard prevents a pre-creation list response from replacing a newly
created record. A future paginated API must provide corresponding aggregate data
instead of deriving whole-merchant totals from one page.

Payment creation uses a synchronous double-submit guard and one HTTP
Idempotency-Key for an unchanged form after an unconfirmed response. It never
automatically resends a POST. Failed/unconfirmed submissions retain their inputs;
confirmed responses clear the form and announce that the payment was saved.
This client key stays separate from the persisted Stripe key. Logout hides all
merchant data immediately; an unconfirmed revocation blocks the panel and offers
an explicit retry. Session cookies remain HttpOnly; CSRF stays in memory, without
browser storage or integration API keys. Stale 401 responses cannot invalidate a
newer client authentication generation.

No data/failed initial reads are distinguished from a confirmed zero total. The
panel keeps last known data on read failure, shows interrupted updates, and uses
safe error text. Keyboard-selectable references, pressed filter states, focus
outlines, live messages, reduced motion and contained mobile table scrolling are
functional improvements; the existing visual structure and palette are retained.

Vite exposes no custom environment prefix (`envPrefix: []`). Configuration remains
server-side, including API_PROXY_TARGET. Docker and CI use Node 24 LTS; the image
uses npm ci and runs the TypeScript/Vite build. Compatible lockfile fixes remove
the dependency audit findings without upgrading the Vite major version. Backend
security fixes pin FastAPI 0.165.0, Starlette 1.6.0 and pytest 9.0.3; the Stripe SDK
and operation schema remain unchanged. Full backend/authentication/concurrency
regression is required after dependency changes.

### Reproducible browser tests

Use Node 24 and the declared backend Python dependencies. Provide an independent,
disposable PostgreSQL database explicitly; the fixture never falls back to the
application DATABASE_URL. It accepts only a PostgreSQL `_test` database at a
loopback/CI host, creates a random schema, seeds synthetic users and payments,
and uses two workers with the existing fake provider. It loads application settings
from a fresh temporary working directory with a cleared environment, so developer
.env files and provider credentials cannot activate external requests.

With the backend virtual environment activated:

```bash
cd frontend
npm ci
npx playwright install chromium
E2E_POSTGRES_URL=postgresql+psycopg://pulsepay_test@127.0.0.1:5432/pulsepay_browser_test npm run test:e2e
```

For an installed Chrome, set E2E_BROWSER_CHANNEL=chrome. E2E_PYTHON can select the
backend interpreter; E2E_FRONTEND_PORT and E2E_API_PORT can select unused local
ports. Existing servers are never reused. The fixture requires explicit opt-in
from Playwright, refuses malformed/non-test/remote configuration before creating
a schema, and offers no test-control route in the application. Its fixed dummy
credentials belong only to synthetic fixtures and must never be used for deployment.

The browser suite covers provider states, lost creation responses, duplicate
submits, cookie/session revocation, merchant switching, foreign IDs, CSRF, slow
polling, safe read errors, keyboard/mobile behavior and unconfirmed logout.
Page requests outside loopback are blocked. Traces containing headers/cookies are
disabled. Generated screenshots and reports are ignored by Git.

CI has backend tests with PostgreSQL 16 plus restore, frontend build/audit on
Node 24, and browser integration with synthetic Stripe. It requires zero skipped
backend cases, uses read-only repository permissions and does not persist checkout
credentials. Local reproduction of these commands is evidence of local validation;
remote GitHub checks must still pass after an explicitly authorized branch push.
No permanent migrations, provider requests or deployments are part of these tests.

## Independent future UI/UX phase (planned only)

The next visual direction is a premium banking-style fintech interface: clarity
and usability inspired by BBVA, blue/turquoise direction similar to CaixaBank,
with an original PulsePay identity. No bank assets, branding or full redesign
are implemented in this activity.

Keep typed API contracts, usePayments, paymentPresentation and the session-aware
client independent of layout/theme changes. Preserve the browser and financial
regression tests during redesign. Review dashboard density and oversized hero
space; navigation and discoverability of mobile payment details; responsive table
column priorities; clearer attempt/retry terminology; accessible field-level
validation and freshness indicators; and consistent state language. Pagination,
larger data volumes and broader assistive-technology checks need their own design
and acceptance criteria. Visual changes must preserve authentication, CSRF,
merchant isolation, idempotency, worker fencing and reconciliation semantics.
