"""Persist immutable test-provider operations without classifying historical payments."""

from alembic import op
import sqlalchemy as sa

revision = "s001_stripe_payment_operations"
down_revision = "a001_dashboard_sessions"
branch_labels = None
depends_on = None

CONTRACT_SQL = """(
 (processing_backend IS NULL AND provider_account_id IS NULL AND provider_livemode IS NULL
  AND provider_idempotency_key IS NULL AND provider_request IS NULL AND provider_request_hash IS NULL
  AND provider_state IS NULL AND provider_status IS NULL AND provider_first_attempt_at IS NULL AND provider_retry_until IS NULL)
 OR
 (processing_backend = 'simulator' AND provider_reference IS NULL AND provider_account_id IS NULL AND provider_livemode IS NULL
  AND provider_idempotency_key IS NULL AND provider_request IS NULL AND provider_request_hash IS NULL
  AND provider_state IS NULL AND provider_status IS NULL AND provider_first_attempt_at IS NULL AND provider_retry_until IS NULL)
 OR
 (processing_backend = 'stripe' AND merchant_id IS NOT NULL AND provider_account_id IS NOT NULL
  AND provider_livemode IS FALSE AND provider_idempotency_key IS NOT NULL AND provider_request IS NOT NULL
  AND provider_request_hash IS NOT NULL AND length(provider_request_hash) = 64
  AND provider_state IN ('prepared','in_flight','pending','requires_action','unknown','review_required','succeeded','failed')
  AND (provider_status IS NULL OR provider_status IN ('succeeded','canceled','processing','requires_payment_method','requires_confirmation','requires_action','requires_capture'))
  AND ((provider_state = 'prepared' AND status = 'PENDING' AND provider_reference IS NULL AND provider_status IS NULL
        AND provider_first_attempt_at IS NULL AND provider_retry_until IS NULL)
       OR (provider_state <> 'prepared' AND provider_first_attempt_at IS NOT NULL
           AND provider_retry_until IS NOT NULL AND provider_retry_until > provider_first_attempt_at)))
) IS TRUE"""
REQUEST_SQL = """processing_backend IS DISTINCT FROM 'stripe' OR (
 provider_account_id ~ '^acct_[A-Za-z0-9]{1,250}$'
 AND (provider_reference IS NULL OR provider_reference ~ '^pi_[A-Za-z0-9_]{1,250}$')
 AND currency IN ('EUR','USD','GBP') AND amount > 0 AND amount <= 999999.99
 AND provider_idempotency_key = 'pp:stripe:v1:' || merchant_id::text || ':' || id::text
 AND provider_request_hash = encode(sha256(convert_to(provider_request, 'UTF8')), 'hex')
 AND (provider_request::jsonb->'params'->>'payment_method') ~ '^pm_[A-Za-z0-9_]{1,250}$'
 AND provider_request::jsonb = jsonb_build_object(
   'operation', 'payment_intent.create_confirm', 'api_version', '2026-06-24.dahlia',
   'account_id', provider_account_id, 'livemode', false,
   'params', jsonb_build_object(
     'amount', (amount * 100)::bigint, 'currency', lower(currency),
     'payment_method', provider_request::jsonb->'params'->>'payment_method',
     'confirm', true, 'capture_method', 'automatic',
     'automatic_payment_methods', jsonb_build_object('enabled', true, 'allow_redirects', 'never'),
     'metadata', jsonb_build_object('merchant_id', merchant_id::text, 'payment_id', id::text)))
 AND (provider_first_attempt_at IS NULL OR provider_retry_until = provider_first_attempt_at + interval '23 hours')
) IS TRUE"""
GUARD_SQL = """CREATE FUNCTION pulsepay_guard_payment_operation() RETURNS trigger LANGUAGE plpgsql AS $$
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


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"]: item for item in inspector.get_columns("payments")}
    if "provider_reference" in existing:
        column = existing["provider_reference"]
        if not isinstance(column["type"], sa.String) or column["type"].length != 255 or not column["nullable"]:
            raise RuntimeError("Existing provider reference requires an explicit recovery plan.")
    else:
        # Reuse the valid additive part of the unpublished local 0bb revision.
        op.add_column("payments", sa.Column("provider_reference", sa.String(255), nullable=True))
    if "ix_payments_provider_reference" not in {item["name"] for item in inspector.get_indexes("payments")}:
        op.create_index("ix_payments_provider_reference", "payments", ["provider_reference"])
    for column in (
        sa.Column("processing_backend", sa.String(16)),
        sa.Column("provider_account_id", sa.String(255)),
        sa.Column("provider_livemode", sa.Boolean()),
        sa.Column("provider_idempotency_key", sa.String(255)),
        sa.Column("provider_request", sa.Text()),
        sa.Column("provider_request_hash", sa.String(64)),
        sa.Column("provider_state", sa.String(32)),
        sa.Column("provider_status", sa.String(32)),
        sa.Column("provider_first_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("provider_retry_until", sa.DateTime(timezone=True)),
    ):
        op.add_column("payments", column)
    op.create_unique_constraint("uq_payment_provider_key", "payments", ["provider_account_id", "provider_livemode", "provider_idempotency_key"])
    op.create_unique_constraint("uq_payment_provider_reference", "payments", ["provider_account_id", "provider_livemode", "provider_reference"])
    op.create_check_constraint("ck_payment_provider_contract", "payments", CONTRACT_SQL)
    op.create_check_constraint("ck_payment_stripe_request", "payments", REQUEST_SQL)
    op.execute(GUARD_SQL)
    op.execute("CREATE TRIGGER trg_payment_operation_immutable BEFORE UPDATE OR DELETE ON payments "
               "FOR EACH ROW EXECUTE FUNCTION pulsepay_guard_payment_operation()")
    op.execute("CREATE TRIGGER trg_payment_operations_no_truncate BEFORE TRUNCATE ON payments "
               "FOR EACH STATEMENT EXECUTE FUNCTION pulsepay_guard_payment_operation()")


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS(SELECT 1 FROM payments WHERE processing_backend IS NOT NULL OR provider_reference IS NOT NULL)"
    )):
        raise RuntimeError("Downgrade requires a verified recovery plan for provider operations.")
    op.execute("DROP TRIGGER trg_payment_operations_no_truncate ON payments")
    op.execute("DROP TRIGGER trg_payment_operation_immutable ON payments")
    op.execute("DROP FUNCTION pulsepay_guard_payment_operation()")
    for name in ("ck_payment_stripe_request", "ck_payment_provider_contract"):
        op.drop_constraint(name, "payments", type_="check")
    for name in ("uq_payment_provider_reference", "uq_payment_provider_key"):
        op.drop_constraint(name, "payments", type_="unique")
    op.drop_index("ix_payments_provider_reference", table_name="payments")
    for name in ("processing_backend", "provider_account_id", "provider_livemode", "provider_idempotency_key",
                 "provider_request", "provider_request_hash", "provider_state", "provider_status",
                 "provider_first_attempt_at", "provider_retry_until", "provider_reference"):
        op.drop_column("payments", name)
