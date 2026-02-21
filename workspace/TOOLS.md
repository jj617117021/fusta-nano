# Available Tools

This document describes the tools available to nanobot.

## File Operations

### read_file
Read the contents of a file.
```
read_file(path: str) -> str
```

### write_file
Write content to a file (creates parent directories if needed).
```
write_file(path: str, content: str) -> str
```

### edit_file
Edit a file by replacing specific text.
```
edit_file(path: str, old_text: str, new_text: str) -> str
```

### list_dir
List contents of a directory.
```
list_dir(path: str) -> str
```

## Shell Execution

### exec
Execute a shell command and return output.
```
exec(command: str, working_dir: str = None) -> str
```

**Safety Notes:**
- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- Optional `restrictToWorkspace` config to limit paths

## Web Access

### web_search
Search the web using Brave Search API.
```
web_search(query: str, count: int = 5) -> str
```

Returns search results with titles, URLs, and snippets. Requires `tools.web.search.apiKey` in config.

### web_fetch
Fetch and extract main content from a URL.
```
web_fetch(url: str, extractMode: str = "markdown", maxChars: int = 50000) -> str
```

**Notes:**
- Content is extracted using readability
- Supports markdown or plain text extraction
- Output is truncated at 50,000 characters by default

## Browser Automation

### browser
Control a headless browser for web automation. Use this tool to browse the web, take screenshots, and interact with web pages.
```
browser(action: str, url: str = "", path: str = "", selector: str = "", text: str = "") -> str
```

**Actions:**
- `navigate` or `open` - Open a URL (requires `url` parameter)
- `screenshot` - Take a screenshot (optional `path` to save, defaults to workspace)
- `click` - Click an element by CSS selector (requires `selector`)
- `type` - Type text into an element (requires `selector` and `text`)
- `press` - Press a keyboard key (requires `key`)
- `snapshot` or `aria` - Get accessibility tree of the page
- `evaluate` or `eval` - Execute JavaScript (requires `script`)
- `wait` - Wait for selector or URL (optional `selector`, `url`, `timeout`)
- `get_url` - Get current URL
- `get_title` - Get page title
- `status` - Check if browser is running
- `stop` or `close` - Close the browser

**Examples:**
```
browser(action="navigate", url="https://example.com")
browser(action="screenshot", path="~/Desktop/screenshot.png")
browser(action="click", selector="#submit-button")
browser(action="type", selector="input[name=q]", text="hello")
browser(action="snapshot")
```

## Image Understanding

### image
Analyze an image with the configured image model. Uses MiniMax VLM for image understanding.
```
image(arg: str) -> str
```

**Examples:**
```
image("path/to/image.png")
image("path/to/image.png What is in this image?")
```

**Notes:**
- Provide the path to an image file (PNG, JPEG, etc.)
- Optionally ask a specific question about the image
- Uses Gemini vision model to analyze the image
- The image is automatically resized and compressed for optimal processing

## Communication

### message
Send a message to the user (used internally).
```
message(content: str, channel: str = None, chat_id: str = None) -> str
```

## Background Tasks

### spawn
Spawn a subagent to handle a task in the background.
```
spawn(task: str, label: str = None) -> str
```

Use for complex or time-consuming tasks that can run independently. The subagent will complete the task and report back when done.

## Scheduled Reminders (Cron)

Use the `exec` tool to create scheduled reminders with `nanobot cron add`:

### Set a recurring reminder
```bash
# Every day at 9am
nanobot cron add --name "morning" --message "Good morning! ☀️" --cron "0 9 * * *"

# Every 2 hours
nanobot cron add --name "water" --message "Drink water! 💧" --every 7200
```

### Set a one-time reminder
```bash
# At a specific time (ISO format)
nanobot cron add --name "meeting" --message "Meeting starts now!" --at "2025-01-31T15:00:00"
```

### Manage reminders
```bash
nanobot cron list              # List all jobs
nanobot cron remove <job_id>   # Remove a job
```

## Heartbeat Task Management

The `HEARTBEAT.md` file in the workspace is checked every 30 minutes.
Use file operations to manage periodic tasks:

### Add a heartbeat task
```python
# Append a new task
edit_file(
    path="HEARTBEAT.md",
    old_text="## Example Tasks",
    new_text="- [ ] New periodic task here\n\n## Example Tasks"
)
```

### Remove a heartbeat task
```python
# Remove a specific task
edit_file(
    path="HEARTBEAT.md",
    old_text="- [ ] Task to remove\n",
    new_text=""
)
```

### Rewrite all tasks
```python
# Replace the entire file
write_file(
    path="HEARTBEAT.md",
    content="# Heartbeat Tasks\n\n- [ ] Task 1\n- [ ] Task 2\n"
)
```

---

## Adding Custom Tools

To add custom tools:
1. Create a class that extends `Tool` in `nanobot/agent/tools/`
2. Implement `name`, `description`, `parameters`, and `execute`
3. Register it in `AgentLoop._register_default_tools()`
