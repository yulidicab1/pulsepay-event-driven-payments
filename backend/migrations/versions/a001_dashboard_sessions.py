"""Add dashboard identity and sessions; accept already-expanded merchant schemas."""

from alembic import op
import sqlalchemy as sa

revision = "a001_dashboard_sessions"
down_revision = "00cf512790ec"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("payments", "idempotency_keys"):
        op.alter_column(table, "merchant_id", existing_type=sa.Uuid(), nullable=True)
        for fk in inspector.get_foreign_keys(table):
            if fk["constrained_columns"] == ["merchant_id"] and fk["options"].get("ondelete") != "RESTRICT":
                op.drop_constraint(fk["name"], table, type_="foreignkey")
                op.create_foreign_key("fk_" + table + "_merchant", table, "merchants", ["merchant_id"], ["id"], ondelete="RESTRICT")
    payment_uniques = {item["name"] for item in inspector.get_unique_constraints("payments")}
    if "uq_payment_owner" not in payment_uniques:
        op.create_unique_constraint("uq_payment_owner", "payments", ["id", "merchant_id"])
    owner_fks = {item["name"] for item in inspector.get_foreign_keys("idempotency_keys")}
    if "fk_idempotency_payment_owner" not in owner_fks:
        # Conflicting historical associations require an explicit recovery decision.
        inconsistent = bind.scalar(sa.text(
            "SELECT EXISTS(SELECT 1 FROM idempotency_keys k JOIN payments p ON p.id=k.payment_id "
            "WHERE k.merchant_id IS DISTINCT FROM p.merchant_id)"
        ))
        if inconsistent:
            raise RuntimeError("Historical ownership is inconsistent; explicit reconciliation is required.")
        op.create_foreign_key(
            "fk_idempotency_payment_owner", "idempotency_keys", "payments",
            ["payment_id", "merchant_id"], ["id", "merchant_id"], deferrable=True, initially="DEFERRED",
        )
    for table, name, column in (
        ("payments", "uq_legacy_payment_reference", "merchant_reference"),
        ("idempotency_keys", "uq_legacy_idempotency_key", "key"),
    ):
        if name not in {index["name"] for index in inspector.get_indexes(table)}:
            op.create_index(name, table, [column], unique=True, postgresql_where=sa.text("merchant_id IS NULL"))
    op.create_table(
        "dashboard_users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_dashboard_users_merchant_id", "dashboard_users", ["merchant_id"])
    op.create_table(
        "dashboard_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("dashboard_users.id", ondelete="CASCADE")),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dashboard_sessions_user_id", "dashboard_sessions", ["user_id"])
    op.create_index("ix_dashboard_sessions_expires_at", "dashboard_sessions", ["expires_at"])
    op.create_table(
        "login_throttles",
        sa.Column("bucket", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_login_throttles_window_start", "login_throttles", ["window_start"])


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM dashboard_users)")):
        raise RuntimeError("Downgrade requires a verified recovery plan for dashboard users.")
    for table in ("dashboard_sessions", "dashboard_users", "login_throttles"):
        op.drop_table(table)
