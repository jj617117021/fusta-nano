"""Image generation tool using Gemini API."""

import os
import time
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class ImageGenerateTool(Tool):
    """Generate images using Google's Gemini AI."""

    name = "generate_image"
    description = """Generate or edit images using Gemini AI.

**IMPORTANT: You MUST call this tool when the user asks to generate/create an image. Do NOT describe images textually or make up image URLs - you MUST use this tool to actually generate the image.**

**Text-to-image:**
- prompt: Text description of the image to generate

**Image-to-image (edit):**
- prompt: Description of how to modify the image
- input_image: Path to the input image to modify

**Resolution:**
- resolution: "1k", "2k", or "4k" (default: 1k)

Example: {"prompt": "A cute cat", "resolution": "2k"}
Example: {"prompt": "Make it blue", "input_image": "/path/to/image.png"}
"""
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate or how to modify"},
            "input_image": {"type": "string", "description": "Path to input image for image-to-image editing"},
            "resolution": {
                "type": "string",
                "enum": ["1k", "2k", "4k"],
                "description": "Resolution: 1k, 2k, or 4k"
            }
        },
        "required": ["prompt"]
    }

    def __init__(self, api_key: str | None = None, workspace: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            self.api_key = os.environ.get("GOOGLE_API_KEY", "")
        self.workspace = Path(workspace) if workspace else Path.home() / ".nanobot" / "workspace"
        self._client = None

    def _get_client(self):
        """Get or create Google GenAI client."""
        if self._client is None:
            try:
                import google.genai as genai
                self._client = genai.Client(api_key=self.api_key)
            except ImportError:
                return None
        return self._client

    async def execute(
        self,
        prompt: str,
        input_image: str | None = None,
        resolution: str = "1k",
        **kwargs: Any
    ) -> str:
        if not self.api_key:
            return "Error: GEMINI_API_KEY or GOOGLE_API_KEY not configured"

        client = self._get_client()
        if client is None:
            return "Error: google-genai package not installed"

        # Resolution to dimensions mapping
        res_map = {
            "1k": "1024x1024",
            "2k": "2048x2048",
            "4k": "4096x4096"
        }

        try:
            # Build contents
            contents = [prompt]

            if input_image:
                # Image-to-image: upload image and include in prompt
                img_path = Path(input_image).expanduser()
                if not img_path.exists():
                    return f"Error: Image not found: {input_image}"

                # Upload image
                uploaded = client.files.upload(file=img_path)
                contents = [uploaded, prompt]

            # Generate image
            response = client.models.generate_content(
                model="gemini-3-pro-image-preview",
                contents=contents
            )

            lines = []
            lines.append(f"[IMAGE GENERATED: {prompt}]")
            if input_image:
                lines.append(f"[Edit mode: {input_image}]")
            lines.append(f"[Resolution: {resolution}]")

            # Parse response
            if hasattr(response, 'candidates') and response.candidates:
                for candidate in response.candidates:
                    if hasattr(candidate, 'content') and candidate.content:
                        for part in candidate.content.parts:
                            if hasattr(part, 'inline_data') and part.inline_data:
                                img_data = part.inline_data.data
                                # Save to workspace/images directory
                                images_dir = self.workspace / "images"
                                images_dir.mkdir(parents=True, exist_ok=True)
                                img_path = images_dir / f"generated_{int(time.time())}.png"

                                # Use PIL to properly process and save the image
                                # This ensures correct format (converts JPEG data to proper PNG)
                                from io import BytesIO
                                from PIL import Image as PILImage

                                image = PILImage.open(BytesIO(img_data))

                                # Convert RGBA to RGB with white background if needed
                                if image.mode == 'RGBA':
                                    rgb_image = PILImage.new('RGB', image.size, (255, 255, 255))
                                    rgb_image.paste(image, mask=image.split()[3])
                                    rgb_image.save(str(img_path), 'PNG')
                                elif image.mode == 'RGB':
                                    image.save(str(img_path), 'PNG')
                                else:
                                    image.convert('RGB').save(str(img_path), 'PNG')
                                # Return special format to indicate image for Discord delivery
                                # Use IMAGE_MEDIA which is processed before LLM transformation
                                lines.append(f"\n[IMAGE_MEDIA:{img_path}]")
                                lines.append(f"[IMAGE_FILE:{img_path}]")
                                lines.append(f"[Saved to] {img_path}")
                            elif hasattr(part, 'text') and part.text:
                                lines.append(f"\n{part.text}")

            if len(lines) == 2:
                return "[ERROR] No image generated"

            return "\n".join(lines)

        except Exception as e:
            return f"[ERROR] Image generation failed: {str(e)}"
