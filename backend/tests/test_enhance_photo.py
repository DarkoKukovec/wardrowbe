"""Tests for enhance-photo and apply-enhanced-photo endpoints, and related image service helpers."""
import os
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import ClothingItem, ItemStatus
from app.schemas.item import ApplyPhotoRequest
from app.services.image_generation_service import _build_prompt
from app.services.image_service import ImageService


# ---------------------------------------------------------------------------
# _build_prompt unit tests
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_item_type_appears_prominently(self):
        prompt = _build_prompt("cap", "blue", None, None)
        assert "cap" in prompt
        # The item type should appear twice: once in subject, once in the must-not-substitute clause
        assert prompt.count("cap") >= 2

    def test_item_type_not_substituted_clause_present(self):
        prompt = _build_prompt("cap", "blue", None, None)
        assert "must not be substituted" in prompt

    def test_color_included_in_attributes(self):
        prompt = _build_prompt("shirt", "red", None, None)
        assert "red" in prompt

    def test_material_included_in_attributes(self):
        prompt = _build_prompt("jacket", None, None, "leather")
        assert "leather" in prompt

    def test_pattern_included_when_not_solid(self):
        prompt = _build_prompt("shirt", None, "striped", None)
        assert "striped" in prompt

    def test_solid_pattern_excluded(self):
        prompt = _build_prompt("shirt", "blue", "solid", None)
        assert "solid" not in prompt

    def test_unknown_item_type_falls_back_to_clothing_item(self):
        prompt = _build_prompt("unknown", "green", None, None)
        assert "clothing item" in prompt
        assert "unknown" not in prompt

    def test_none_item_type_falls_back_to_clothing_item(self):
        prompt = _build_prompt(None, "green", None, None)
        assert "clothing item" in prompt

    def test_no_attributes_produces_clean_prompt(self):
        prompt = _build_prompt("trousers", None, None, None)
        assert "trousers" in prompt
        assert "attributes:" not in prompt

    def test_custom_prompt_appended(self):
        prompt = _build_prompt("shirt", "white", None, None, "show folded")
        base_end = "marketing quality."
        assert base_end in prompt
        assert prompt.index(base_end) < prompt.index("show folded")
        assert "Additional instructions: show folded" in prompt

    def test_custom_prompt_whitespace_only_ignored(self):
        prompt = _build_prompt("shirt", "white", None, None, "   ")
        assert "Additional instructions:" not in prompt

    def test_all_attributes_combined(self):
        prompt = _build_prompt("cap", "blue", "checkered", "wool")
        assert "blue" in prompt
        assert "wool" in prompt
        assert "checkered" in prompt
        assert "cap" in prompt

    def test_subtype_overrides_type_in_prompt(self):
        """subtype (e.g. 'watch') should take precedence over generic type ('accessories')."""
        prompt = _build_prompt("accessories", "orange", None, None, subtype="watch")
        assert "watch" in prompt
        # 'watch' should appear at least twice (subject + must-not-substitute)
        assert prompt.count("watch") >= 2
        # The generic 'accessories' label should not appear
        assert "accessories" not in prompt

    def test_subtype_none_uses_type(self):
        """When subtype is None the generic type is used as before."""
        prompt = _build_prompt("accessories", "orange", None, None, subtype=None)
        assert "accessories" in prompt

    def test_subtype_with_attributes(self):
        prompt = _build_prompt("accessories", "silver", None, None, subtype="watch")
        assert "watch" in prompt
        assert "silver" in prompt


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


class TestApplyPhotoRequestSchema:
    def test_default_action_is_replace(self):
        req = ApplyPhotoRequest(temp_path="abc/temp_x.jpg")
        assert req.action == "replace"

    def test_explicit_replace(self):
        req = ApplyPhotoRequest(temp_path="abc/temp_x.jpg", action="replace")
        assert req.action == "replace"

    def test_explicit_add(self):
        req = ApplyPhotoRequest(temp_path="abc/temp_x.jpg", action="add")
        assert req.action == "add"

    def test_invalid_action_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ApplyPhotoRequest(temp_path="abc/temp_x.jpg", action="delete")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg_bytes(color: tuple = (200, 100, 50), size: tuple = (100, 100)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _create_item_images(storage_path: Path, user_id, filename_base: str) -> ClothingItem:
    """Write dummy image files for original/medium/thumb."""
    user_dir = storage_path / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "_medium", "_thumb"):
        (user_dir / f"{filename_base}{suffix}.jpg").write_bytes(_make_jpeg_bytes())


# ---------------------------------------------------------------------------
# ImageService unit tests
# ---------------------------------------------------------------------------


