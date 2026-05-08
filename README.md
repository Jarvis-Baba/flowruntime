# FlowRuntime

> A deterministic, self-healing workflow engine for browser automation.  
> **Automation that survives UI change.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/playwright-1.50%2B-green)](https://playwright.dev/)

---

## What is this?

**Not an AI agent. Not a chatbot wrapper. Not a Selenium replacement.**

FlowRuntime is a **state-driven execution runtime** that turns brittle Playwright scripts into self-healing JSON workflows. It executes deterministically, validates every step against real DOM state, and only invokes an LLM when something actually breaks.

```
JSON workflow → Control Runtime → DOM Execution → State Verification → ✓
                                              ↘ Failure → Self-heal → Replan
```

## Why

Playwright scripts break when the UI changes. AI agents are slow and expensive.  
FlowRuntime splits the difference:

| | Playwright Script | AI Agent | FlowRuntime |
|---|---|---|---|
| Speed | Fast | Slow (LLM per step) | Fast (LLM only on failure) |
| UI resilience | Brittle | Tolerant | Self-healing |
| Token cost | $0 | $0.50+/run | ~$0.01/run |
| Deterministic | Yes | No | Yes (hot path) |
| Observable | Logs | Black box | Telemetry per step |

## Quick start

```bash
pip install playwright
python -m playwright install chromium

python examples/run_demo.py
```

## Architecture

```
                    ┌─────────────────┐
                    │  Workflow JSON   │  ← bootstrap / cold-start
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Control Runtime  │  sequence / loop / condition
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───┐  ┌──────▼──────┐  ┌────▼──────┐
     │  Executor   │  │  Verifier   │  │  Context   │
     │  (Playwright)│  │  (expect)   │  │  (collect) │
     └────────┬───┘  └──────┬──────┘  └────┬──────┘
              │              │              │
     ┌────────▼──────────────▼──────────────▼────┐
     │              DOM Snapshot                   │
     │    (Actionable Nodes + bbox + aria)        │
     └──────────────────────┬─────────────────────┘
                            │
                    ┌───────▼────────┐
                    │  ClaudePlanner  │  ← replan only
                    │  (LLM, on fail) │
                    └────────────────┘
```

### Three layers

**L0 — DOM Execution** (Playwright)  
Deterministic action execution. Navigate, click, fill, upload, type, eval.

**L1 — State Verification** (expect + soft-wait)  
Every step validates against real DOM. `text_contains`, `selector` visible, configurable timeout. No LLM needed.

**L2 — Self-Healing** (fuzzy match + replan)  
Selector broken? Fuzzy-matches against current DOM. Step failed? Captures full DOM snapshot + screenshot, replans remaining steps. Black-box telemetry records every failure for analysis.

### Control primitives

```json
// Sequence — fixed order
{"steps": [{"action": "navigate", "url": "..."}, {"action": "click", ...}]}

// Loop — repeat until DOM condition
{"type": "loop", "until": {"type": "data_increment", "data_selector": ".item", "stable_for": 2}, "max_iterations": 20}

// Condition — branch on page state
{"type": "condition", "condition": {"check": "text_contains", "value": "登录"}, "then": [...], "else": [...]}
```

## Real workflows

### Xianyu (闲鱼) publish listing

```json
[
  {"desc": "导航至发布页", "action": {"action": "navigate", "url": "https://pub.2.taobao.com/publish.htm"}, "expect": {"selector": ".upload-area", "timeout": 12000}},
  {"desc": "上传商品图片", "action": {"action": "upload", "selector": "input[type='file']", "files": ["{{image_path}}"]}, "expect": {"selector": ".image-preview-item"}, "collect": {"image_count": "text@.image-preview-item"}},
  {"desc": "填写标题", "action": {"action": "fill", "selector": "input[name='title']", "value": "{{title}}"}},
  {"desc": "填写价格", "action": {"action": "fill", "selector": "input[name='price']", "value": "{{price}}"}},
  {"desc": "点击发布", "action": {"action": "click", "selector": "button:has-text('确认发布')"}, "expect": {"text_contains": "发布成功", "timeout": 10000}}
]
```

### Cross-step variable passing

```json
{"collect": {"image_id": "attr:data-id@.image-preview-item"}}
// Later steps reference {{image_id}}
```

## Use cases

- **Marketplace automation** — Xianyu, eBay, Shopify listing management
- **Data collection** — infinite scroll, pagination, expand/collapse scraping
- **Resilient bots** — UI-change-tolerant automation pipelines
- **Form automation** — multi-page forms with state-dependent fields

## Project structure

```
flowruntime/
├── unified_executor.py    # DOM execution engine + self-healing
├── task_planner.py         # Control runtime + workflow engine
├── workflows/              # JSON workflow templates
│   ├── xianyu_publish.json
│   └── modelscope_browse.json
├── examples/
│   └── run_demo.py         # One-command demo
├── failures/               # Replan telemetry (for analysis)
└── executor_state/         # Failure snapshots (screenshot + DOM)
```

## Requirements

- Python 3.10+
- Playwright (`pip install playwright && python -m playwright install chromium`)
- No API keys required (LLM is optional, only for replan)

## Status

**V3 — Production-ready prototype.**  
All control primitives pass. Self-healing verified against real DOM mutations.  
Next: semantic state layer (V4).

## License

MIT
