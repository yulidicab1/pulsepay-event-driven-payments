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

```bash
docker compose up --build
```

Available services:

- Frontend dashboard: [http://localhost:5173](http://localhost:5173)
- Backend API: [http://localhost:8000](http://localhost:8000)
- Health endpoint: [http://localhost:8000/api/v1/health](http://localhost:8000/api/v1/health)
- PostgreSQL: `localhost:5432`

### Run backend locally without Docker

The database schema is managed by Alembic. Apply the migrations before starting
the API (the app no longer creates tables on startup):

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

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

From the repo root:

```bash
cd backend
.venv/bin/pytest
```

## Configuration

The defaults are tuned for local Docker Compose usage.

### Backend environment variables

- `DATABASE_URL`
- `API_V1_PREFIX`
- `APP_ENV`
- `PAYMENT_QUEUE_NAME`
- `PAYMENT_MAX_RETRIES`
- `PAYMENT_RETRY_DELAY_SECONDS`
- `PAYMENT_RANDOM_FAILURE_RATE`
- `WORKER_POLL_INTERVAL_SECONDS`
- `WORKER_BATCH_SIZE`

### Database migrations

Schema changes are managed with Alembic (`backend/migrations`). Under Docker
Compose the backend runs `alembic upgrade head` automatically before starting
the API. Common commands from `backend/`:

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
- Add authentication and per-merchant authorization
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

