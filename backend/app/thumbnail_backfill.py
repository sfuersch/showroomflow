from __future__ import annotations

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import PhotoAsset, PhotoProcessingVariant
from app.storage import ObjectStorage
from app.thumbnails import create_thumbnail, thumbnail_key


def _store_thumbnail(storage: ObjectStorage, source_key: str) -> str:
    target_key = thumbnail_key(source_key)
    storage.put_object(
        object_key=target_key,
        content=create_thumbnail(storage.get_object(object_key=source_key)),
        content_type="image/jpeg",
    )
    return target_key


def main() -> None:
    storage = ObjectStorage(get_settings())
    created = 0
    failed = 0
    with SessionLocal() as db:
        photos = list(
            db.scalars(select(PhotoAsset).where(PhotoAsset.uploaded_at.is_not(None)))
        )
        for photo in photos:
            try:
                if photo.original_thumbnail_object_key is None:
                    photo.original_thumbnail_object_key = _store_thumbnail(
                        storage, photo.original_object_key
                    )
                    created += 1
                if (
                    photo.processed_object_key
                    and photo.processed_thumbnail_object_key is None
                ):
                    photo.processed_thumbnail_object_key = _store_thumbnail(
                        storage, photo.processed_object_key
                    )
                    created += 1
                db.commit()
            except Exception as exc:
                db.rollback()
                failed += 1
                print(f"Foto {photo.id}: {exc}")

        variants = list(
            db.scalars(
                select(PhotoProcessingVariant).where(
                    PhotoProcessingVariant.object_key.is_not(None),
                    PhotoProcessingVariant.thumbnail_object_key.is_(None),
                )
            )
        )
        for variant in variants:
            try:
                if variant.object_key:
                    variant.thumbnail_object_key = _store_thumbnail(
                        storage, variant.object_key
                    )
                    created += 1
                    db.commit()
            except Exception as exc:
                db.rollback()
                failed += 1
                print(f"Variante {variant.id}: {exc}")
    print(f"Thumbnail-Backfill abgeschlossen: {created} erstellt, {failed} fehlgeschlagen.")


if __name__ == "__main__":
    main()
