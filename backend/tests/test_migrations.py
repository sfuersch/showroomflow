import ast
from pathlib import Path


MIGRATIONS = Path(__file__).parents[1] / "migrations" / "versions"


def test_alembic_revision_identifiers_fit_version_column() -> None:
    revisions: list[str] = []

    for migration in MIGRATIONS.glob("*.py"):
        module = ast.parse(migration.read_text(), filename=str(migration))
        for statement in module.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "revision" for target in statement.targets):
                continue
            revision = ast.literal_eval(statement.value)
            assert isinstance(revision, str)
            assert len(revision) <= 32, (
                f"Alembic revision {revision!r} in {migration.name} exceeds VARCHAR(32)"
            )
            revisions.append(revision)

    assert len(revisions) == len(set(revisions))
