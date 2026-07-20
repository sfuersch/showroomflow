"""Add central orientation catalog and tenant assignments."""

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "0018_orientation_catalog"
down_revision = "0017_benchmark_images"
branch_labels = None
depends_on = None


ORIENTATIONS = [
    ("front", "Front", "Fahrzeug gerade und vollständig von vorne aufnehmen.", "exterior", True),
    ("front-left", "Diagonal vorne links", "Vordere linke Fahrzeugecke vollständig zeigen.", "exterior", True),
    ("left", "Seite links", "Linke Fahrzeugseite gerade und vollständig aufnehmen.", "exterior", True),
    ("rear-left", "Diagonal hinten links", "Hintere linke Fahrzeugecke vollständig zeigen.", "exterior", True),
    ("rear", "Heck", "Fahrzeug gerade und vollständig von hinten aufnehmen.", "exterior", True),
    ("rear-right", "Diagonal hinten rechts", "Hintere rechte Fahrzeugecke vollständig zeigen.", "exterior", True),
    ("right", "Seite rechts", "Rechte Fahrzeugseite gerade und vollständig aufnehmen.", "exterior", True),
    ("front-right", "Diagonal vorne rechts", "Vordere rechte Fahrzeugecke vollständig zeigen.", "exterior", True),
    ("interior", "Innenraum", "Gesamteindruck des Innenraums aufnehmen.", "interior", False),
    ("steering-wheel", "Lenkrad", "Lenkrad mittig und ohne Spiegelungen aufnehmen.", "detail", False),
    ("dashboard", "Armaturenbrett", "Armaturenbrett vollständig und scharf aufnehmen.", "detail", False),
    ("interior-left", "Blick ins Fahrzeug links", "Seitlichen Einblick durch die linke Tür aufnehmen.", "interior", False),
    ("interior-right", "Blick ins Fahrzeug rechts", "Seitlichen Einblick durch die rechte Tür aufnehmen.", "interior", False),
    ("rear-seat-left", "Rücksitzbank links", "Rücksitzbank von der linken Fahrzeugseite aufnehmen.", "interior", False),
    ("rear-seat-right", "Rücksitzbank rechts", "Rücksitzbank von der rechten Fahrzeugseite aufnehmen.", "interior", False),
    ("trunk", "Kofferraum", "Geöffneten Kofferraum vollständig aufnehmen.", "detail", False),
]


def _orientation_id(position: int) -> uuid.UUID:
    return uuid.UUID(f"8d84c123-77f0-4aa0-9000-{position:012d}")


def upgrade() -> None:
    orientations = op.create_table(
        "orientations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(80), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False, unique=True),
        sa.Column("instruction", sa.String(500), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("default_capture_order", sa.Integer(), nullable=False),
        sa.Column("default_export_order", sa.Integer(), nullable=True),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("requires_processing", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        orientations,
        [
            {
                "id": _orientation_id(position),
                "key": key,
                "name": name,
                "instruction": instruction,
                "category": category,
                "default_capture_order": position,
                "default_export_order": position,
                "is_required": True,
                "requires_processing": processing,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
            for position, (key, name, instruction, category, processing) in enumerate(
                ORIENTATIONS, start=1
            )
        ],
    )
    op.add_column("capture_steps", sa.Column("orientation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_capture_steps_orientation_id",
        "capture_steps",
        "orientations",
        ["orientation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_capture_steps_orientation_id", "capture_steps", ["orientation_id"])
    connection = op.get_bind()
    for position, (_, name, _, _, _) in enumerate(ORIENTATIONS, start=1):
        connection.execute(
            sa.text(
                "UPDATE capture_steps SET orientation_id = :orientation_id "
                "WHERE name = :name AND orientation_id IS NULL"
            ),
            {"orientation_id": _orientation_id(position), "name": name},
        )


def downgrade() -> None:
    op.drop_index("ix_capture_steps_orientation_id", table_name="capture_steps")
    op.drop_constraint("fk_capture_steps_orientation_id", "capture_steps", type_="foreignkey")
    op.drop_column("capture_steps", "orientation_id")
    op.drop_table("orientations")
