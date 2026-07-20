"""Add orientation processing modes and repeatable templates."""

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "0019_orientation_templates"
down_revision = "0018_orientation_catalog"
branch_labels = None
depends_on = None


CATALOG = [
    ("front", "Vorne", "exterior", "optimized", False, 1, 1, True),
    ("front-left", "Vorne links", "exterior", "optimized", False, 1, 1, True),
    (
        "front-lower-left",
        "Vorderes unten links zugeschnitten",
        "exterior",
        "optimized",
        False,
        1,
        1,
        True,
    ),
    ("left", "Links", "exterior", "optimized", False, 1, 1, True),
    ("rear-left", "Hinten links", "exterior", "optimized", False, 1, 1, True),
    ("rear", "Hinten", "exterior", "optimized", False, 1, 1, True),
    ("rear-right", "Hinten rechts", "exterior", "optimized", False, 1, 1, True),
    ("right", "Rechts", "exterior", "optimized", False, 1, 1, True),
    ("front-right", "Vorne rechts", "exterior", "optimized", False, 1, 1, True),
    (
        "front-lower-right",
        "Vorderes unten rechts zugeschnitten",
        "exterior",
        "optimized",
        False,
        1,
        1,
        True,
    ),
    ("driver-entry", "Einstieg Fahrer", "interior", "optimized", False, 1, 1, True),
    ("steering-wheel", "Lenkrad", "interior", "optimized", False, 1, 1, True),
    ("instruments", "Instrumente", "interior", "original", False, 1, 1, True),
    ("driver-door", "Türe Fahrerseite", "interior", "original", False, 1, 1, True),
    ("rear-row-driver", "Hintere Reihe Fahrer", "interior", "original", False, 1, 1, True),
    ("front-interior", "Innenansicht vorne", "interior", "optimized", False, 1, 1, True),
    ("center-console", "Mittelkonsole", "interior", "original", False, 1, 1, True),
    ("infotainment", "Navigation/Infotainment", "interior", "original", True, 1, 5, True),
    ("passenger-door", "Türe Beifahrerseite", "interior", "original", False, 1, 1, True),
    ("passenger-entry", "Einstieg Beifahrer", "interior", "optimized", False, 1, 1, True),
    ("driver-door-open", "Fahrertür geöffnet", "interior", "optimized", False, 1, 1, True),
    ("passenger-door-open", "Beifahrertür geöffnet", "interior", "optimized", False, 1, 1, True),
    (
        "rear-row-passenger",
        "Hintere Reihe Beifahrer",
        "interior",
        "configurable",
        False,
        1,
        1,
        True,
    ),
    ("trunk-open", "Kofferraum offen", "interior", "configurable", False, 1, 1, True),
    ("engine-bay", "Motorraum", "detail", "configurable", False, 1, 1, True),
    ("wheel-front-left", "Reifen vorne links", "detail", "optimized", False, 1, 1, True),
    ("wheel-front-right", "Reifen vorne rechts", "detail", "optimized", False, 1, 1, True),
    ("wheel-rear-left", "Reifen hinten links", "detail", "optimized", False, 1, 1, True),
    ("wheel-rear-right", "Reifen hinten rechts", "detail", "optimized", False, 1, 1, True),
    ("panoramic-roof", "Panorama-Schiebedach", "interior", "optimized", False, 1, 1, True),
    ("windshield", "Windschutzscheibe", "detail", "original", False, 1, 1, True),
    ("tire-tread", "Reifenprofil", "detail", "original", True, 1, 4, False),
    ("odometer", "Kilometerstand", "detail", "original", False, 1, 1, True),
    ("key", "Schlüssel", "detail", "original", False, 1, 1, True),
    ("damage", "Schaden", "detail", "original", True, 1, 20, False),
    ("special", "Spezialaufnahme", "special", "original", True, 1, 50, False),
]

LEGACY_KEYS = {
    "interior": "front-interior",
    "dashboard": "instruments",
    "interior-left": "driver-entry",
    "interior-right": "passenger-entry",
    "rear-seat-left": "rear-row-driver",
    "rear-seat-right": "rear-row-passenger",
    "trunk": "trunk-open",
}


def _new_id(key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"showroomflow-orientation:{key}")


