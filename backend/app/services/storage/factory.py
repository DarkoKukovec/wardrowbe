"""Selects the storage backend named by ``STORAGE_BACKEND``."""

from functools import lru_cache

from app.config import Settings, get_settings
from app.services.storage.base import StorageBackend
from app.services.storage.filesystem import FilesystemStorage

FILESYSTEM = "filesystem"
S3 = "s3"
SUPPORTED_BACKENDS = (FILESYSTEM, S3)


def create_storage_backend(settings: Settings) -> StorageBackend:
    """Build the backend described by *settings*. Raises on an unknown name."""
    name = (settings.storage_backend or "").strip().lower()

    if name == FILESYSTEM:
        return FilesystemStorage(settings.storage_path)

    if name == S3:
        # Fails loudly rather than degrading to local disk.
        settings.validate_storage()

        # Imported lazily so filesystem deployments do not pay for botocore.
        from app.services.storage.s3 import S3Storage

        return S3Storage(
            endpoint_url=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            force_path_style=settings.s3_force_path_style,
        )

    raise ValueError(
        f"Unknown STORAGE_BACKEND: {settings.storage_backend!r}. "
        f"Supported backends: {', '.join(SUPPORTED_BACKENDS)}."
    )


@lru_cache(maxsize=1)
def get_storage_backend() -> StorageBackend:
    """The process-wide storage backend."""
    return create_storage_backend(get_settings())


async def close_storage_backend() -> None:
    """Release the process-wide backend's resources (called on shutdown)."""
    if get_storage_backend.cache_info().currsize:
        await get_storage_backend().close()
        get_storage_backend.cache_clear()
