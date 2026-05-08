#!/usr/bin/env python3
"""
Unified Executor V2 — DOM-first self-healing execution with Action Graph.

L0: DOM (Playwright)          — 唯一稳定执行通道
L1: Actionable DOM Snapshot   — 结构化可操作节点 + bounding box
L2: Claude                    — 诊断 + 生成修复策略（含历史记忆）
L3: Vision (Win-CUA)          — V3, 遮挡/歧义场景辅助诊断

Upgrades from V1:
  - DOM snapshot: raw text → structured ActionableNode[]
  - Bounding box capture for occlusion detection
  - Short-term memory (history_attempts) prevents repair loops

Usage:
    from unified_executor import UnifiedExecutor
    ue = UnifiedExecutor()
    result = ue.execute({"action": "click", "selector": "button.submit"})
"""

import json
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser

# ── Config ───────────────────────────────────────────
STATE_DIR = Path(__file__).parent / "executor_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TIMEOUT = 15000
RETRY_DELAY = 1.0
MAX_RETRIES = 2


# ── Data Structures ──────────────────────────────────

@dataclass
class ActionableNode:
    """A single interactive element extracted from the DOM."""
    tag: str
    text: str = ""
    aria_label: str = ""
    role: str = ""
    placeholder: str = ""
    name: str = ""              # id or name attribute
    href: str = ""
    visible: bool = True
    bbox: Optional[dict] = None  # {x, y, width, height}
    selector_hint: str = ""     # Suggested stable selector for this node

    def match_score(self, query: str) -> float:
        """Simple relevance score for fuzzy matching a query against this node."""
        q = query.lower().strip(" .#[]")
        # Strip Playwright selector prefixes (text=, css=, xpath=)
        for prefix in ("text=", "css=", "xpath="):
            if q.startswith(prefix):
                q = q[len(prefix):]
        # Strip quotes
        q = q.strip("'\"")
        if not q:
            return 0.0
        fields = [self.text, self.aria_label, self.placeholder, self.name, self.role]
        for f in fields:
            if q in f.lower():
                return 0.9
        # Partial word overlap
        q_words = set(q.split())
        all_text = " ".join(f for f in fields if f).lower()
        t_words = set(all_text.split())
        overlap = q_words & t_words
        if overlap:
            return 0.5 * len(overlap) / len(q_words)
        # Character-level fuzzy fallback (handles typos like "modelz"→"models")
        # Only for short queries to avoid false positives on random long strings
        if len(q) <= 10:
            q_chars = set(q)
            t_chars = set(all_text)
            if q_chars:
                char_overlap = len(q_chars & t_chars) / len(q_chars)
                if char_overlap > 0.75:
                    return 0.35 * char_overlap
        return 0.0


@dataclass
class ActionResult:
    action: dict
    status: str              # "ok" | "failed" | "fixed" | "escalated"
    duration_ms: float = 0
    retries: int = 0
    error: Optional[str] = None
    error_type: Optional[str] = None
    screenshot_path: Optional[str] = None
    dom_snapshot_path: Optional[str] = None
    fix_applied: Optional[str] = None
    result: Optional[dict] = None


# ── Core Executor ────────────────────────────────────