def upgrade() -> None:
    op.add_column(
        "orientations",
        sa.Column("processing_mode", sa.String(32), nullable=False, server_default="original"),
    )
    op.add_column(
        "orientations",
        sa.Column("is_repeatable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "orientations",
        sa.Column("default_instance_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "orientations", sa.Column("max_instances", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("orientations", sa.Column("silhouette_object_key", sa.String(500)))
    op.add_column("orientations", sa.Column("silhouette_content_type", sa.String(100)))
    op.add_column(
        "capture_steps",
        sa.Column("orientation_instance_index", sa.Integer(), nullable=False, server_default="1"),
    )

    connection = op.get_bind()
    existing = {
        row.key: row.id for row in connection.execute(sa.text("SELECT id, key FROM orientations"))
    }
    now = datetime.now(timezone.utc)
    for position, (
        key,
        name,
        category,
        mode,
        repeatable,
        default_count,
        max_count,
        required,
    ) in enumerate(CATALOG, start=1):
        legacy_key = next((old for old, new in LEGACY_KEYS.items() if new == key), key)
        orientation_id = existing.get(legacy_key)
        if orientation_id is not None:
            orientation_id = uuid.UUID(str(orientation_id))
        processing = mode == "optimized"
        if orientation_id is None:
            orientation_id = _new_id(key)
            connection.execute(
                sa.text(
                    "INSERT INTO orientations "
                    "(id, key, name, instruction, category, default_capture_order, "
                    "default_export_order, is_required, requires_processing, processing_mode, "
                    "is_repeatable, default_instance_count, max_instances, is_active, created_at, updated_at) "
                    "VALUES (:id, :key, :name, '', :category, :position, :position, :required, "
                    ":processing, :mode, :repeatable, :default_count, :max_count, true, :now, :now)"
                ).bindparams(sa.bindparam("id", type_=sa.Uuid())),
                {
                    "id": orientation_id,
                    "key": key,
                    "name": name,
                    "category": category,
                    "position": position,
                    "required": required,
                    "processing": processing,
                    "mode": mode,
                    "repeatable": repeatable,
                    "default_count": default_count,
                    "max_count": max_count,
                    "now": now,
                },
            )
        else:
            connection.execute(
                sa.text(
                    "UPDATE orientations SET key=:key, name=:name, category=:category, "
                    "default_capture_order=:position, default_export_order=:position, "
                    "is_required=:required, requires_processing=:processing, processing_mode=:mode, "
                    "is_repeatable=:repeatable, default_instance_count=:default_count, "
                    "max_instances=:max_count, updated_at=:now WHERE id=:id"
                ).bindparams(sa.bindparam("id", type_=sa.Uuid())),
                {
                    "id": orientation_id,
                    "key": key,
                    "name": name,
                    "category": category,
                    "position": position,
                    "required": required,
                    "processing": processing,
                    "mode": mode,
                    "repeatable": repeatable,
                    "default_count": default_count,
                    "max_count": max_count,
                    "now": now,
                },
            )
            connection.execute(
                sa.text(
                    "UPDATE capture_steps SET category=:category WHERE orientation_id=:id"
                ).bindparams(sa.bindparam("id", type_=sa.Uuid())),
                {"id": orientation_id, "category": category},
            )
            if mode != "configurable":
                connection.execute(
                    sa.text(
                        "UPDATE capture_steps SET requires_processing=:processing "
                        "WHERE orientation_id=:id"
                    ).bindparams(sa.bindparam("id", type_=sa.Uuid())),
                    {"id": orientation_id, "processing": processing},
                )
            connection.execute(
                sa.text(
                    "UPDATE capture_steps AS target SET name=:name "
                    "WHERE target.orientation_id=:id AND NOT EXISTS ("
                    "SELECT 1 FROM capture_steps AS other "
                    "WHERE other.dealership_id=target.dealership_id "
                    "AND other.name=:name AND other.id<>target.id)"
                ).bindparams(sa.bindparam("id", type_=sa.Uuid())),
                {"id": orientation_id, "name": name},
            )


def downgrade() -> None:
    original_keys = {
        "front",
        "front-left",
        "left",
        "rear-left",
        "rear",
        "rear-right",
        "right",
        "front-right",
        "front-interior",
        "steering-wheel",
        "instruments",
        "driver-entry",
        "passenger-entry",
        "rear-row-driver",
        "rear-row-passenger",
        "trunk-open",
    }
    connection = op.get_bind()
    for key, *_ in CATALOG:
        if key not in original_keys:
            connection.execute(sa.text("DELETE FROM orientations WHERE key=:key"), {"key": key})
    op.drop_column("capture_steps", "orientation_instance_index")
    op.drop_column("orientations", "silhouette_content_type")
    op.drop_column("orientations", "silhouette_object_key")
    op.drop_column("orientations", "max_instances")
    op.drop_column("orientations", "default_instance_count")
    op.drop_column("orientations", "is_repeatable")
    op.drop_column("orientations", "processing_mode")
