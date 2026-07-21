"""Add orientation-specific interior and opening background modes."""

from alembic import op
import sqlalchemy as sa


revision = "0024_interior_background_modes"
down_revision = "0023_window_manual_review"
branch_labels = None
depends_on = None


WINDOW_KEYS = (
    "steering-wheel",
    "front-interior",
    "rear-row-driver",
    "rear-row-passenger",
)
OPENING_KEYS = (
    "driver-door",
    "passenger-door",
    "driver-entry",
    "passenger-entry",
    "driver-door-open",
    "passenger-door-open",
    "trunk-open",
)


def _quoted(keys: tuple[str, ...]) -> str:
    return ", ".join(f"'{key}'" for key in keys)


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE orientations SET processing_mode = 'window_background', "
            "requires_processing = true "
            f"WHERE key IN ({_quoted(WINDOW_KEYS)})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE orientations SET processing_mode = 'opening_background', "
            "requires_processing = true "
            f"WHERE key IN ({_quoted(OPENING_KEYS)})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE capture_steps SET requires_processing = true "
            "WHERE orientation_id IN (SELECT id FROM orientations "
            f"WHERE key IN ({_quoted(WINDOW_KEYS + OPENING_KEYS)}))"
        )
    )


def downgrade() -> None:
    original_keys = (
        "driver-door",
        "passenger-door",
        "rear-row-driver",
    )
    configurable_keys = ("rear-row-passenger", "trunk-open")
    optimized_keys = (
        "driver-entry",
        "passenger-entry",
        "driver-door-open",
        "passenger-door-open",
        "front-interior",
    )
    op.execute(
        sa.text(
            "UPDATE orientations SET processing_mode = 'original', "
            "requires_processing = false "
            f"WHERE key IN ({_quoted(original_keys)})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE orientations SET processing_mode = 'configurable' "
            f"WHERE key IN ({_quoted(configurable_keys)})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE orientations SET processing_mode = 'optimized', "
            "requires_processing = true "
            f"WHERE key IN ({_quoted(optimized_keys)})"
        )
    )
