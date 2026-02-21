"""Image understanding tool."""

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class ImageUnderstandTool(Tool):
    """Tool for understanding images using vision models."""

    name = "image"
    description = "Analyze an image with the configured image model. Provide a prompt and image path or URL."

    def __init__(self, context_builder):
        self.context = context_builder

    async def execute(self, arg: str) -> str:
        """
        Understand an image file.

        Args:
            arg: Image path or URL, optionally with a question.
                  Format: "path/to/image.png" or "path/to/image.png What is this?"
        """
        # Parse arg - could be just path, or path + question
        parts = arg.strip().rsplit(" ", 1)
        path = parts[0]
        question = parts[1] if len(parts) > 1 else "Describe this image in detail."

        # Check if it's a URL or file path
        if path.startswith("http://") or path.startswith("https://"):
            # For URLs, we'd need to download first
            return f"Error: URL not supported yet. Please use a local file path."

        image_path = Path(path).expanduser()
        if not image_path.is_file():
            return f"Error: File not found: {path}"

        # Use the context's describe_image method
        description = await self.context.describe_image(image_path)
        if description:
            return f"[Image Description]\n{description}"
        else:
            return "Error: Failed to understand image. Make sure vision model is configured."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "arg": {
                    "type": "string",
                    "description": "Question about the image, or image path/URL"
                }
            },
            "required": ["arg"]
        }
