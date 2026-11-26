"""Recoverable per-payment Stripe leases; no provider execution or historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = "s002_stripe_execution_leases"
down_revision = "s001_stripe_payment_operations"
branch_labels = None
depends_on = None

EXECUTION_CONTRACT_SQL = """(provider_attempt_count >= 0
 AND ((provider_execution_token IS NULL AND provider_lease_until IS NULL)
      OR (provider_execution_token IS NOT NULL AND provider_lease_until IS NOT NULL
          AND processing_backend = 'stripe' AND provider_state = 'in_flight'))
 AND (processing_backend = 'stripe' OR (provider_execution_token IS NULL
      AND provider_next_attempt_at IS NULL AND provider_attempt_count = 0))
 AND (processing_backend IS DISTINCT FROM 'stripe' OR (
      (provider_state = 'succeeded' AND status = 'COMPLETED' AND provider_status = 'succeeded' AND provider_reference IS NOT NULL)
   OR (provider_state = 'failed' AND status = 'FAILED' AND provider_status IN ('canceled','requires_payment_method') AND provider_reference IS NOT NULL)
   OR (provider_state = 'prepared' AND status = 'PENDING')
   OR (provider_state IN ('in_flight','pending','requires_action','unknown','review_required') AND status = 'PROCESSING')
 ))) IS TRUE"""

PREVIOUS_GUARD_SQL = """CREATE FUNCTION pulsepay_guard_payment_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'TRUNCATE' THEN
   RAISE EXCEPTION 'Payment truncation requires a recovery plan.' USING ERRCODE = '23514';
 END IF;
 IF TG_OP = 'DELETE' THEN
   IF OLD.processing_backend = 'stripe' OR OLD.provider_reference IS NOT NULL THEN
     RAISE EXCEPTION 'Payment operation deletion requires a recovery plan.' USING ERRCODE = '23514';
   END IF;
   RETURN OLD;
 END IF;
 IF NEW.id IS DISTINCT FROM OLD.id OR NEW.processing_backend IS DISTINCT FROM OLD.processing_backend
    OR (OLD.merchant_id IS NOT NULL AND NEW.merchant_id IS DISTINCT FROM OLD.merchant_id) THEN
   RAISE EXCEPTION 'Payment operation identity is immutable.' USING ERRCODE = '23514';
 END IF;
 IF OLD.processing_backend IS NOT NULL AND
    ROW(NEW.merchant_reference, NEW.amount, NEW.currency, NEW.provider_account_id, NEW.provider_livemode,
        NEW.provider_idempotency_key, NEW.provider_request, NEW.provider_request_hash)
    IS DISTINCT FROM
    ROW(OLD.merchant_reference, OLD.amount, OLD.currency, OLD.provider_account_id, OLD.provider_livemode,
        OLD.provider_idempotency_key, OLD.provider_request, OLD.provider_request_hash) THEN
   RAISE EXCEPTION 'Payment operation parameters are immutable.' USING ERRCODE = '23514';
 END IF;
 IF OLD.provider_reference IS NOT NULL AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference THEN
   RAISE EXCEPTION 'Provider reference is immutable.' USING ERRCODE = '23514';
 END IF;
 IF OLD.provider_first_attempt_at IS NOT NULL AND
    ROW(NEW.provider_first_attempt_at, NEW.provider_retry_until) IS DISTINCT FROM
    ROW(OLD.provider_first_attempt_at, OLD.provider_retry_until) THEN
   RAISE EXCEPTION 'Provider retry window is immutable.' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END;
$$"""

EXECUTION_GUARDS = """ IF NEW.provider_attempt_count < OLD.provider_attempt_count THEN
   RAISE EXCEPTION 'Provider execution counter cannot decrease.' USING ERRCODE = '23514';
 END IF;
 IF OLD.provider_state IN ('succeeded','failed') AND
    ROW(NEW.provider_state, NEW.provider_status, NEW.status, NEW.failure_reason)
    IS DISTINCT FROM ROW(OLD.provider_state, OLD.provider_status, OLD.status, OLD.failure_reason) THEN
   RAISE EXCEPTION 'Final provider result is immutable.' USING ERRCODE = '23514';
 END IF;
"""

GUARD_SQL = PREVIOUS_GUARD_SQL.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1).replace(
    " RETURN NEW;", EXECUTION_GUARDS + " RETURN NEW;")


def upgrade():
    op.add_column("payments", sa.Column("provider_execution_token", sa.Uuid(), nullable=True))
    op.add_column("payments", sa.Column("provider_lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("provider_attempt_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("payments", sa.Column("provider_next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_payment_execution_contract", "payments", EXECUTION_CONTRACT_SQL)
    op.create_index("ix_payment_provider_claim", "payments", ["processing_backend", "provider_account_id", "provider_next_attempt_at"])
    op.execute(GUARD_SQL)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM payments WHERE provider_attempt_count <> 0 OR provider_execution_token IS NOT NULL OR provider_next_attempt_at IS NOT NULL)")):
        raise RuntimeError("Execution downgrade requires a verified recovery plan.")
    op.execute(PREVIOUS_GUARD_SQL.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    op.drop_index("ix_payment_provider_claim", table_name="payments")
    op.drop_constraint("ck_payment_execution_contract", "payments", type_="check")
    for name in ("provider_next_attempt_at", "provider_attempt_count", "provider_lease_until", "provider_execution_token"):
        op.drop_column("payments", name)
