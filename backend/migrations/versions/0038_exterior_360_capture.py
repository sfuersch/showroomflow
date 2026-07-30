"""Add guided 360 degree exterior capture orientation.

Revision ID: 0038_exterior_360_capture
Revises: 0037_editable_openai_mask_prompt
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa


revision = "0038_exterior_360_capture"
down_revision = "0037_editable_openai_mask_prompt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    orientation_id = uuid.uuid4()
    next_default_order = (
        connection.execute(sa.text("SELECT COALESCE(MAX(default_capture_order), 0) + 1 FROM orientations"))
        .scalar_one()
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO orientations (
                id, key, name, instruction, category,
                default_capture_order, default_export_order,
                is_required, requires_processing, processing_mode,
                is_repeatable, default_instance_count, max_instances,
                is_active, created_at, updated_at
            )
            VALUES (
                :id, 'exterior-360', '360° Außenaufnahme',
                'Zwölf Außenaufnahmen auf dem angezeigten Kreis aufnehmen. '
                'Die App führt zu Abstand, Winkel und Kamerahaltung.',
                'exterior', :default_order, :default_order,
                false, true, 'exterior_360',
                true, 12, 12, true, NOW(), NOW()
            )
            """
        ),
        {"id": orientation_id, "default_order": next_default_order},
    )

    dealerships = connection.execute(sa.text("SELECT id FROM dealerships")).scalars().all()
    for dealership_id in dealerships:
        capture_order = connection.execute(
            sa.text(
                "SELECT COALESCE(MAX(capture_order), 0) FROM capture_steps "
                "WHERE dealership_id = :dealership_id"
            ),
            {"dealership_id": dealership_id},
        ).scalar_one()
        export_order = connection.execute(
            sa.text(
                "SELECT COALESCE(MAX(export_order), 0) FROM capture_steps "
                "WHERE dealership_id = :dealership_id"
            ),
            {"dealership_id": dealership_id},
        ).scalar_one()
        for instance_index in range(1, 13):
            capture_order += 1
            export_order += 1
            connection.execute(
                sa.text(
                    """
                    INSERT INTO capture_steps (
                        id, dealership_id, orientation_id, orientation_instance_index,
                        name, instruction, category, capture_order, export_order,
                        is_required, requires_processing, is_active, created_at, updated_at
                    )
                    VALUES (
                        :id, :dealership_id, :orientation_id, :instance_index,
                        :name,
                        'Zum angezeigten Punkt gehen, Kamera auf die Fahrzeugmitte richten '
                        'und erst im grünen Toleranzbereich auslösen.',
                        'exterior', :capture_order, :export_order,
                        false, true, true, NOW(), NOW()
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "dealership_id": dealership_id,
                    "orientation_id": orientation_id,
                    "instance_index": instance_index,
                    "name": f"360° Außenaufnahme {instance_index}",
                    "capture_order": capture_order,
                    "export_order": export_order,
                },
            )


def downgrade() -> None:
    connection = op.get_bind()
    orientation_id = connection.execute(
        sa.text("SELECT id FROM orientations WHERE key = 'exterior-360'")
    ).scalar_one_or_none()
    if orientation_id is None:
        return
    connection.execute(
        sa.text("DELETE FROM capture_steps WHERE orientation_id = :orientation_id"),
        {"orientation_id": orientation_id},
    )
    connection.execute(
        sa.text("DELETE FROM orientations WHERE id = :orientation_id"),
        {"orientation_id": orientation_id},
    )
