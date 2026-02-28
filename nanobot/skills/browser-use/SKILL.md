---
name: browser-use
description: Browser automation using browser-use CLI with real Chrome. Use when: navigating websites, filling forms, clicking elements, taking screenshots, extracting data, or any web interaction requiring a real browser with your logged-in Chrome profile.
metadata: {"nanobot": {"always": true, "emoji": "🌐", "requires": {"bins": ["browser-use"]}}}
---

# Browser Automation

Automates browser interactions using browser-use CLI with your real Chrome profile (logged-in state preserved).

## Core Workflow (ALWAYS follow this)

1. **Open URL first**: `{"action": "open", "url": "https://..."}`
2. **Get page state**: `{"action": "state"}` - Returns clickable elements with indices (ALWAYS do this before clicking/typing)
3. **Interact using indices**: Use the index numbers from state to interact with elements

## Quick Reference

| Need | Action | Example |
|------|--------|---------|
| **Smart click (RECOMMENDED)** | smart_click + ref | `{"action": "smart_click", "ref": "e5"}` |
| **Smart input (RECOMMENDED)** | smart_input + ref + text | `{"action": "smart_input", "ref": "e3", "text": "hello"}` |
| Click button/link | click + index | `{"action": "click", "index": 5}` |
| **Select dropdown option (DO NOT use click)** | **select + index + option** | **`{"action": "select", "index": 2, "option": "Beijing"}`** |
| Type in input | input + index + text | `{"action": "input", "index": 3, "text": "hello"}` |
| Check/Uncheck checkbox | check/uncheck + index | `{"action": "check", "index": 0}` or `{"action": "uncheck", "index": 0}` |
| Hover element | hover + index | `{"action": "hover", "index": 1}` |
| Press keyboard | keys + keys | `{"action": "keys", "keys": "Enter"}` |
| Wait for element | wait + target + type | `{"action": "wait", "target": ".loading", "type": "selector"}` |
| Scroll | scroll + direction | `{"action": "scroll", "direction": "down"}` |
| Go back | back | `{"action": "back"}` |
| Screenshot | screenshot | `{"action": "screenshot"}` |
| Get page title | get + what: title | `{"action": "get", "what": "title"}` |
| Get element text | get + what: text + index | `{"action": "get", "what": "text", "index": 0}` |
| Get input value | get + what: value + index | `{"action": "get", "what": "value", "index": 3}` |
| Execute JS | eval + code | `{"action": "eval", "code": "document.cookie"}` |
| Close browser | close | `{"action": "close"}` |

## All Available Actions

**Basic:** `open`, `state`, `click`, `input`, `select`, `hover`, `keys`, `wait`, `screenshot`, `close`, `scroll`, `back`, `eval`, `get`, `check`, `uncheck`

**Advanced (Playwright-powered - MORE RELIABLE):**
- `smart_click` - Click with auto-retry (USE THIS for buttons/links)
- `smart_input` - Input with better targeting (USE THIS for text fields)
- `find` - Semantic locators by role/text/label

## Advanced Actions (Playwright-powered)

For complex elements, use these Playwright-powered actions:

```python
# Smart click - auto-retry if fails, scroll to element
{"action": "smart_click", "ref": "e5"}

# Smart input - better targeting for text fields
{"action": "smart_input", "ref": "e3", "text": "hello"}

# Find by semantic locators
{"action": "find", "strategy": "role", "value": "button", "action": "click", "name": "Submit"}
{"action": "find", "strategy": "text", "value": "Sign In", "action": "click"}
{"action": "find", "strategy": "label", "value": "Email", "action": "fill", "text": "user@test.com"}
```

**Note:** Use `e1`, `e2`, `e3` format for refs (not just numbers like `1`, `2`).

## Complex Form Elements

**For Combo Boxes / Autocomplete:**
- Use `smart_input` instead of basic `input`
- Or use `find` with label strategy

**For Radio Buttons:**
- Use `smart_click` instead of basic `click`

**For Checkboxes:**
- Use built-in `check` / `uncheck` actions
- Or use `smart_click`

## Checkbox & Complex Interactions

**Recommended: Use built-in `check` and `uncheck` actions** (like OpenClaw's agent-browser):

```python
# Check a checkbox by index
{"action": "check", "index": 0}

# Uncheck a checkbox by index
{"action": "uncheck", "index": 0}
```

These actions properly trigger the click event (not just set .checked property).

**IMPORTANT: Use .click() NOT just .checked = true**
Setting `.checked = true` doesn't trigger the events LinkedIn expects. Use `.click()` instead:

```python
# Check a checkbox by ID - USE CLICK
{"action": "eval", "code": "document.querySelector('#agree').click()"}

# Check by selector - USE CLICK
{"action": "eval", "code": "document.querySelectorAll('input[type=checkbox]')[0].click()"}

# Alternative: dispatch proper click event
{"action": "eval", "code": "document.querySelector('#agree').dispatchEvent(new MouseEvent('click', {bubbles: true}))"}

# Uncheck
{"action": "eval", "code": "document.querySelector('#checkbox').click()"}
```

## Data Extraction

Use `get` to extract page/element data:

```python
# Page info
{"action": "get", "what": "title"}    # Page title
{"action": "get", "what": "html"}     # Full page HTML

# Element info (requires index from state)
{"action": "get", "what": "text", "index": 0}        # Element text content
{"action": "get", "what": "value", "index": 3}       # Input/textarea value
{"action": "get", "what": "attributes", "index": 2}  # All attributes
{"action": "get", "what": "bbox", "index": 4}         # Bounding box (x, y, width, height)
```

## Wait Conditions

```python
# Wait for element to appear
{"action": "wait", "target": ".button", "type": "selector"}

# Wait for text to appear
{"action": "wait", "target": "Success!", "type": "text"}

# Wait for element to disappear
{"action": "wait", "target": ".loading", "type": "selector"}
```

## Complete Workflow Example

```python
# 1. Open website
{"action": "open", "url": "https://example.com/login"}

# 2. Get clickable elements
{"action": "state"}
# Output shows: [0] Username input, [1] Password input, [2] Login button

# 3. Click username field and type
{"action": "input", "index": 0, "text": "myuser"}

# 4. Click password field and type
{"action": "input", "index": 1, "text": "mypass"}

# 5. Click login button
{"action": "click", "index": 2}

# 6. Wait for redirect
{"action": "wait", "target": ".dashboard", "type": "selector"}

# 7. Get page title to verify
{"action": "get", "what": "title"}
```

## Tips

1. **ALWAYS run `state` first** - See available elements and their indices before interacting
2. **Sessions persist** - Browser stays open between commands, preserving login state
3. **Element not found?** - Scroll down and run `state` again to refresh element list
4. **Checkbox problems?** - Use `eval` with `.click()` NOT `.checked = true` (must trigger actual click event!)
5. **For dropdowns: use `select` NEVER `click`** - The `select` action handles clicking the dropdown and selecting the option automatically. Do NOT try to click the dropdown first!

## Troubleshooting

**Browser won't start:**
- Run `browser-use close --all` to clean up stale sessions
- Try with `--headed` flag visible

**Element not found:**
- Run `state` to check current elements
- Element might be below fold - scroll first: `{"action": "scroll", "direction": "down"}`
- Run `state` again after scrolling

**Session issues:**
- Check with `browser-use sessions`
- Clean slate: `browser-use close --all`

## Cleanup

**Always close the browser when done:**
```python
{"action": "close"}
```
