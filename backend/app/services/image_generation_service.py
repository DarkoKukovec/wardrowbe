import base64
import io
import logging

import httpx
from PIL import Image

from app.config import get_settings

logger = logging.getLogger(__name__)


def _build_prompt(
    item_type: str | None,
    color: str | None,
    pattern: str | None,
    material: str | None,
    custom_prompt: str | None = None,
    subtype: str | None = None,
) -> str:
    """Build a descriptive marketing photo prompt from item attributes."""
    canonical_type = item_type if item_type not in (None, "unknown") else "clothing item"
    # Use subtype as the specific item label when available (e.g. "watch" over "accessories")
    specific_type = subtype if subtype else canonical_type

    attribute_parts: list[str] = []
    if color:
        attribute_parts.append(color)
    if material:
        attribute_parts.append(material)
    if pattern and pattern != "solid":
        attribute_parts.append(pattern)

    if attribute_parts:
        attributes = ", ".join(attribute_parts)
        subject = f"a {specific_type} — attributes: {attributes}"
    else:
        subject = f"a {specific_type}"

    base = (
        f"Professional e-commerce product photo of exactly {subject}. "
        f"The item is a {specific_type} and must not be substituted with any other item type. "
        "Clean white background, no wrinkles, perfectly flat-laid or ghost-mannequin style, "
        "studio lighting, sharp focus, no people, no mannequin visible, marketing quality."
    )
    if custom_prompt and custom_prompt.strip():
        return f"{base} Additional instructions: {custom_prompt.strip()}"
    return base


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
        custom_prompt: str | None = None,
        subtype: str | None = None,
    ) -> bytes:
        """
        Generate a marketing-style photo of a clothing item.

        Args:
            item_type: Clothing type (e.g. "shirt").
            subtype: More specific item name (e.g. "watch"), used instead of item_type in the
                     prompt when provided to avoid generic substitutions.
            color: Primary colour.
            pattern: Pattern descriptor.
            material: Fabric/material.
            custom_prompt: Optional free-text instructions to steer the generation.

        Returns raw JPEG bytes of the generated image.
        Raises ValueError if image generation is not configured.
        Raises RuntimeError on API errors.
        """
        if not self.is_available():
            raise ValueError(
                "Image generation is not configured. "
                "Set AI_BASE_URL and AI_API_KEY to enable this feature."
            )

        prompt = _build_prompt(item_type, color, pattern, material, custom_prompt, subtype)
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
