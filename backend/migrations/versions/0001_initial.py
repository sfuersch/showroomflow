"""Initial multi-tenant and authentication tables."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

user_role = sa.Enum("SYSTEM_ADMIN", "DEALERSHIP_ADMIN", "PHOTOGRAPHER", name="userrole")
job_status = sa.Enum(
    "DRAFT",
    "CAPTURING",
    "UPLOADING",
    "PROCESSING",
    "REVIEW_REQUIRED",
    "EXPORTING",
    "COMPLETED",
    "FAILED",
    name="jobstatus",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "dealerships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("auto_export_enabled", sa.Boolean(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "locations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dealership_id", sa.Uuid(), sa.ForeignKey("dealerships.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_locations_dealership_id", "locations", ["dealership_id"])
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dealership_id", sa.Uuid(), sa.ForeignKey("dealerships.id"), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_dealership_id", "users", ["dealership_id"])
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])
    op.create_index("ix_refresh_sessions_token_hash", "refresh_sessions", ["token_hash"])
    op.create_table(
        "vehicle_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dealership_id", sa.Uuid(), sa.ForeignKey("dealerships.id"), nullable=False),
        sa.Column("location_id", sa.Uuid(), sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("vin", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("brand", sa.String(100), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("auto_export", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("dealership_id", "vin", "version", name="uq_vehicle_job_version"),
    )
    op.create_index("ix_vehicle_jobs_dealership_id", "vehicle_jobs", ["dealership_id"])
    op.create_index("ix_vehicle_jobs_location_id", "vehicle_jobs", ["location_id"])
    op.create_index("ix_vehicle_jobs_vin", "vehicle_jobs", ["vin"])
    op.create_table(
        "export_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("vehicle_job_id", sa.Uuid(), sa.ForeignKey("vehicle_jobs.id"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("zip_filename", sa.String(255), nullable=False),
        sa.Column("successful", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.String(1000), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_export_runs_vehicle_job_id", "export_runs", ["vehicle_job_id"])


def downgrade() -> None:
    op.drop_table("export_runs")
    op.drop_table("vehicle_jobs")
    op.drop_table("refresh_sessions")
    op.drop_table("users")
    op.drop_table("locations")
    op.drop_table("dealerships")
    job_status.drop(op.get_bind(), checkfirst=True)
    user_role.drop(op.get_bind(), checkfirst=True)
