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


## Stripe test gateway (not connected to payment processing)

The reusable gateway uses `stripe==15.3.1`, `StripeClient.v1.payment_intents`
and API version `2026-06-24.dahlia`. It is not wired to the API, worker or
frontend; the simulator and dashboard authentication continue to run as before.
Setting Stripe configuration does not activate payments.

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
external test-mode verification requires separate approval. Account ownership,
durable idempotency, leases, reconciliation and integration are still pending.


## Durable provider operations (Stripe activity 2)

Each new Payment fixes its processing backend at creation. Existing API calls
still create simulator payments; the gateway is not connected. Internal service
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
Only explicit simulator rows are claimed by the current worker; prepared Stripe
rows are also excluded. Changing an environment variable cannot reroute a payment.

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

Leases/fencing, provider execution/reconciliation, account verification, public
provider DTOs and UI remain later activities. No production account or credential
is supported. Real external test-mode calls still need separate approval.
