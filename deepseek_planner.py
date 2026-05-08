#!/usr/bin/env python3
"""
DeepSeek Planner — LLM-driven workflow generation and replan.

Replaces ClaudePlanner stub with real DeepSeek API calls.
Used by WorkflowEngine for cold-start planning and failure replan.

Usage:
    planner = DeepSeekPlanner()
    steps = planner.cold_start("在闲鱼发布商品", snapshot)
    steps = planner.replan(goal, history, failed_step, snapshot)
"""

import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ── Config ───────────────────────────────────────────
API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL_PLAN = os.environ.get("MODEL_PLAN", "deepseek-v4-pro")
MODEL_FAST = os.environ.get("MODEL_FAST", "deepseek-v4-flash")
MAX_PLAN_TOKENS = 4096


# ── Planner System Prompts ───────────────────────────

SYSTEM_COLD_START = """\
你是一个浏览器自动化规划器。根据用户的 Goal 和当前页面的 Actionable Nodes（可操作元素列表），
生成一个 JSON 格式的工作流步骤列表。

每个步骤的格式：
{
    "desc": "步骤描述（中文）",
    "action": {"action": "navigate|click|fill|upload|wait|eval", ...},
    "expect": {"selector": "...", "timeout": 5000}  // 可选，步骤成功标志
}

控制流格式：
- 循环: {"type": "loop", "action": {...}, "until": {"type": "data_increment", "data_selector": ".item", "stable_for": 2}, "max_iterations": 20}
- 条件: {"type": "condition", "condition": {"check": "text_contains|selector_visible", "value": "..."}, "then": [...], "else": [...]}

规则：
1. 只返回 JSON 数组，不要任何解释文字，不要 markdown 代码块标记
2. 优先使用页面中已存在的 selector（参考提供的 Actionable Nodes）
3. selector 优先用 text= 或 has-text() 形式，比 CSS class 更稳定
4. 涉及登录的步骤放在最前面
5. 每步之后加 expect 验证是否成功"""

SYSTEM_REPLAN = """\
你是一个浏览器自动化修复器。当前工作流某一步执行失败，需要你生成修复步骤。

你会收到：
1. 原始目标 (goal)
2. 已完成步骤的历史 (history)
3. 失败步骤的信息 (failed_step)
4. 当前页面的 Actionable Nodes (current_nodes)

任务：生成最短的修复步骤，使系统从当前状态恢复到工作流的下一个检查点。

规则：
1. 只返回 JSON 数组，不要任何解释文字，不要 markdown 代码块标记
2. 步骤越少越好（优先关闭弹窗/等待加载，而非重新导航）
3. 优先使用当前页面中真实存在的 selector
4. 如果当前页面状态与预期严重偏离，可以包含一个 navigate 步骤回到正确页面"""


# ── DeepSeek API Client ──────────────────────────────

class DeepSeekClient:
    """Minimal DeepSeek API client. Uses urllib (no pip deps)."""

    def __init__(self, api_key: str = "", api_base: str = API_BASE):
        self.api_key = api_key or API_KEY
        self.api_base = api_base

    def chat(self, system: str, user: str, model: str = MODEL_PLAN,
             temperature: float = 0.1, max_tokens: int = MAX_PLAN_TOKENS) -> str:
        """Single-turn chat. Returns response text."""

        if not self.api_key:
            return self._mock_response(user)

        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }).encode("utf-8")

        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    f"{self.api_base}/chat/completions",
                    data=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:200]
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[DeepSeek] HTTP {e.code}: {body}")
                return ""
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[DeepSeek] Error: {e}")
                return ""
        return ""

    def _mock_response(self, user: str) -> str:
        """Fallback when API key is missing — returns empty steps."""
        print("[DeepSeek] No API key — returning empty plan. Set DEEPSEEK_API_KEY.")
        return "[]"


# ── Planner ──────────────────────────────────────────

