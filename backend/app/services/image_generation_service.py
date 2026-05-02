import base64
import io
import logging

import httpx
from PIL import Image

from app.config import get_settings

logger = logging.getLogger(__name__)


def _build_prompt(item_type: str | None, color: str | None, pattern: str | None, material: str | None) -> str:
    """Build a descriptive marketing photo prompt from item attributes."""
    parts: list[str] = []
    if color:
        parts.append(color)
    if material:
        parts.append(material)
    if pattern and pattern != "solid":
        parts.append(pattern)
    if item_type and item_type != "unknown":
        parts.append(item_type)

    item_description = " ".join(parts) if parts else "clothing item"
    return (
        f"A {item_description}, presented as a professional e-commerce product photo: "
        "clean white background, no wrinkles, perfectly flat-laid or ghost-mannequin style, "
        "studio lighting, sharp focus, no people, no mannequin visible, marketing quality."
    )


class ImageGenerationService:
    """Service for AI-powered marketing photo generation."""

    def __init__(self):
        self.settings = get_settings()

    def is_available(self) -> bool:
        """Return True if image generation is configured."""
        return bool(self.settings.ai_base_url and self.settings.ai_api_key)

    def _get_headers(self) -> dict:
        headers: dict = {}
        if self.settings.ai_api_key:
            headers["Authorization"] = f"Bearer {self.settings.ai_api_key}"
        return headers

    async def generate(
        self,
        item_type: str | None = None,
        color: str | None = None,
        pattern: str | None = None,
        material: str | None = None,
    ) -> bytes:
        """
        Generate a marketing-style photo of a clothing item.

        Returns raw JPEG bytes of the generated image.
        Raises ValueError if image generation is not configured.
        Raises RuntimeError on API errors.
        """
        if not self.is_available():
            raise ValueError(
                "Image generation is not configured. "
                "Set AI_BASE_URL and AI_API_KEY to enable this feature."
            )

        prompt = _build_prompt(item_type, color, pattern, material)
        model = self.settings.ai_image_generation_model

        logger.info(f"Generating marketing photo with model={model}, prompt={prompt!r}")

        payload: dict = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        }

        base_url = self.settings.ai_base_url.rstrip("/")

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            try:
                response = await client.post(
                    f"{base_url}/images/generations",
                    headers=self._get_headers(),
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Image generation API error: {e} — {e.response.text[:500]}")
                raise RuntimeError(f"Image generation failed: {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error(f"Image generation request error: {e}")
                raise RuntimeError("Image generation request failed") from e

        data = response.json()
        b64_data: str = data["data"][0]["b64_json"]
        raw = base64.b64decode(b64_data)

        # Normalise to JPEG
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        return output.getvalue()