class UnifiedExecutor:
    """DOM-first executor with structured state capture and self-healing."""

    def __init__(self, headless: bool = True, timeout: int = DEFAULT_TIMEOUT):
        self.headless = headless
        self.timeout = timeout
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self._launch()

    def _launch(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.page = self.browser.new_page()
        self.page.set_default_timeout(self.timeout)

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    # ── Public API ────────────────────────────────

    def execute(self, action: dict) -> ActionResult:
        """Main entry. DOM-first with structured diagnosis and history-aware retry."""
        t0 = time.time()
        result = ActionResult(action=action, status="failed")
        history: list[dict] = []  # Short-term memory: tracks attempted fixes

        for attempt in range(MAX_RETRIES + 1):
            result.retries = attempt
            try:
                output = self._dom_execute(action)
                result.status = "ok"
                result.result = output
                result.duration_ms = (time.time() - t0) * 1000
                return result
            except Exception as e:
                if attempt < MAX_RETRIES:
                    diagnosis = self._capture_and_diagnose(action, e, attempt, history)
                    if diagnosis.get("fix") == "retry":
                        applied = self._apply_fix(diagnosis, action)
                        result.fix_applied = diagnosis.get("description")
                        # Record for short-term memory
                        history.append({
                            "attempt": attempt,
                            "error_type": diagnosis["error_type"],
                            "fix_applied": applied,
                            "description": diagnosis.get("description"),
                        })
                        time.sleep(RETRY_DELAY)
                        continue
                # Final failure
                result.status = "failed"
                result.error = str(e)
                result.error_type = self._classify_error(e)
                result.duration_ms = (time.time() - t0) * 1000
                if not self._last_screenshot:
                    self._capture_state(action, attempt)
                result.screenshot_path = self._last_screenshot
                result.dom_snapshot_path = self._last_snapshot
                return result

        result.status = "escalated"
        result.duration_ms = (time.time() - t0) * 1000
        return result

    def navigate(self, url: str) -> ActionResult:
        return self.execute({"action": "navigate", "url": url})

    def click(self, selector: str) -> ActionResult:
        return self.execute({"action": "click", "selector": selector})

    def fill(self, selector: str, value: str) -> ActionResult:
        return self.execute({"action": "fill", "selector": selector, "value": value})

    def get_text(self, selector: str = "body") -> str:
        return self.page.locator(selector).inner_text()

    def snapshot(self) -> dict:
        """Return ActionableNode[] for current page state. Entry point for Planner."""
        nodes = self._extract_actionable_nodes()
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "viewport": self.page.viewport_size,
            "nodes": [asdict(n) for n in nodes],
            "node_count": len(nodes),
        }

    # ── DOM Execution Layer (L0) ──────────────────

    def _dom_execute(self, action: dict) -> dict:
        act = action["action"]
        sel = action.get("selector", "")
        page = self.page

        if act == "navigate":
            page.goto(action["url"], wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # Some pages never reach networkidle, proceed anyway
            return {"url": page.url, "title": page.title()}

        elif act == "click":
            page.locator(sel).first.click()
            return {"clicked": sel}

        elif act == "fill":
            val = action.get("value", "")
            page.locator(sel).first.fill(val)
            return {"filled": sel, "value": val}

        elif act == "type":
            text = action.get("text", "")
            page.locator(sel).first.type(text)
            return {"typed": sel, "text": text}

        elif act == "upload":
            files = action.get("files", [])
            page.locator(sel).first.set_input_files(files)
            return {"uploaded": sel, "files": files}

        elif act == "wait":
            sec = float(action.get("seconds", 1))
            time.sleep(sec)
            return {"waited": sec}

        elif act == "eval":
            js = action.get("js", "")
            output = page.evaluate(js)
            return {"eval_result": output}

        else:
            raise ValueError(f"Unknown action: {act}")

    # ── State Capture Layer (L1) ──────────────────

    def _capture_state(self, action: dict, attempt: int) -> dict:
        """Capture screenshot + structured Actionable DOM snapshot."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tag = hashlib.md5(json.dumps(action).encode()).hexdigest()[:8]
        prefix = f"fail_{ts}_{tag}_r{attempt}"

        # Screenshot
        ss_path = str(STATE_DIR / f"{prefix}.png")
        self.page.screenshot(path=ss_path, full_page=False)
        self._last_screenshot = ss_path

        # Structured actionable DOM
        nodes = self._extract_actionable_nodes()
        dom_path = str(STATE_DIR / f"{prefix}.json")
        dom_data = {
            "url": self.page.url,
            "title": self.page.title(),
            "action": action,
            "attempt": attempt,
            "viewport": self.page.viewport_size,
            "nodes": [asdict(n) for n in nodes],
            "node_count": len(nodes),
            # Lightweight text preview for quick context
            "page_summary": self.page.locator("body").inner_text()[:500],
        }
        Path(dom_path).write_text(json.dumps(dom_data, ensure_ascii=False, indent=2))
        self._last_snapshot = dom_path

        return dom_data

    def _capture_and_diagnose(self, action: dict, error: Exception, attempt: int,
                              history: list[dict]) -> dict:
        """Capture state, then analyze with history awareness to generate fix."""
        dom_data = self._capture_state(action, attempt)
        error_type = self._classify_error(error)
        already_tried = [h.get("fix_applied", "") for h in history]

        diagnosis = {
            "error_type": error_type,
            "error": str(error),
            "attempt": attempt,
            "url": dom_data["url"],
            "title": dom_data["title"],
            "screenshot": self._last_screenshot,
            "dom_snapshot": self._last_snapshot,
            "node_count": dom_data["node_count"],
            "history": history,
            "already_tried": already_tried,
        }

        # Auto-fix strategies
        if error_type == "selector_not_found":
            diagnosis["fix"] = "retry"
            sel = action.get("selector", "")
            # Fuzzy match against structured nodes
            alternatives = self._fuzzy_match_structured(sel, dom_data["nodes"], already_tried)
            if alternatives:
                diagnosis["alternatives"] = alternatives
                diagnosis["description"] = f"substitute selector: {alternatives[0]}"
            else:
                diagnosis["fix"] = "escalate"
                diagnosis["description"] = "no matching element found"

        elif error_type == "timeout":
            diagnosis["fix"] = "retry"
            diagnosis["description"] = "retry with extended wait"

        elif error_type == "navigation":
            diagnosis["fix"] = "retry"
            diagnosis["description"] = "wait for page stabilization"

        else:
            diagnosis["fix"] = "escalate"
            diagnosis["description"] = f"unknown error: {str(error)[:100]}"

        return diagnosis

    def _apply_fix(self, diagnosis: dict, action: dict) -> Optional[str]:
        """Apply fix strategy. Returns the fix that was applied (for history)."""
        applied = None
        if diagnosis["fix"] == "retry":
            alt = diagnosis.get("alternatives")
            if alt and action.get("selector"):
                action["selector"] = alt[0]
                applied = alt[0]
        return applied

    # ── Action Graph Extraction ────────────────────

    def _extract_actionable_nodes(self, max_nodes: int = 80) -> list[ActionableNode]:
        """Extract structured interactive elements from the current page.

        Captures: buttons, links, inputs, selects, textareas
        Each node includes: tag, text, aria-label, role, bbox, selector_hint.
        """
        selectors = [
            "button",
            "a[href]",
            "input:not([type='hidden'])",
            "textarea",
            "select",
            "[role='button']",
            "[role='link']",
            "[role='tab']",
            "[role='menuitem']",
        ]
        nodes: list[ActionableNode] = []
        seen_texts: set[tuple[str, str]] = set()  # dedup by (tag, text)

        for sel in selectors:
            try:
                elements = self.page.locator(sel).all()
                for el in elements:
                    if len(nodes) >= max_nodes:
                        break
                    try:
                        if not el.is_visible():
                            continue
                        tag = el.evaluate("el => el.tagName.toLowerCase()")
                        text = (el.inner_text() or "").strip()[:120]
                        key = (tag, text)
                        if key in seen_texts:
                            continue
                        seen_texts.add(key)

                        aria_label = el.get_attribute("aria-label") or ""
                        role_attr = el.get_attribute("role") or ""
                        placeholder = el.get_attribute("placeholder") or ""
                        name_attr = (el.get_attribute("id") or el.get_attribute("name") or "")
                        href = el.get_attribute("href") or ""

                        # Bounding box for occlusion/alignment checks
                        bbox_raw = el.evaluate(
                            "el => JSON.stringify(el.getBoundingClientRect())"
                        )
                        bbox = json.loads(bbox_raw) if bbox_raw else None
                        if bbox:
                            bbox = {k: round(v, 1) for k, v in bbox.items()}

                        # Build stable selector hint
                        hint = _build_selector_hint(tag, text, aria_label, placeholder, name_attr)

                        nodes.append(ActionableNode(
                            tag=tag,
                            text=text,
                            aria_label=aria_label,
                            role=role_attr,
                            placeholder=placeholder,
                            name=name_attr,
                            href=href,
                            visible=True,
                            bbox=bbox,
                            selector_hint=hint,
                        ))
                    except Exception:
                        pass
                if len(nodes) >= max_nodes:
                    break
            except Exception:
                continue

        return nodes

    # ── Helpers ───────────────────────────────────

    def _classify_error(self, error: Exception) -> str:
        msg = str(error).lower()
        if "locator" in msg or "selector" in msg or "resolve" in msg:
            return "selector_not_found"
        if "timeout" in msg:
            return "timeout"
        if "navigation" in msg or "goto" in msg:
            return "navigation"
        return "other"

    def _fuzzy_match_structured(self, target: str, nodes: list[dict],
                                 exclude: list[str]) -> list[str]:
        """Score ActionableNodes against target, exclude already-tried selectors."""
        scored = []
        for n in nodes:
            node = ActionableNode(**n)
            score = node.match_score(target)
            if score > 0 and node.selector_hint not in exclude:
                scored.append((score, node.selector_hint))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:3]]

    # Legacy helper kept for reference, but structured matching is preferred
    def _list_visible(self, selector: str, limit: int = 30) -> list[str]:
        try:
            elements = self.page.locator(selector).all()
            texts = []
            for el in elements[:limit]:
                try:
                    if el.is_visible():
                        text = el.inner_text()[:80] if el.inner_text() else "(empty)"
                        tag = el.evaluate("el => el.tagName.toLowerCase()")
                        texts.append(f"<{tag}> {text}")
                except Exception:
                    pass
            return texts
        except Exception:
            return []


# ── Selector Hint Builder ───────────────────────────

def _build_selector_hint(tag: str, text: str, aria_label: str,
                         placeholder: str, name: str) -> str:
    """Heuristic to pick the most stable selector for an element."""
    if name:
        return f"{tag}#{name}"
    if aria_label:
        return f"{tag}[aria-label='{aria_label}']"
    if placeholder:
        return f"{tag}[placeholder='{placeholder}']"
    if text:
        escaped = text.replace("'", "\\'")
        if len(escaped) <= 50:
            return f"{tag}:has-text('{escaped}')"
        return f"{tag}:has-text('{escaped[:47]}...')"
    return tag


# ── Quick Test ───────────────────────────────────────
if __name__ == "__main__":
    ue = UnifiedExecutor(headless=True)

    print("=== Navigate to ModelScope ===")
    r = ue.execute({"action": "navigate", "url": "https://modelscope.cn/models"})
    print(f"  Status: {r.status} | {r.result}")
    print(f"  Duration: {r.duration_ms:.0f}ms")

    print("\n=== [Self-Healing Test] Click broken selector 'text=Modelz' ===")
    print("  (deliberate typo — should fuzzy-match to 'Models' link)")
    r = ue.execute({"action": "click", "selector": "text=Modelz"})
    print(f"  Status: {r.status}")
    print(f"  Fix applied: {r.fix_applied}")
    print(f"  Retries: {r.retries}")
    if r.error:
        print(f"  Error: {r.error[:120]}")

    print("\n=== Click non-existent element (should fail+capture) ===")
    r = ue.execute({"action": "click", "selector": "button.non-existent-xyz"})
    print(f"  Status: {r.status}")
    print(f"  Error type: {r.error_type}")
    print(f"  Screenshot: {r.screenshot_path}")
    print(f"  DOM snapshot: {r.dom_snapshot_path}")
    print(f"  Fix applied: {r.fix_applied}")
    print(f"  Retries: {r.retries}")

    ue.close()
    print("\n=== Done ===")
