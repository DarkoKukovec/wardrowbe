import asyncio
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

import imagehash
from PIL import Image

from app.config import get_settings
from app.services.storage import (
    FilesystemStorage,
    ObjectNotFoundError,
    StorageBackend,
    get_storage_backend,
)

settings = get_settings()

# Image size configurations
# Thumbnail: Used in cards/grids. 400px supports ~200px display on retina
# Medium: Used in detail views and outfit displays
# Original: Full resolution for zoom/download
SIZES = {
    "thumbnail": (400, 400),
    "medium": (800, 800),
    "original": (2400, 2400),
}

# JPEG quality per size. Highest for the original, lowest for the thumbnail.
QUALITY = {
    "original": 95,
    "medium": 90,
    "thumbnail": 88,
}

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}


class ImageService:
    """
    Garment image processing.

    All I/O goes through a StorageBackend and is addressed by relative key
    (``{user_id}/{filename}``) — exactly the value stored in the
    ``image_path`` / ``medium_path`` / ``thumbnail_path`` columns. Those keys
    are identical on the filesystem and in S3.
    """

    def __init__(
        self,
        storage_path: str | None = None,
        backend: StorageBackend | None = None,
    ):
        if backend is not None:
            self.backend = backend
        elif storage_path is not None:
            # Explicit filesystem root — used by tooling and tests.
            self.backend = FilesystemStorage(storage_path)
        else:
            self.backend = get_storage_backend()

    def _generate_filename(self, extension: str = ".jpg") -> str:
        """Generate a unique filename."""
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        unique_id = uuid.uuid4().hex[:8]
        return f"{timestamp}_{unique_id}{extension}"

    @staticmethod
    def variant_keys(image_key: str) -> dict[str, str]:
        """Derive the medium/thumbnail keys that accompany an original key."""
        base = image_key.rsplit(".", 1)[0]
        return {
            "original": image_key,
            "medium": f"{base}_medium.jpg",
            "thumbnail": f"{base}_thumb.jpg",
        }

    def _convert_heic(self, image_data: bytes) -> Image.Image:
        """Convert HEIC/HEIF to PIL Image."""
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError:
            pass

        return Image.open(BytesIO(image_data))

    def _open(self, image_data: bytes, original_filename: str = "") -> Image.Image:
        """Open image bytes, routing HEIC/HEIF through pillow-heif."""
        ext = Path(original_filename).suffix.lower()
        if ext in (".heic", ".heif"):
            return self._convert_heic(image_data)
        return Image.open(BytesIO(image_data))

    def _resize_image(
        self,
        image: Image.Image,
        max_size: tuple[int, int],
        quality: int = 92,
    ) -> bytes:
        """Resize image maintaining aspect ratio."""
        # Convert to RGB if necessary (handles RGBA, P mode, etc.)
        if image.mode in ("RGBA", "P", "LA"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode == "P":
                image = image.convert("RGBA")
            background.paste(image, mask=image.split()[-1] if image.mode == "RGBA" else None)
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        # Resize maintaining aspect ratio
        image.thumbnail(max_size, Image.Resampling.LANCZOS)

        # Save to bytes
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()

    def _render_variants(self, image: Image.Image) -> dict[str, bytes]:
        """Render original/medium/thumbnail JPEG bytes from a loaded image."""
        return {
            size_name: self._resize_image(image.copy(), max_size, quality=QUALITY[size_name])
            for size_name, max_size in SIZES.items()
        }

    async def _store_variants(self, keys: dict[str, str], variants: dict[str, bytes]) -> None:
        await asyncio.gather(
            *(self.backend.put(keys[size_name], data) for size_name, data in variants.items())
        )

    async def process_and_store(
        self,
        user_id: uuid.UUID,
        image_data: bytes,
        original_filename: str,
    ) -> dict[str, str]:
        """
        Process an uploaded image and store all sizes.

        Returns dict with paths for each size:
        {
            "original": "user_id/20240116_123456_abc123.jpg",
            "medium": "user_id/20240116_123456_abc123_medium.jpg",
            "thumbnail": "user_id/20240116_123456_abc123_thumb.jpg",
        }
        """
        # Validate file extension
        ext = Path(original_filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {ext}")

        image = self._open(image_data, original_filename)

        # Generate base filename
        base_filename = self._generate_filename(".jpg")
        keys = self.variant_keys(f"{user_id}/{base_filename}")

        variants = await asyncio.to_thread(self._render_variants, image)
        await self._store_variants(keys, variants)

        # Compute perceptual hash for duplicate detection
        image_hash = self.compute_phash(image_data, original_filename)

        return {
            "image_path": keys["original"],
            "medium_path": keys["medium"],
            "thumbnail_path": keys["thumbnail"],
            "image_hash": image_hash,
        }

    async def read_image(self, key: str, label: str = "Image") -> bytes:
        """Read the bytes stored at *key*. Raises ValueError if it is absent."""
        try:
            return await self.backend.get(key)
        except ObjectNotFoundError as e:
            raise ValueError(f"{label} not found: {key}") from e

    async def exists(self, key: str) -> bool:
        return await self.backend.exists(key)

    async def delete_images(self, paths: dict[str, str | None]) -> None:
        """Delete all image files for an item."""
        keys = [path for path in paths.values() if path]
        if keys:
            await asyncio.gather(*(self.backend.delete(key) for key in keys))

    def validate_image(self, image_data: bytes, content_type: str) -> bool:
        """Validate image data and content type."""
        # Check content type
        if content_type not in ALLOWED_MIME_TYPES:
            return False

        # Check file size (max 20MB)
        if len(image_data) > 20 * 1024 * 1024:
            return False

        # Try to open as image
        try:
            if content_type in ("image/heic", "image/heif"):
                self._convert_heic(image_data)
            else:
                Image.open(BytesIO(image_data))
            return True
        except Exception:
            return False

    def compute_phash(self, image_data: bytes, original_filename: str) -> str:
        """
        Compute perceptual hash (pHash) for an image.

        Returns a 16-character hex string representing the 64-bit hash.
        """
        image = self._open(image_data, original_filename)

        # Convert to RGB if needed for consistent hashing
        if image.mode != "RGB":
            image = image.convert("RGB")

        # Compute perceptual hash
        phash = imagehash.phash(image)
        return str(phash)

    @staticmethod
    def hash_distance(hash1: str, hash2: str) -> int:
        """
        Compute Hamming distance between two hashes.

        Lower distance = more similar images.
        Distance 0 = identical/near-identical images.
        Distance < 10 = very similar images.
        """
        h1 = imagehash.hex_to_hash(hash1)
        h2 = imagehash.hex_to_hash(hash2)
        return h1 - h2

    @staticmethod
    def is_duplicate(hash1: str, hash2: str, threshold: int = 8) -> bool:
        """
        Check if two images are duplicates based on hash distance.

        Default threshold of 8 catches near-identical images while allowing
        for minor differences in lighting/compression.
        """
        return ImageService.hash_distance(hash1, hash2) <= threshold

    def _composite_on_background(
        self,
        image_data: bytes,
        bg_color: tuple[int, int, int],
    ) -> Image.Image:
        """Cut the subject out of *image_data* and paste it on a solid colour."""
        from app.services.background_removal import get_provider

        image = Image.open(BytesIO(image_data)).convert("RGB")
        result = get_provider().remove(image)

        background = Image.new("RGBA", result.size, (*bg_color, 255))
        background.paste(result, mask=result.split()[3])
        return background.convert("RGB")

    async def remove_background(
        self,
        image_path: str,
        bg_color: tuple[int, int, int] = (255, 255, 255),
    ) -> dict[str, str]:
        """Replace an item's image with a background-removed version, in place."""
        keys = self.variant_keys(image_path)
        original = await self.read_image(image_path)

        def _render() -> dict[str, bytes]:
            final = self._composite_on_background(original, bg_color)
            return self._render_variants(final)

        variants = await asyncio.to_thread(_render)
        await self._store_variants(keys, variants)

        return {
            "image_path": keys["original"],
            "medium_path": keys["medium"],
            "thumbnail_path": keys["thumbnail"],
        }

    async def render_background_removed(
        self,
        image_path: str,
        bg_color: tuple[int, int, int] = (255, 255, 255),
    ) -> bytes:
        """
        Background-remove an item's image and return the JPEG bytes.

        Nothing is stored — the caller decides whether the result becomes a
        preview or replaces the original.
        """
        original = await self.read_image(image_path)

        def _render() -> bytes:
            final = self._composite_on_background(original, bg_color)
            buf = BytesIO()
            final.save(buf, format="JPEG", quality=95, optimize=True)
            return buf.getvalue()

        return await asyncio.to_thread(_render)

    async def rotate_image(self, image_path: str, direction: str = "cw") -> dict[str, str]:
        """
        Rotate an image and regenerate all sizes.

        Args:
            image_path: Relative key of the original image (e.g., "user_id/filename.jpg")
            direction: "cw" for clockwise 90°, "ccw" for counter-clockwise 90°

        Returns:
            dict with updated paths (same as input since we overwrite)
        """
        keys = self.variant_keys(image_path)
        original = await self.read_image(image_path)

        # PIL rotates counter-clockwise by default
        angle = -90 if direction == "cw" else 90

        def _render() -> dict[str, bytes]:
            image = Image.open(BytesIO(original))

            # Convert to RGB if necessary
            if image.mode in ("RGBA", "P", "LA"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                if image.mode == "P":
                    image = image.convert("RGBA")
                background.paste(
                    image, mask=image.split()[-1] if image.mode == "RGBA" else None
                )
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            return self._render_variants(image.rotate(angle, expand=True))

        variants = await asyncio.to_thread(_render)
        await self._store_variants(keys, variants)

        return {
            "image_path": keys["original"],
            "medium_path": keys["medium"],
            "thumbnail_path": keys["thumbnail"],
        }

    async def save_temp_image(self, user_id: uuid.UUID, image_data: bytes) -> str:
        """
        Save raw image bytes to a temporary object in the user's storage prefix.

        Returns the relative key of the temp object (e.g. "user_id/temp_<uuid>.jpg").
        """
        key = f"{user_id}/temp_{uuid.uuid4().hex}.jpg"
        await self.backend.put(key, image_data)
        return key

    async def apply_temp_image(self, item_image_path: str, temp_path: str) -> dict[str, str]:
        """
        Replace an item's image files with the image stored at *temp_path*.

        Regenerates medium and thumbnail variants in-place (same keys as the
        original) and deletes the temp object afterwards.

        Returns a dict with the (unchanged) relative keys for original/medium/thumb.
        Raises ValueError if either object does not exist.
        """
        temp_data = await self.read_image(temp_path, label="Temp image")

        keys = self.variant_keys(item_image_path)
        if not await self.backend.exists(item_image_path):
            raise ValueError(f"Item image not found: {item_image_path}")

        def _render() -> dict[str, bytes]:
            return self._render_variants(Image.open(BytesIO(temp_data)).convert("RGB"))

        variants = await asyncio.to_thread(_render)
        await self._store_variants(keys, variants)
        await self.backend.delete(temp_path)

        return {
            "image_path": keys["original"],
            "medium_path": keys["medium"],
            "thumbnail_path": keys["thumbnail"],
        }

    async def discard_temp_image(self, temp_path: str) -> None:
        """Delete a temporary image if it still exists."""
        await self.backend.delete(temp_path)
