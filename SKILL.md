# FlowRuntime Skill

State-driven browser workflow engine with self-healing execution.
Code: `~/cc-workspace/` | GitHub: https://github.com/Jarvis-Baba/flowruntime

## Execution Contract

When using FlowRuntime, you MUST follow this pipeline. Do NOT bypass it with raw Playwright.

1. **Convert goal → workflow** — use existing JSON template, or call `DeepSeekPlanner.cold_start(goal, snapshot)`
2. **Execute via WorkflowEngine only** — `engine.run(workflow_path)` or `ControlRuntime.run_sequence(steps)`
3. **Every step returns** `ActionResult` — check `status`, `error_type`, `fix_applied`
4. **On failure**: call `planner.replan(goal, history, failed_step, snapshot)` — replace only remaining steps, never full reset
5. **Stop when**: `report.status == "ok"` or replans exceed `max_replans`
6. **All replan events go to** `failures/` — never skip telemetry

## Quick Start

```python
import sys
sys.path.insert(0, '/home/jarvis/cc-workspace')

from unified_executor import UnifiedExecutor
from task_planner import WorkflowEngine
from session_store import SessionStore

# Option A: Template workflow
ue = UnifiedExecutor(headless=True)
engine = WorkflowEngine(ue)
report = engine.run("modelscope_browse.json")
print(report.status)  # "ok" | "partial" | "failed"

# Option B: Session-aware workflow
store = SessionStore()
session = store.load("zhihu")          # from Tianyan V2 legacy
ue = UnifiedExecutor(headless=True)
ue.inject_session(session["cookies"])  # inject before navigate
engine = WorkflowEngine(ue)
report = engine.run("xianyu_publish.json", vars={"title": "iPhone 15", "price": "5000"})

# Option C: Raw actions with self-healing
ue = UnifiedExecutor(headless=True)
r = ue.execute({"action": "navigate", "url": "https://modelscope.cn/models"})
r = ue.execute({"action": "click", "selector": "text=DeepSeek-V4"})
snap = ue.snapshot()  # ActionableNode[] for planner
ue.close()
```

## Modules

| Module | Purpose |
|--------|---------|
| `unified_executor.UnifiedExecutor` | Browser execution + self-healing + snapshot |
| `task_planner.WorkflowEngine` | Workflow runner (template or DeepSeek-planned) |
| `task_planner.ControlRuntime` | Low-level state machine (seq/loop/condition) |
| `session_store.SessionStore` | Encrypted cookie persistence (Tianyan V2 compat) |
| `deepseek_planner.DeepSeekPlanner` | LLM cold-start + replan (used auto when DEEPSEEK_API_KEY set) |

## Key Patterns

### Session injection (for logged-in workflows)
```python
store = SessionStore()
session = store.load("profile_name")
ue.inject_session(session["cookies"], session.get("localStorage"))
```

### Run workflow with variables
```python
engine.run("xianyu_publish.json", vars={
    "title": "二手 MacBook", "price": "8000",
    "description": "自用95新", "image_path": "/tmp/photo.jpg"
})
```

### Snapshot for planner consumption
```python
snap = ue.snapshot()
# => {"url": "...", "nodes": [ActionableNode], "node_count": N}
# Each node: tag, text, aria_label, role, bbox, selector_hint
```

### Condition + Loop in workflows
```json
{"type": "condition", "condition": {"check": "text_contains", "value": "登录"}, "then": [...], "else": [...]}
{"type": "loop", "action": {...}, "until": {"type": "data_increment", "data_selector": ".item", "stable_for": 2}, "max_iterations": 20}
```

## Dependencies
- `playwright` (browser automation)
- `pycryptodome` (session encryption, optional)
- `DEEPSEEK_API_KEY` env var (for LLM replan, optional)

## Files
```
~/cc-workspace/
├── unified_executor.py    # DOM engine
├── task_planner.py        # Control runtime
├── session_store.py       # Cookie persistence
├── deepseek_planner.py    # LLM planner
├── workflows/             # JSON templates
├── failures/              # Replan telemetry
└── executor_state/        # Failure screenshots+snapshots
```
