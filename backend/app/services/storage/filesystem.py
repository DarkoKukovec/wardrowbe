"""Filesystem storage backend — the default, and the historical behaviour."""

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from app.services.storage.base import (
    CHUNK_SIZE,
    ObjectNotFoundError,
    StorageBackend,
    validate_key,
)


class FilesystemStorage(StorageBackend):
    """Stores objects as files under *root*, one file per key."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._resolved_root = self.root.resolve()

    def path_for(self, key: str) -> Path:
        """Resolve *key* to an absolute path inside the root, or raise."""
        path = self.root / validate_key(key)
        resolved = path.resolve()
        if not resolved.is_relative_to(self._resolved_root):
            raise ValueError(f"Invalid storage key (escapes storage root): {key!r}")
        return path

    async def put(self, key: str, data: bytes) -> None:
        path = self.path_for(key)
        await asyncio.to_thread(self._write, path, data)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        path = self.path_for(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as e:
            raise ObjectNotFoundError(key) from e

    async def open_stream(self, key: str, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        path = self.path_for(key)
        try:
            handle = await asyncio.to_thread(path.open, "rb")
        except FileNotFoundError as e:
            raise ObjectNotFoundError(key) from e

        async def _iterate() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await asyncio.to_thread(handle.read, chunk_size)
                    if not chunk:
                        return
                    yield chunk
            finally:
                handle.close()

        return _iterate()

    async def delete(self, key: str) -> None:
        path = self.path_for(key)
        await asyncio.to_thread(path.unlink, True)

    async def exists(self, key: str) -> bool:
        path = self.path_for(key)
        return await asyncio.to_thread(path.is_file)

    async def copy(self, src_key: str, dst_key: str) -> None:
        src = self.path_for(src_key)
        dst = self.path_for(dst_key)
        if not await asyncio.to_thread(src.is_file):
            raise ObjectNotFoundError(src_key)
        await asyncio.to_thread(self._copy, src, dst)

    @staticmethod
    def _copy(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
