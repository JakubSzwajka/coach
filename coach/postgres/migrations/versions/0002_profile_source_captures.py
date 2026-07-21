"""Add Profiles, source connections, and immutable captures.

Revision ID: 0002_profile_source_captures
Revises: 0001_runtime_baseline
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_profile_source_captures"
down_revision = "0001_runtime_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clerk_issuer", sa.Text(), nullable=False),
        sa.Column("clerk_subject", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(clerk_issuer) <> ''", name="ck_profile_clerk_issuer"
        ),
        sa.CheckConstraint(
            "btrim(clerk_subject) <> ''", name="ck_profile_clerk_subject"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clerk_issuer", "clerk_subject", name="uq_profile_clerk_actor"
        ),
    )
    op.create_table(
        "source_connections",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("connection_key", sa.Text(), nullable=False),
        sa.Column(
            "state", sa.Text(), nullable=False, server_default="disconnected"
        ),
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=True),
        sa.Column("encrypted_tokens", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("btrim(provider) <> ''", name="ck_source_provider"),
        sa.CheckConstraint(
            "btrim(connection_key) <> ''", name="ck_source_connection_key"
        ),
        sa.CheckConstraint(
            "state IN ('disconnected', 'connected', 'needs_reconnect')",
            name="ck_source_connection_state",
        ),
        sa.CheckConstraint(
            "encrypted_credentials IS NULL OR ("
            "octet_length(encrypted_credentials) >= 64 AND "
            "substring(encrypted_credentials from 1 for 4) = "
            "decode('6763313a', 'hex'))",
            name="ck_encrypted_credentials_envelope",
        ),
        sa.CheckConstraint(
            "encrypted_tokens IS NULL OR ("
            "octet_length(encrypted_tokens) >= 64 AND "
            "substring(encrypted_tokens from 1 for 4) = "
            "decode('6763313a', 'hex'))",
            name="ck_encrypted_tokens_envelope",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint(
            "profile_id",
            "provider",
            "connection_key",
            name="uq_source_connection_identity",
        ),
    )
    op.create_table(
        "collected_records",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_connection_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("record_kind", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("btrim(record_kind) <> ''", name="ck_record_kind"),
        sa.CheckConstraint("btrim(source_key) <> ''", name="ck_record_source_key"),
        sa.ForeignKeyConstraint(
            ["profile_id", "source_connection_id"],
            ["source_connections.profile_id", "source_connections.id"],
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint(
            "profile_id",
            "source_connection_id",
            "record_kind",
            "source_key",
            name="uq_collected_record_source_identity",
        ),
    )
    op.create_table(
        "collected_record_captures",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "collected_record_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "octet_length(content_hash) = 32", name="ck_capture_sha256_length"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "collected_record_id"],
            ["collected_records.profile_id", "collected_records.id"],
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint(
            "profile_id",
            "collected_record_id",
            "content_hash",
            name="uq_capture_record_content",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_guard_profile_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.clerk_issuer IS DISTINCT FROM NEW.clerk_issuer
               OR OLD.clerk_subject IS DISTINCT FROM NEW.clerk_subject THEN
                RAISE EXCEPTION 'profile identity binding is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER profile_identity_immutable
        BEFORE UPDATE OR DELETE ON profiles
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_profile_identity()
        """
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_guard_source_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF OLD.profile_id IS DISTINCT FROM NEW.profile_id
               OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.provider IS DISTINCT FROM NEW.provider
               OR OLD.connection_key IS DISTINCT FROM NEW.connection_key THEN
                RAISE EXCEPTION 'source connection identity is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_connection_identity_immutable
        BEFORE UPDATE ON source_connections
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_source_identity()
        """
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_reject_record_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            RAISE EXCEPTION 'collected record identity is immutable'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER collected_records_immutable
        BEFORE UPDATE OR DELETE ON collected_records
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_reject_record_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_reject_capture_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            RAISE EXCEPTION 'collected record captures are immutable'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER collected_record_captures_immutable
        BEFORE UPDATE OR DELETE ON collected_record_captures
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_reject_capture_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER collected_record_captures_immutable "
        "ON collected_record_captures"
    )
    op.execute("DROP FUNCTION garmin_coach_reject_capture_mutation()")
    op.execute("DROP TRIGGER collected_records_immutable ON collected_records")
    op.execute("DROP FUNCTION garmin_coach_reject_record_mutation()")
    op.execute(
        "DROP TRIGGER source_connection_identity_immutable "
        "ON source_connections"
    )
    op.execute("DROP FUNCTION garmin_coach_guard_source_identity()")
    op.execute("DROP TRIGGER profile_identity_immutable ON profiles")
    op.execute("DROP FUNCTION garmin_coach_guard_profile_identity()")
    op.drop_table("collected_record_captures")
    op.drop_table("collected_records")
    op.drop_table("source_connections")
    op.drop_table("profiles")
