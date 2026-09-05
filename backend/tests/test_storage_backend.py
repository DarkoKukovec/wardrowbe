"""Tests for the pluggable storage backends and their configuration."""

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.services.storage import (
    FilesystemStorage,
    ObjectNotFoundError,
    create_storage_backend,
)
from app.services.storage.s3 import S3Storage

BASE_ENV = {
    "secret_key": "not-the-default-value",
    "debug": False,
}


# ---------------------------------------------------------------------------
# Filesystem backend
# ---------------------------------------------------------------------------


class TestFilesystemStorage:
    async def test_round_trip(self, tmp_path):
        storage = FilesystemStorage(tmp_path)
        key = "11111111-1111-1111-1111-111111111111/20240101_120000_abcd1234.jpg"

        assert await storage.exists(key) is False

        await storage.put(key, b"payload")
        assert await storage.exists(key) is True
        assert await storage.get(key) == b"payload"

        # The key is the on-disk path relative to the root, verbatim.
        assert (tmp_path / key).read_bytes() == b"payload"

        await storage.delete(key)
        assert await storage.exists(key) is False

    async def test_get_missing_raises_object_not_found(self, tmp_path):
        storage = FilesystemStorage(tmp_path)
        with pytest.raises(ObjectNotFoundError):
            await storage.get("user/missing.jpg")

    async def test_delete_missing_is_silent(self, tmp_path):
        storage = FilesystemStorage(tmp_path)
        await storage.delete("user/missing.jpg")

    async def test_open_stream_yields_whole_object(self, tmp_path):
        storage = FilesystemStorage(tmp_path)
        data = b"x" * 200_000
        await storage.put("user/big.jpg", data)

        chunks = [chunk async for chunk in await storage.open_stream("user/big.jpg")]

        assert len(chunks) > 1  # actually streamed, not one buffered blob
        assert b"".join(chunks) == data

    async def test_open_stream_missing_raises(self, tmp_path):
        storage = FilesystemStorage(tmp_path)
        with pytest.raises(ObjectNotFoundError):
            await storage.open_stream("user/missing.jpg")

    async def test_copy(self, tmp_path):
        storage = FilesystemStorage(tmp_path)
        await storage.put("user/a.jpg", b"data")
        await storage.copy("user/a.jpg", "user/b.jpg")
        assert await storage.get("user/b.jpg") == b"data"

    async def test_copy_missing_source_raises(self, tmp_path):
        storage = FilesystemStorage(tmp_path)
        with pytest.raises(ObjectNotFoundError):
            await storage.copy("user/missing.jpg", "user/b.jpg")

    @pytest.mark.parametrize(
        "key",
        [
            "../escape.jpg",
            "user/../../escape.jpg",
            "/absolute.jpg",
            "user//double.jpg",
            "user/./same.jpg",
            "",
        ],
    )
    async def test_traversing_keys_are_rejected(self, tmp_path, key):
        storage = FilesystemStorage(tmp_path)
        with pytest.raises(ValueError):
            await storage.put(key, b"data")
        with pytest.raises(ValueError):
            await storage.get(key)
        with pytest.raises(ValueError):
            await storage.exists(key)

    async def test_symlink_escape_is_rejected(self, tmp_path):
        """A key resolving outside the root raises rather than silently working."""
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.jpg").write_bytes(b"secret")

        storage = FilesystemStorage(root)
        (root / "link").symlink_to(outside)

        with pytest.raises(ValueError, match="escapes storage root"):
            await storage.get("link/secret.jpg")


# ---------------------------------------------------------------------------
# S3 backend, against a fake client (no network)
# ---------------------------------------------------------------------------


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk, self._pos = self._data[self._pos :], len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "FakeBody":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()


