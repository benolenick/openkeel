# AGENTS.md — Project Instructions for Codex

## Hyphae — Long-term Memory (MANDATORY)

Hyphae is a fact-retrieval memory system running locally at **http://127.0.0.1:8100** with 40K+ facts about this project, past work, infrastructure, and decisions.

**You MUST use Hyphae before answering questions about past work or project history.**

### Search for context (recall)
```bash
curl -s -X POST http://127.0.0.1:8100/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "<search terms>", "top_k": 10}'
```

### Save new knowledge (remember)
```bash
curl -s -X POST http://127.0.0.1:8100/remember \
  -H "Content-Type: application/json" \
  -d '{"text": "<concise fact>", "source": "agent"}'
```

### Search across ALL projects (unscoped)
```bash
curl -s -X POST http://127.0.0.1:8100/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "<search terms>", "top_k": 10, "scope": {}}'
```

### When to use
- **At session start** — recall recent project context: `{"query": "recent work project status", "top_k": 10}`
- **Before answering questions about past work** — always search first, do NOT guess
- **When you discover something important** — save it immediately
- **When something is unfamiliar** — search with `"scope": {}` to check all projects

## Kanban Board — Task Tracker (http://127.0.0.1:8200)

```bash
# List all tasks
curl -s http://127.0.0.1:8200/api/tasks | python3 -m json.tool

# Move task to in_progress
curl -s -X POST http://127.0.0.1:8200/api/task/{id}/move \
  -H "Content-Type: application/json" -d '{"status":"in_progress"}'

# Move task to done
curl -s -X POST http://127.0.0.1:8200/api/task/{id}/move \
  -H "Content-Type: application/json" -d '{"status":"done"}'

# Create new task
curl -s -X POST http://127.0.0.1:8200/api/task \
  -H "Content-Type: application/json" \
  -d '{"title":"...","description":"...","status":"todo","priority":"medium","type":"task","board":"default"}'
```

## Chrome Browser Automation (Periscope Pattern)

Navigate Chrome via Playwright CDP:
```python
from playwright.async_api import async_playwright
async with async_playwright() as p:
    browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]
    await page.goto("https://example.com")
```

Launch Chrome: `google-chrome --remote-debugging-port=9222 --user-data-dir=~/chrome-cdp --no-first-run "about:blank" &`

### Keyboard-free React form filling (JS)
```javascript
var nativeSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
nativeSetter.call(element, 'new value');
element.dispatchEvent(new Event('input', {bubbles: true}));
element.dispatchEvent(new Event('change', {bubbles: true}));
```

## Infrastructure (LAN)
- **jagg**: 192.168.0.224 (this machine — GPU server, automations)
- **kagg**: 192.168.0.59 (secondary server)
- **zasz**: 192.168.0.197
- **kaloth**: 192.168.0.48 (Chemister, large models)

## Key Directories
- `~/Desktop/job_apply/` — Job application automation (LinkedIn, Greenhouse, Indeed, Monster, career boards)
- `~/openkeel/` — OpenKeel governance terminal
- `~/tools/` — Various automation tools
- `/mnt/nvme/` — Large storage (Chemister, NCMS trading system, etc.)
