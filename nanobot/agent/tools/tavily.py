"""Tavily AI Search tool - optimized for LLMs."""

import os
from typing import Any

from nanobot.agent.tools.base import Tool


class TavilySearchTool(Tool):
    """Search the web using Tavily AI Search API.

    Tavily is optimized for LLM consumption with AI-generated answer summaries,
    structured results, and domain filtering.
    """

    name = "tavily_search"
    description = """AI-optimized web search using Tavily.

**Features:**
- AI-generated answer summaries from search results
- Clean, structured results (title, URL, content, score)
- Domain filtering (include/exclude specific sources)
- Image search

**Parameters:**
- query: Search query
- depth: "basic" (fast) or "advanced" (comprehensive)
- topic: "general" (default) or "news" (last 7 days)
- max_results: Number of results (1-10)

Example: {"query": "Python best practices", "depth": "advanced"}
"""
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "description": "Search depth: basic (fast) or advanced (comprehensive)"
            },
            "topic": {
                "type": "string",
                "enum": ["general", "news"],
                "description": "Topic: general or news (last 7 days)"
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results (1-10)",
                "minimum": 1,
                "maximum": 10
            },
            "include_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of domains to include"
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of domains to exclude"
            },
            "include_images": {
                "type": "boolean",
                "description": "Include relevant images in results"
            },
            "include_raw_content": {
                "type": "boolean",
                "description": "Include raw HTML content of sources"
            }
        },
        "required": ["query"]
    }

    def __init__(self, api_key: str | None = None, max_results: int = 5):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.max_results = max_results

    async def execute(
        self,
        query: str,
        depth: str = "basic",
        topic: str = "general",
        max_results: int | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        include_images: bool = False,
        include_raw_content: bool = False,
        **kwargs: Any
    ) -> str:
        if not self.api_key:
            return "Error: TAVILY_API_KEY not configured. Get one at https://tavily.com"

        try:
            from tavily import TavilyClient
        except ImportError:
            return "Error: tavily-python package not installed. Run: pip install tavily-python"

        try:
            client = TavilyClient(api_key=self.api_key)

            n = max_results or self.max_results

            response = client.search(
                query=query,
                search_depth=depth,
                topic=topic,
                max_results=n,
                include_answer=True,
                include_raw_content=include_raw_content,
                include_images=include_images,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )

            lines = []
            lines.append(f"[TAVILY SEARCH: {query}]")
            lines.append(f"Depth: {depth}, Topic: {topic}, Results: {n}")
            lines.append("")

            # AI Answer
            if response.get("answer"):
                lines.append("=== AI ANSWER ===")
                lines.append(response["answer"])
                lines.append("")

            # Results
            results = response.get("results", [])
            if results:
                lines.append("=== RESULTS ===")
                for i, item in enumerate(results, 1):
                    title = item.get("title", "No title")
                    url = item.get("url", "N/A")
                    score = item.get("score", 0)
                    content = item.get("content", "")
                    if len(content) > 200:
                        content = content[:200] + "..."

                    lines.append(f"{i}. {title}")
                    lines.append(f"   URL: {url}")
                    lines.append(f"   Score: {score:.3f}")
                    if content:
                        lines.append(f"   {content}")
                    if include_raw_content and item.get("raw_content"):
                        raw = item.get("raw_content", "")
                        if len(raw) > 300:
                            raw = raw[:300] + "..."
                        lines.append(f"   [Raw Content] {raw}")
                    lines.append("")

            # Images
            if include_images and response.get("images"):
                images = response.get("images", [])
                lines.append("=== IMAGES ===")
                for img_url in images[:5]:
                    lines.append(f"   {img_url}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            return f"[ERROR] Tavily search failed: {str(e)}"