def _client_error(code: str, status: int, operation: str = "GetObject") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class FakeS3Client:
    """Records the exact kwargs each call receives."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.head_error: ClientError | None = None

    async def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    async def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        key = kwargs["Key"]
        if key not in self.objects:
            raise _client_error("NoSuchKey", 404)
        return {"Body": FakeBody(self.objects[key])}

    async def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        self.objects.pop(kwargs["Key"], None)
        return {}

    async def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if self.head_error is not None:
            raise self.head_error
        if kwargs["Key"] not in self.objects:
            raise _client_error("404", 404, "HeadObject")
        return {}

    async def copy_object(self, **kwargs):
        self.calls.append(("copy_object", kwargs))
        src = kwargs["CopySource"]["Key"]
        if src not in self.objects:
            raise _client_error("NoSuchKey", 404, "CopyObject")
        self.objects[kwargs["Key"]] = self.objects[src]
        return {}


def make_s3_storage(**overrides) -> tuple[S3Storage, FakeS3Client]:
    params = {
        "endpoint_url": "https://silo.example.com",
        "bucket": "wardrowbe",
        "region": "us-east-1",
        "access_key": "key",
        "secret_key": "secret",
        "force_path_style": True,
    }
    params.update(overrides)
    storage = S3Storage(**params)
    fake = FakeS3Client()
    storage._client = fake
    return storage, fake


class TestS3Storage:
    async def test_keys_pass_through_unchanged(self):
        storage, fake = make_s3_storage()
        key = "11111111-1111-1111-1111-111111111111/20240101_120000_abcd1234_medium.jpg"

        await storage.put(key, b"payload")
        assert fake.calls[0] == ("put_object", {"Bucket": "wardrowbe", "Key": key, "Body": b"payload"})
        assert await storage.get(key) == b"payload"
        assert fake.calls[1][1]["Key"] == key

    async def test_get_missing_raises_object_not_found(self):
        storage, _ = make_s3_storage()
        with pytest.raises(ObjectNotFoundError):
            await storage.get("user/missing.jpg")

    async def test_exists_false_on_404(self):
        storage, _ = make_s3_storage()
        assert await storage.exists("user/missing.jpg") is False

    async def test_exists_true_when_present(self):
        storage, _ = make_s3_storage()
        await storage.put("user/a.jpg", b"data")
        assert await storage.exists("user/a.jpg") is True

    async def test_exists_reraises_non_404_client_error(self):
        storage, fake = make_s3_storage()
        fake.head_error = _client_error("AccessDenied", 403, "HeadObject")

        with pytest.raises(ClientError) as excinfo:
            await storage.exists("user/a.jpg")
        assert excinfo.value.response["Error"]["Code"] == "AccessDenied"

    async def test_open_stream_chunks_and_closes(self):
        storage, _ = make_s3_storage()
        data = b"y" * 150_000
        await storage.put("user/big.jpg", data)

        chunks = [chunk async for chunk in await storage.open_stream("user/big.jpg", 32 * 1024)]

        assert len(chunks) > 1
        assert b"".join(chunks) == data

    async def test_open_stream_missing_raises_before_body(self):
        storage, _ = make_s3_storage()
        with pytest.raises(ObjectNotFoundError):
            await storage.open_stream("user/missing.jpg")

    async def test_copy_missing_source_raises(self):
        storage, _ = make_s3_storage()
        with pytest.raises(ObjectNotFoundError):
            await storage.copy("user/missing.jpg", "user/b.jpg")

    async def test_traversing_key_rejected(self):
        storage, fake = make_s3_storage()
        with pytest.raises(ValueError):
            await storage.put("../escape.jpg", b"data")
        with pytest.raises(ValueError):
            await storage.get("/absolute.jpg")
        assert fake.calls == []

    def test_force_path_style_config(self):
        storage, _ = make_s3_storage()
        config = storage._client_kwargs()["config"]
        assert config.s3["addressing_style"] == "path"
        assert storage._client_kwargs()["endpoint_url"] == "https://silo.example.com"
        assert storage._client_kwargs()["region_name"] == "us-east-1"

    def test_virtual_host_style_when_disabled(self):
        storage, _ = make_s3_storage(force_path_style=False)
        assert storage._client_kwargs()["config"].s3["addressing_style"] == "auto"


# ---------------------------------------------------------------------------
# Factory and configuration validation
# ---------------------------------------------------------------------------


class TestStorageFactory:
    def test_filesystem_is_default(self, tmp_path):
        settings = Settings(storage_path=str(tmp_path), **BASE_ENV)
        assert settings.storage_backend == "filesystem"
        backend = create_storage_backend(settings)
        assert isinstance(backend, FilesystemStorage)
        assert backend.root == tmp_path

    def test_s3_selected(self, tmp_path):
        settings = Settings(
            storage_backend="s3",
            storage_path=str(tmp_path),
            s3_endpoint="https://silo.example.com",
            s3_bucket="wardrowbe",
            s3_access_key="key",
            s3_secret_key="secret",
            **BASE_ENV,
        )
        backend = create_storage_backend(settings)
        assert isinstance(backend, S3Storage)
        assert backend.bucket == "wardrowbe"

    def test_unknown_backend_raises(self, tmp_path):
        settings = Settings(storage_backend="gcs", storage_path=str(tmp_path), **BASE_ENV)
        with pytest.raises(ValueError, match="Unknown STORAGE_BACKEND"):
            create_storage_backend(settings)


class TestStorageConfigValidation:
    def test_filesystem_needs_nothing(self, tmp_path):
        Settings(storage_path=str(tmp_path), **BASE_ENV).validate_storage()

    @pytest.mark.parametrize(
        "missing", ["s3_endpoint", "s3_bucket", "s3_access_key", "s3_secret_key"]
    )
    def test_missing_required_var_raises(self, tmp_path, missing):
        params = {
            "storage_backend": "s3",
            "storage_path": str(tmp_path),
            "s3_endpoint": "https://silo.example.com",
            "s3_bucket": "wardrowbe",
            "s3_access_key": "key",
            "s3_secret_key": "secret",
        }
        params[missing] = ""

        settings = Settings(**params, **BASE_ENV)
        with pytest.raises(RuntimeError, match=missing.upper()):
            settings.validate_storage()

        # The factory must not degrade to local disk either.
        with pytest.raises(RuntimeError):
            create_storage_backend(settings)

    def test_unknown_backend_raises(self, tmp_path):
        settings = Settings(storage_backend="gcs", storage_path=str(tmp_path), **BASE_ENV)
        with pytest.raises(RuntimeError, match="Unknown STORAGE_BACKEND"):
            settings.validate_storage()

    def test_validate_security_covers_storage(self, tmp_path):
        """Startup validation must reject a half-configured S3 backend."""
        settings = Settings(
            storage_backend="s3",
            storage_path=str(tmp_path),
            s3_endpoint="https://silo.example.com",
            s3_bucket="wardrowbe",
            s3_access_key="",
            s3_secret_key="secret",
            **BASE_ENV,
        )
        with pytest.raises(RuntimeError, match="S3_ACCESS_KEY"):
            settings.validate_security()
