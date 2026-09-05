"""Storage backend interface for garment images.

Keys are ALWAYS relative, forward-slash paths (e.g. ``{user_id}/{filename}``)
and are identical across backends: the S3 object key of an image is byte-for-byte
the same string as its path relative to the filesystem storage root. That is what
makes a backend migration a plain ``mc mirror`` with no database rewrite.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

# Read size for streaming responses.
CHUNK_SIZE = 64 * 1024


class StorageError(Exception):
    """Base class for storage failures."""


class ObjectNotFoundError(StorageError):
    """Raised when a key does not exist in the backend."""

    def __init__(self, key: str):
        super().__init__(f"Object not found: {key}")
        self.key = key


def validate_key(key: str) -> str:
    """
    Validate a storage key and return it normalised.

    Keys must be relative, forward-slash separated and must not escape the
    storage root. A key that would escape raises — it is never silently
    resolved into something else.
    """
    if not key or not isinstance(key, str):
        raise ValueError(f"Invalid storage key: {key!r}")

    if "\\" in key:
        raise ValueError(f"Invalid storage key (backslash): {key!r}")

    if key.startswith("/"):
        raise ValueError(f"Invalid storage key (absolute): {key!r}")

    segments = key.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"Invalid storage key (empty or traversing segment): {key!r}")

    return key


class StorageBackend(ABC):
    """Blob storage for garment images, addressed by relative key."""

    @abstractmethod
    async def put(self, key: str, data: bytes) -> None:
        """Write *data* at *key*, replacing anything already there."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Read the whole object at *key*. Raises ObjectNotFoundError if absent."""

    @abstractmethod
    async def open_stream(self, key: str, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        """
        Open *key* and return an async iterator over its bytes.

        The object is opened eagerly so a missing key raises
        ObjectNotFoundError before any response body has been started.
        """

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete *key*. Deleting a missing key is not an error."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Whether *key* currently holds an object."""

    @abstractmethod
    async def copy(self, src_key: str, dst_key: str) -> None:
        """Copy *src_key* to *dst_key*. Raises ObjectNotFoundError if the source is absent."""

    async def close(self) -> None:
        """Release any held resources. Backends without state need not override."""
