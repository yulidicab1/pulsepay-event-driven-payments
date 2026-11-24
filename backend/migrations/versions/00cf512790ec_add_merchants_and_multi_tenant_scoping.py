"""Expand merchant ownership while preserving unassigned historical records."""

from alembic import op
import sqlalchemy as sa

revision = "00cf512790ec"
down_revision = "72888bf6baba"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "merchants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "merchant_api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), sa.ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    for column in ("merchant_id", "key_prefix", "key_hash"):
        op.create_index("ix_merchant_api_keys_" + column, "merchant_api_keys", [column], unique=column == "key_hash")
    for table, reference, constraint in (
        ("payments", "merchant_reference", "uq_payments_merchant_reference"),
        ("idempotency_keys", "key", "uq_idempotency_keys_merchant_key"),
    ):
        op.add_column(table, sa.Column("merchant_id", sa.Uuid(), nullable=True))
        op.create_foreign_key("fk_" + table + "_merchant", table, "merchants", ["merchant_id"], ["id"], ondelete="RESTRICT")
        op.create_index("ix_" + table + "_merchant_id", table, ["merchant_id"])
        op.drop_index("ix_" + table + "_" + reference, table_name=table)
        op.create_index("ix_" + table + "_" + reference, table, [reference])
        op.create_unique_constraint(constraint, table, ["merchant_id", reference])
    op.create_unique_constraint("uq_payment_owner", "payments", ["id", "merchant_id"])
    op.create_foreign_key(
        "fk_idempotency_payment_owner", "idempotency_keys", "payments",
        ["payment_id", "merchant_id"], ["id", "merchant_id"], deferrable=True, initially="DEFERRED",
    )
    op.create_index("uq_legacy_payment_reference", "payments", ["merchant_reference"], unique=True, postgresql_where=sa.text("merchant_id IS NULL"))
    op.create_index("uq_legacy_idempotency_key", "idempotency_keys", ["key"], unique=True, postgresql_where=sa.text("merchant_id IS NULL"))


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM merchants)")):
        raise RuntimeError("Downgrade requires a verified recovery plan for merchant data.")
    op.drop_constraint("fk_idempotency_payment_owner", "idempotency_keys", type_="foreignkey")
    op.drop_constraint("uq_payment_owner", "payments", type_="unique")
    op.drop_index("uq_legacy_payment_reference", table_name="payments")
    op.drop_index("uq_legacy_idempotency_key", table_name="idempotency_keys")
    for table, reference, constraint in (
        ("payments", "merchant_reference", "uq_payments_merchant_reference"),
        ("idempotency_keys", "key", "uq_idempotency_keys_merchant_key"),
    ):
        op.drop_constraint("fk_" + table + "_merchant", table, type_="foreignkey")
        op.drop_constraint(constraint, table, type_="unique")
        op.drop_index("ix_" + table + "_merchant_id", table_name=table)
        op.drop_index("ix_" + table + "_" + reference, table_name=table)
        op.drop_column(table, "merchant_id")
        op.create_index("ix_" + table + "_" + reference, table, [reference], unique=True)
    op.drop_table("merchant_api_keys")
    op.drop_table("merchants")