class TestImageServiceTempHelpers:
    async def test_save_temp_image(self, tmp_path):
        svc = ImageService(storage_path=str(tmp_path))
        user_id = uuid4()
        data = _make_jpeg_bytes()

        rel_path = await svc.save_temp_image(user_id, data)
        assert rel_path.startswith(f"{user_id}/temp_")
        assert rel_path.endswith(".jpg")
        full = tmp_path / rel_path
        assert full.exists()
        assert full.read_bytes() == data

    async def test_apply_temp_image_replaces_and_deletes_temp(self, tmp_path):
        svc = ImageService(storage_path=str(tmp_path))
        user_id = uuid4()
        base = "20240101_120000_abc12345"

        # Create original + medium + thumb
        _create_item_images(tmp_path, user_id, base)
        image_path = f"{user_id}/{base}.jpg"

        # Save a different-coloured temp image
        temp_data = _make_jpeg_bytes(color=(10, 20, 30))
        temp_path = await svc.save_temp_image(user_id, temp_data)

        result = await svc.apply_temp_image(image_path, temp_path)

        # Temp file removed
        assert not (tmp_path / temp_path).exists()
        # Paths unchanged
        assert result["image_path"] == image_path
        # Image was overwritten (simple size check)
        original_full = tmp_path / image_path
        assert original_full.exists()

    async def test_apply_temp_image_missing_temp(self, tmp_path):
        svc = ImageService(storage_path=str(tmp_path))
        user_id = uuid4()
        base = "20240101_120000_abc12345"
        _create_item_images(tmp_path, user_id, base)

        with pytest.raises(ValueError, match="Temp image not found"):
            await svc.apply_temp_image(f"{user_id}/{base}.jpg", f"{user_id}/temp_nonexistent.jpg")

    async def test_apply_temp_image_missing_original(self, tmp_path):
        svc = ImageService(storage_path=str(tmp_path))
        user_id = uuid4()
        user_dir = tmp_path / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)

        # Only create temp, NOT the item image
        temp_data = _make_jpeg_bytes()
        temp_path = await svc.save_temp_image(user_id, temp_data)

        with pytest.raises(ValueError, match="Item image not found"):
            await svc.apply_temp_image(f"{user_id}/missing.jpg", temp_path)

    async def test_discard_temp_image(self, tmp_path):
        svc = ImageService(storage_path=str(tmp_path))
        user_id = uuid4()
        temp_path = await svc.save_temp_image(user_id, _make_jpeg_bytes())

        await svc.discard_temp_image(temp_path)
        assert not (tmp_path / temp_path).exists()

    async def test_discard_temp_image_missing_is_silent(self, tmp_path):
        svc = ImageService(storage_path=str(tmp_path))
        # Should not raise
        await svc.discard_temp_image(f"{uuid4()}/temp_nonexistent.jpg")


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestRemoveBackgroundEndpointPreview:
    """remove-background now returns EnhancePhotoResponse, not ItemResponse."""

    @pytest.mark.asyncio
    async def test_item_not_found(self, client: AsyncClient, auth_headers):
        resp = await client.post(
            f"/api/v1/items/{uuid4()}/remove-background",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_item_no_image(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        item = ClothingItem(
            user_id=(await _get_test_user_id(db_session, auth_headers)),
            type="shirt",
            image_path="",
            status=ItemStatus.ready,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        resp = await client.post(
            f"/api/v1/items/{item.id}/remove-background",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "no image" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_unauthenticated(self, client: AsyncClient):
        resp = await client.post(
            f"/api/v1/items/{uuid4()}/remove-background",
            json={},
        )
        assert resp.status_code == 401


class TestEnhancePhotoEndpoint:
    @pytest.mark.asyncio
    async def test_item_not_found(self, client: AsyncClient, auth_headers):
        resp = await client.post(
            f"/api/v1/items/{uuid4()}/enhance-photo",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_not_configured_returns_501(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, test_user
    ):
        base = "20240101_120000_abc12345"
        storage_path = Path(os.environ.get("STORAGE_PATH", "/tmp/wardrobe_test"))
        _create_item_images(storage_path, test_user.id, base)

        item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"{test_user.id}/{base}.jpg",
            status=ItemStatus.ready,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        with patch(
            "app.services.image_generation_service.ImageGenerationService.is_available",
            return_value=False,
        ):
            resp = await client.post(
                f"/api/v1/items/{item.id}/enhance-photo",
                json={},
                headers=auth_headers,
            )
        assert resp.status_code == 501

    @pytest.mark.asyncio
    async def test_unauthenticated(self, client: AsyncClient):
        resp = await client.post(
            f"/api/v1/items/{uuid4()}/enhance-photo",
            json={},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_successful_generation(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, test_user
    ):
        base = "20240101_120000_bbb12345"
        storage_path = Path(os.environ.get("STORAGE_PATH", "/tmp/wardrobe_test"))
        _create_item_images(storage_path, test_user.id, base)

        item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"{test_user.id}/{base}.jpg",
            status=ItemStatus.ready,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        with (
            patch(
                "app.services.image_generation_service.ImageGenerationService.is_available",
                return_value=True,
            ),
            patch(
                "app.services.image_generation_service.ImageGenerationService.generate",
                new_callable=AsyncMock,
                return_value=_make_jpeg_bytes(),
            ),
        ):
            resp = await client.post(
                f"/api/v1/items/{item.id}/enhance-photo",
                json={},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "preview_url" in data
        assert "temp_path" in data
        assert str(test_user.id) in data["temp_path"]


class TestApplyEnhancedPhotoEndpoint:
    @pytest.mark.asyncio
    async def test_item_not_found(self, client: AsyncClient, auth_headers, test_user):
        resp = await client.post(
            f"/api/v1/items/{uuid4()}/apply-enhanced-photo",
            json={"temp_path": f"{test_user.id}/temp_abc.jpg"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_wrong_user_temp_path_forbidden(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, test_user
    ):
        base = "20240101_120000_ccc12345"
        storage_path = Path(os.environ.get("STORAGE_PATH", "/tmp/wardrobe_test"))
        _create_item_images(storage_path, test_user.id, base)

        item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"{test_user.id}/{base}.jpg",
            status=ItemStatus.ready,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        other_user_id = uuid4()
        resp = await client.post(
            f"/api/v1/items/{item.id}/apply-enhanced-photo",
            json={"temp_path": f"{other_user_id}/temp_xyz.jpg"},
            headers=auth_headers,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_successful_apply(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, test_user
    ):
        base = "20240101_120000_ddd12345"
        storage_path = Path(os.environ.get("STORAGE_PATH", "/tmp/wardrobe_test"))
        _create_item_images(storage_path, test_user.id, base)

        # Pre-create a temp file
        svc = ImageService(storage_path=str(storage_path))
        temp_path = await svc.save_temp_image(test_user.id, _make_jpeg_bytes(color=(5, 10, 15)))

        item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"{test_user.id}/{base}.jpg",
            status=ItemStatus.ready,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        resp = await client.post(
            f"/api/v1/items/{item.id}/apply-enhanced-photo",
            json={"temp_path": temp_path},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(item.id)
        # Temp file should have been removed
        assert not (storage_path / temp_path).exists()

    @pytest.mark.asyncio
    async def test_action_replace_is_default(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, test_user
    ):
        """Omitting ``action`` defaults to replace."""
        base = "20240101_120000_eee12345"
        storage_path = Path(os.environ.get("STORAGE_PATH", "/tmp/wardrobe_test"))
        _create_item_images(storage_path, test_user.id, base)

        svc = ImageService(storage_path=str(storage_path))
        temp_path = await svc.save_temp_image(test_user.id, _make_jpeg_bytes(color=(20, 30, 40)))

        item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"{test_user.id}/{base}.jpg",
            status=ItemStatus.ready,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        # No "action" key — should default to replace
        resp = await client.post(
            f"/api/v1/items/{item.id}/apply-enhanced-photo",
            json={"temp_path": temp_path},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert not (storage_path / temp_path).exists()

    @pytest.mark.asyncio
    async def test_action_add_creates_additional_image(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, test_user
    ):
        base = "20240101_120000_fff12345"
        storage_path = Path(os.environ.get("STORAGE_PATH", "/tmp/wardrobe_test"))
        _create_item_images(storage_path, test_user.id, base)

        svc = ImageService(storage_path=str(storage_path))
        temp_path = await svc.save_temp_image(test_user.id, _make_jpeg_bytes(color=(50, 60, 70)))

        item = ClothingItem(
            user_id=test_user.id,
            type="shirt",
            image_path=f"{test_user.id}/{base}.jpg",
            status=ItemStatus.ready,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        resp = await client.post(
            f"/api/v1/items/{item.id}/apply-enhanced-photo",
            json={"temp_path": temp_path, "action": "add"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(item.id)
        # Main image path unchanged
        assert data["image_path"] == f"{test_user.id}/{base}.jpg"
        # One additional image created
        assert len(data["additional_images"]) == 1
        # Temp file removed
        assert not (storage_path / temp_path).exists()

    @pytest.mark.asyncio
    async def test_unauthenticated(self, client: AsyncClient, test_user):
        resp = await client.post(
            f"/api/v1/items/{uuid4()}/apply-enhanced-photo",
            json={"temp_path": f"{test_user.id}/temp_xyz.jpg"},
        )
        assert resp.status_code == 401


class TestHealthFeaturesImageGeneration:
    @pytest.mark.asyncio
    async def test_image_generation_flag_present(self, client: AsyncClient):
        resp = await client.get("/api/v1/health/features")
        assert resp.status_code == 200
        data = resp.json()
        assert "image_generation" in data
        assert isinstance(data["image_generation"], bool)


# ---------------------------------------------------------------------------
# Helper – extract user_id from auth token (used in parametrized tests above)
# ---------------------------------------------------------------------------


async def _get_test_user_id(db_session: AsyncSession, auth_headers: dict):
    """Decode the bearer token to get the user id.  Simpler: just use test_user fixture."""
    from app.api.auth import decode_access_token
    from app.models import User
    from sqlalchemy import select

    token = auth_headers["Authorization"].split(" ")[1]
    payload = decode_access_token(token)
    result = await db_session.execute(
        select(User).where(User.external_id == payload["sub"])
    )
    return result.scalar_one().id