class DeepSeekPlanner:
    """LLM-driven workflow planner using DeepSeek API.

    Drop-in replacement for ClaudePlanner stub in task_planner.py.
    """

    def __init__(self, client: Optional[DeepSeekClient] = None):
        self.client = client or DeepSeekClient()

    def cold_start(self, goal: str, snapshot: dict) -> list[dict]:
        """Generate initial workflow from goal + current page state."""
        nodes = snapshot.get("nodes", [])
        node_texts = self._format_nodes(nodes[:60])  # Top 60 to stay within context

        prompt = f"""Goal: {goal}

Current URL: {snapshot.get('url', 'unknown')}
Page title: {snapshot.get('title', 'unknown')}

Available interactive elements on this page:
{node_texts}

Generate the workflow steps to accomplish the goal."""

        print(f"\n[DeepSeekPlanner] Cold start: {goal}")
        print(f"[DeepSeekPlanner] Nodes: {len(nodes)} → sending {min(len(nodes), 60)}")

        response = self.client.chat(SYSTEM_COLD_START, prompt)
        return self._parse_steps(response)

    def replan(self, goal: str, history: list,
               failed_step, snapshot: dict) -> list[dict]:
        """Replan remaining steps after a failure."""
        nodes = snapshot.get("nodes", [])
        node_texts = self._format_nodes(nodes[:40])

        history_text = "\n".join(
            f"  [{h.index}] {h.desc}: {h.status}"
            for h in (history or [])[-5:]  # Last 5 steps
        )

        prompt = f"""Goal: {goal}

Completed steps:
{history_text or '(none)'}

FAILED step [{failed_step.index}]: {failed_step.desc}
Error: {failed_step.error}

Current URL: {snapshot.get('url', 'unknown')}

Current page elements:
{node_texts}

Generate repair steps to recover from this failure and continue toward the goal."""

        print(f"\n[DeepSeekPlanner] Replan: step {failed_step.index} failed")
        print(f"[DeepSeekPlanner] Error: {failed_step.error[:100]}")

        response = self.client.chat(SYSTEM_REPLAN, prompt, model=MODEL_FAST)
        return self._parse_steps(response)

    def _format_nodes(self, nodes: list[dict]) -> str:
        """Format ActionableNodes for LLM consumption."""
        lines = []
        for n in nodes[:80]:
            tag = n.get("tag", "?")
            text = (n.get("text") or "")[:80]
            hint = n.get("selector_hint", "")
            aria = n.get("aria_label", "")
            role = n.get("role", "")
            extras = []
            if aria:
                extras.append(f"aria='{aria}'")
            if role:
                extras.append(f"role='{role}'")
            extra_str = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"  <{tag}> \"{text}\"  hint={hint}{extra_str}")
        return "\n".join(lines)

    def _parse_steps(self, response: str) -> list[dict]:
        """Parse JSON step array from LLM response. Handles markdown fences."""
        text = response.strip()
        if not text:
            return []

        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove opening ```json or ```
            if lines[0].startswith("```"):
                lines = lines[1:]
            # Remove closing ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            # Try to extract JSON array from mixed text
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
        return []


# ── Quick Test ───────────────────────────────────────
if __name__ == "__main__":
    planner = DeepSeekPlanner()

    # Test with mock snapshot
    snap = {
        "url": "https://modelscope.cn/models",
        "title": "ModelScope Models",
        "node_count": 10,
        "nodes": [
            {"tag": "a", "text": "Models", "selector_hint": "a:has-text('Models')"},
            {"tag": "a", "text": "DeepSeek-V4", "selector_hint": "a:has-text('DeepSeek-V4')"},
            {"tag": "button", "text": "login / register", "selector_hint": "button:has-text('login')"},
            {"tag": "input", "text": "", "selector_hint": "input[placeholder='search']", "placeholder": "search"},
        ],
    }

    print("=== DeepSeekPlanner Test ===\n")
    print(f"API key set: {bool(API_KEY)}")
    print(f"Model: {MODEL_PLAN}")

    steps = planner.cold_start("找到 DeepSeek-V4 模型并打开", snap)
    if steps:
        print(f"\nGenerated {len(steps)} steps:")
        for s in steps:
            print(f"  [{s.get('desc', '?')}] action={s.get('action', {}).get('action')}")
    else:
        print("\n(No steps generated — set DEEPSEEK_API_KEY to test with real API)")
