"""
GET /api/v1/images/{user_id}/{filename} streams bytes out of the storage
backend rather than off local disk, so the same route serves a filesystem
deployment and an S3 one. These tests pin that contract against an in-memory
backend: any backend satisfying StorageBackend must produce these responses.
"""

import uuid
from collections.abc import AsyncIterator

import pytest

from app.api import images as images_api
from app.services.storage import ObjectNotFoundError, StorageBackend, validate_key
from app.utils.signed_urls import sign_image_url

IMAGE_BYTES = b"\xff\xd8\xff\xe0garment-jpeg-body" * 64


class InMemoryStorage(StorageBackend):
    """Minimal StorageBackend over a dict, to exercise the route without I/O."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)

    async def put(self, key: str, data: bytes) -> None:
        self.objects[validate_key(key)] = data

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[validate_key(key)]
        except KeyError as e:
            raise ObjectNotFoundError(key) from e

    async def open_stream(self, key: str, chunk_size: int = 8) -> AsyncIterator[bytes]:
        data = await self.get(key)

        async def _iter() -> AsyncIterator[bytes]:
            for start in range(0, len(data), chunk_size):
                yield data[start : start + chunk_size]

        return _iter()

    async def delete(self, key: str) -> None:
        self.objects.pop(validate_key(key), None)

    async def exists(self, key: str) -> bool:
        return validate_key(key) in self.objects

    async def copy(self, src_key: str, dst_key: str) -> None:
        self.objects[validate_key(dst_key)] = await self.get(src_key)


@pytest.fixture
def stored_image(test_user, monkeypatch) -> str:
    """Put one garment image in the backend the route reads from; return its key."""
    key = f"{test_user.id}/20260101_120000_abcd1234.jpg"
    backend = InMemoryStorage({key: IMAGE_BYTES})
    monkeypatch.setattr(images_api, "get_storage_backend", lambda: backend)
    return key


class TestImageServing:
    async def test_owner_gets_the_stored_bytes(self, client, auth_headers, stored_image):
        response = await client.get(f"/api/v1/images/{stored_image}", headers=auth_headers)

        assert response.status_code == 200
        assert response.content == IMAGE_BYTES
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "private, max-age=3600, must-revalidate"

    async def test_signed_url_serves_without_a_session(self, client, stored_image):
        response = await client.get(sign_image_url(stored_image))

        assert response.status_code == 200
        assert response.content == IMAGE_BYTES

    async def test_missing_object_is_404_not_500(self, client, auth_headers, test_user, stored_image):
        response = await client.get(
            f"/api/v1/images/{test_user.id}/20260101_120000_deadbeef.jpg",
            headers=auth_headers,
        )

        assert response.status_code == 404

    async def test_another_users_image_is_denied(self, client, auth_headers, stored_image):
        response = await client.get(
            f"/api/v1/images/{uuid.uuid4()}/20260101_120000_abcd1234.jpg",
            headers=auth_headers,
        )

        assert response.status_code == 401

    async def test_unauthenticated_is_denied(self, client, stored_image):
        response = await client.get(f"/api/v1/images/{stored_image}")

        assert response.status_code == 401

    @pytest.mark.parametrize(
        "filename",
        ["../../etc/passwd", "no-extension", "shell.sh", "sneaky.jpg.exe"],
    )
    async def test_filename_is_rejected_before_storage_is_touched(
        self, client, auth_headers, test_user, stored_image, filename
    ):
        response = await client.get(
            f"/api/v1/images/{test_user.id}/{filename}", headers=auth_headers
        )

        assert response.status_code in (400, 404)
        assert response.status_code != 200
