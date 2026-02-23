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
- input_images: Path(s) to input image(s) for editing (supports up to 14 images)

**Resolution:**
- resolution: "1k", "2k", or "4k" (default: 1k)

Example: {"prompt": "A cute cat", "resolution": "2k"}
Example: {"prompt": "Make it blue", "input_images": ["/path/to/image.png"]}
Example: {"prompt": "Combine these images", "input_images": ["/path/to/img1.png", "/path/to/img2.png"]}
"""
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate or how to modify"},
            "input_images": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Path(s) to input image(s) for editing (supports up to 14 images)"
            },
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
                from google.genai import types
                self._client = genai.Client(api_key=self.api_key)
            except ImportError:
                return None
        return self._client

    def _get_genai_config(self, resolution: str):
        """Get Google GenAI config for image generation."""
        try:
            from google.genai import types
            res_map = {
                "1k": "1K",
                "2k": "2K",
                "4k": "4K"
            }
            return types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(
                    image_size=res_map.get(resolution, "1K")
                )
            )
        except ImportError:
            return None

    async def execute(
        self,
        prompt: str,
        input_images: list[str] | None = None,
        resolution: str = "1k",
        **kwargs: Any
    ) -> str:
        if not self.api_key:
            return "Error: GEMINI_API_KEY or GOOGLE_API_KEY not configured"

        client = self._get_client()
        if client is None:
            return "Error: google-genai package not installed"

        try:
            from PIL import Image as PILImage

            # Handle backward compatibility: accept single input_image
            if input_images is None:
                input_images = kwargs.get("input_image")
                if input_images:
                    input_images = [input_images] if isinstance(input_images, str) else input_images

            # Validate input images
            if input_images and len(input_images) > 14:
                return f"Error: Too many input images ({len(input_images)}). Maximum is 14."

            # Build contents
            contents = []
            max_input_dim = 0

            # Load and upload input images if provided
            if input_images:
                for img_path_str in input_images:
                    img_path = Path(img_path_str).expanduser()
                    if not img_path.exists():
                        return f"Error: Image not found: {img_path_str}"

                    # Track largest dimension for auto-resolution
                    try:
                        img = PILImage.open(img_path)
                        width, height = img.size
                        max_input_dim = max(max_input_dim, width, height)
                    except Exception:
                        pass

                    # Upload image
                    uploaded = client.files.upload(file=img_path)
                    contents.append(uploaded)

                contents.append(prompt)
                img_count = len(input_images)
            else:
                contents = [prompt]

            # Auto-detect resolution from largest input if not explicitly set
            effective_resolution = resolution
            if max_input_dim > 0 and resolution == "1k":
                if max_input_dim >= 3000:
                    effective_resolution = "4k"
                elif max_input_dim >= 1500:
                    effective_resolution = "2k"

            # Generate image with resolution config
            config = self._get_genai_config(effective_resolution)
            if config:
                response = client.models.generate_content(
                    model="gemini-3-pro-image-preview",
                    contents=contents,
                    config=config
                )
            else:
                response = client.models.generate_content(
                    model="gemini-3-pro-image-preview",
                    contents=contents
                )

            lines = []
            lines.append(f"[IMAGE GENERATED: {prompt}]")
            if input_images:
                img_count = len(input_images)
                lines.append(f"[Edit mode: {img_count} image{'s' if img_count > 1 else ''}]")
            lines.append(f"[Resolution: {effective_resolution}]")

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
