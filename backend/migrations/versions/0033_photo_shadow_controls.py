"""Add per-photo shadow controls.

Revision ID: 0033_photo_shadow_controls
Revises: 0032_photo_shadow_override
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_photo_shadow_controls"
down_revision: str | None = "0032_photo_shadow_override"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column_name in (
        "vehicle_shadow_distance_percent",
        "vehicle_shadow_angle_degrees",
        "vehicle_shadow_spread_percent",
        "vehicle_shadow_blur_percent",
        "vehicle_shadow_contact_percent",
    ):
        op.add_column(
            "photo_assets",
            sa.Column(column_name, sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    for column_name in reversed(
        (
            "vehicle_shadow_distance_percent",
            "vehicle_shadow_angle_degrees",
            "vehicle_shadow_spread_percent",
            "vehicle_shadow_blur_percent",
            "vehicle_shadow_contact_percent",
        )
    ):
        op.drop_column("photo_assets", column_name)
