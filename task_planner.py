#!/usr/bin/env python3
"""
Task Planner V3 — Control Runtime Kernel.

State-driven execution engine. Planner is bootstrap only;
the runtime controls step progression via DOM state conditions.

Architecture:
    goal → ClaudePlanner (cold start) → WorkflowEngine
              ↓
         ControlRuntime ←→ StateVerifier + ContextStore
              ↓
        UnifiedExecutor (limbs)

Control primitives: sequence, loop, condition.
Claude invoked only on cold start and failure replan — never in the hot loop.

Usage:
    from task_planner import WorkflowEngine
    from unified_executor import UnifiedExecutor

    ue = UnifiedExecutor()
    engine = WorkflowEngine(ue)
    result = engine.run("workflows/xianyu_publish.json",
                        vars={"image_path": "/tmp/item.jpg"})
"""

import json
import time
import hashlib
import copy
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field

from unified_executor import UnifiedExecutor


# ── Config ───────────────────────────────────────────
WORKFLOW_DIR = Path(__file__).parent / "workflows"
TELEMETRY_DIR = Path(__file__).parent / "failures"
DEFAULT_EXPECT_TIMEOUT = 5000   # soft-wait ms before replan
LOOP_INTERVAL = 1.0             # seconds between loop iterations


# ── Context Store ────────────────────────────────────

class ContextStore:
    """Cross-step variable carrier. Resolves {{var}} in action dicts."""

    def __init__(self, initial: Optional[dict] = None):
        self.store: dict = initial or {}

    def save(self, key: str, value):
        self.store[key] = value

    def get(self, key: str):
        return self.store.get(key)

    def resolve(self, obj):
        """Recursively replace {{var}} in any string within a nested structure."""
        if isinstance(obj, str):
            result = obj
            for k, v in self.store.items():
                result = result.replace(f"{{{{{k}}}}}", str(v))
            return result
        elif isinstance(obj, dict):
            return {kk: self.resolve(vv) for kk, vv in obj.items()}
        elif isinstance(obj, list):
            return [self.resolve(item) for item in obj]
        return obj


# ── State Verifier ───────────────────────────────────

class StateVerifier:
    """Soft-wait for expect conditions. Returns True if satisfied within timeout."""

    def __init__(self, page):
        self.page = page

    def verify(self, expect: Optional[dict]) -> bool:
        if not expect:
            return True
        timeout = expect.get("timeout", DEFAULT_EXPECT_TIMEOUT)
        selector = expect.get("selector")
        text_contains = expect.get("text_contains")

        try:
            if selector:
                self.page.wait_for_selector(
                    selector, state="visible", timeout=timeout
                )
            if text_contains:
                deadline = time.time() + timeout / 1000.0
                while time.time() < deadline:
                    if text_contains in self.page.content():
                        return True
                    time.sleep(0.3)
                return False
            return True
        except Exception:
            return False


# ── Data Structures ──────────────────────────────────

@dataclass
class StepResult:
    index: int
    desc: str
    action: dict
    status: str              # "ok" | "failed" | "replanned"
    expect_satisfied: bool = True
    collected: dict = field(default_factory=dict)
    error: Optional[str] = None
    executor_result: Optional[dict] = None  # ActionResult as dict


@dataclass
class RunReport:
    goal: str
    status: str              # "ok" | "partial" | "failed"
    steps: list = field(default_factory=list)
    replans: int = 0
    total_duration_ms: float = 0


# ── Control Runtime ──────────────────────────────────

class ControlRuntime:
    """State machine that executes sequence/loop/condition primitives.

    Does NOT know about Claude or replan. It just runs steps and
    reports per-step results. The WorkflowEngine handles replan logic.
    """

    def __init__(self, executor: UnifiedExecutor, verifier: StateVerifier,
                 context: ContextStore):
        self.exec = executor
        self.verifier = verifier
        self.context = context

    def run_sequence(self, steps: list[dict], start_index: int = 0) -> list[StepResult]:
        """Execute steps[start_index:] in order. Stops on first failure."""
        results: list[StepResult] = []
        for i, step in enumerate(steps):
            actual_index = start_index + i
            desc = step.get("desc", f"step_{actual_index}")
            step_type = step.get("type", "action")

            if step_type == "loop":
                sr = self._run_loop(step, actual_index, desc)
            elif step_type == "condition":
                sr = self._run_condition(step, actual_index, desc)
            else:
                sr = self._run_action(step, actual_index, desc)

            results.append(sr)

            if sr.status == "failed":
                break

        return results

    def _run_action(self, step: dict, index: int, desc: str) -> StepResult:
        action_raw = step.get("action", {})
        action = self.context.resolve(action_raw)

        result = self.exec.execute(action)

        sr = StepResult(
            index=index,
            desc=desc,
            action=action,
            status=result.status,
            executor_result={
                "status": result.status,
                "error": result.error,
                "error_type": result.error_type,
                "fix_applied": result.fix_applied,
                "retries": result.retries,
                "duration_ms": result.duration_ms,
            },
        )

        if result.status == "ok":
            # Verify expect
            expect = step.get("expect")
            if not self.verifier.verify(expect):
                sr.status = "failed"
                sr.expect_satisfied = False
                sr.error = f"expect failed: {expect}"
                return sr

            # Collect variables
            collect_cfg = step.get("collect", {})
            for var_name, source in collect_cfg.items():
                val = self._extract_value(source)
                if val is not None:
                    self.context.save(var_name, val)
                    sr.collected[var_name] = val
        else:
            sr.error = result.error
            sr.expect_satisfied = False

        return sr

    def _run_loop(self, step: dict, index: int, desc: str) -> StepResult:
        """Repeat action until condition or max_iterations."""
        action_raw = step.get("action", {})
        until = step.get("until", {})
        max_iter = step.get("max_iterations", 20)
        interval = step.get("interval", LOOP_INTERVAL)

        metric_type = until.get("type", "node_count_stable")
        target_sel = until.get("selector", "*")
        stable_for = until.get("stable_for", 2)
        data_selector = until.get("data_selector")

        prev_count = 0
        stable_count = 0

        for it in range(max_iter):
            action = self.context.resolve(action_raw)
            result = self.exec.execute(action)

            if result.status != "ok":
                return StepResult(
                    index=index, desc=desc, action=action,
                    status="failed",
                    error=f"loop iteration {it} failed: {result.error}",
                )

            time.sleep(interval)

            # Evaluate until condition
            curr_count = self._measure(metric_type, target_sel, data_selector)

            if curr_count == prev_count:
                stable_count += 1
                if stable_count >= stable_for:
                    return StepResult(
                        index=index, desc=desc, action=action,
                        status="ok",
                        executor_result={
                            "iterations": it + 1,
                            "final_metric": curr_count,
                            "termination": "stable",
                        },
                    )
            else:
                stable_count = 0
            prev_count = curr_count

        return StepResult(
            index=index, desc=desc, action=action_raw,
            status="failed",
            error=f"loop exhausted max_iterations={max_iter} without stabilizing",
        )

    def _run_condition(self, step: dict, index: int, desc: str) -> StepResult:
        """Evaluate condition, execute matching branch."""
        cond = step.get("condition", {})
        check = cond.get("check", "selector_visible")
        selector = cond.get("selector", "")
        value = cond.get("value", True)

        cond_met = self._evaluate_condition(check, selector, value)
        branch = step.get("then") if cond_met else step.get("else", [])

        if not branch:
            return StepResult(
                index=index, desc=desc, action={},
                status="ok",  # no-op branch, not a failure
                executor_result={"branch": "then" if cond_met else "else", "executed": False},
            )

        step_results = self.run_sequence(branch, index)
        # Merge: return first result as representative, or aggregate
        if step_results:
            return step_results[0]
        return StepResult(index=index, desc=desc, action={}, status="ok")

    def _measure(self, metric_type: str, selector: str,
                 data_selector: Optional[str] = None) -> int:
        if metric_type == "data_increment" and data_selector:
            try:
                return self.exec.page.locator(data_selector).count()
            except Exception:
                return 0
        # Default: node_count_stable or node_count
        try:
            return self.exec.page.locator(selector).count()
        except Exception:
            return 0

    def _evaluate_condition(self, check: str, selector: str, value) -> bool:
        if check == "selector_visible":
            try:
                el = self.exec.page.locator(selector).first
                return el.is_visible() == value
            except Exception:
                return not value
        if check == "text_contains":
            try:
                return (value in self.exec.page.content()) == True
            except Exception:
                return False
        return False

    def _extract_value(self, source: str) -> Optional[str]:
        """Parse collect source strings like 'attr:src@img.preview' or 'text@.status'."""
        try:
            if source.startswith("attr:"):
                rest = source[len("attr:"):]
                attr, sel = rest.split("@", 1)
                el = self.exec.page.locator(sel).first
                return el.get_attribute(attr)
            if source.startswith("text@"):
                sel = source[len("text@"):]
                return self.exec.page.locator(sel).first.inner_text()
            # Assume it's a raw CSS selector, return text
            return self.exec.page.locator(source).first.inner_text()
        except Exception:
            return None


# ── Claude Planner (stub) ────────────────────────────

class ClaudePlanner:
    """Cold-start plan generation and failure replan.

    Currently a stub that prints diagnostic context.
    Real implementation: sends goal + ActionableNode[] to Claude,
    receives JSON step array.
    """

    def cold_start(self, goal: str, snapshot: dict) -> list[dict]:
        """Generate initial workflow from goal + current page state."""
        print(f"\n{'='*60}")
        print(f"[ClaudePlanner] Cold start planning")
        print(f"  Goal: {goal}")
        print(f"  URL: {snapshot.get('url')}")
        print(f"  Nodes available: {snapshot.get('node_count')}")
        print(f"{'='*60}")
        print("[ClaudePlanner] No template matched. "
              "Provide a workflow template or implement API call.")
        return []

    def replan(self, goal: str, history: list[StepResult],
               failed_step: StepResult, snapshot: dict) -> list[dict]:
        """Replan remaining steps after failure."""
        print(f"\n{'='*60}")
        print(f"[ClaudePlanner] REPLAN triggered")
        print(f"  Goal: {goal}")
        print(f"  Failed step [{failed_step.index}]: {failed_step.desc}")
        print(f"  Error: {failed_step.error}")
        print(f"  History: {len(history)} completed steps")
        print(f"  Current URL: {snapshot.get('url')}")
        print(f"  Nodes available: {snapshot.get('node_count')}")
        print(f"{'='*60}")
        print("[ClaudePlanner] Replan not yet integrated. "
              "Provide remaining steps or implement API call.")
        return []


# ── Workflow Engine ──────────────────────────────────

class WorkflowEngine:
    """Top-level orchestrator.

    Loads workflow templates (JSON), delegates to ControlRuntime,
    triggers ClaudePlanner on failure. Records replan events to
    failures/ for V4 state model training.

    Usage:
        ue = UnifiedExecutor()
        engine = WorkflowEngine(ue)
        report = engine.run("workflows/xianyu_publish.json")
    """

    def __init__(self, executor: UnifiedExecutor,
                 planner: Optional[ClaudePlanner] = None):
        self.exec = executor
        self.planner = planner or self._default_planner()
        self.context = ContextStore()
        self.verifier = StateVerifier(executor.page)
        self.runtime = ControlRuntime(executor, self.verifier, self.context)
        self.max_replans = 2

    @staticmethod
    def _default_planner():
        """Auto-detect planner: DeepSeek if API key set, else ClaudePlanner stub."""
        import os
        if os.environ.get("DEEPSEEK_API_KEY"):
            from deepseek_planner import DeepSeekPlanner
            return DeepSeekPlanner()
        return ClaudePlanner()

    def run(self, workflow_path: str, goal: str = "",
            vars: Optional[dict] = None) -> RunReport:
        """Main entry. Load workflow, execute, handle replan."""
        t0 = time.time()

        if vars:
            for k, v in vars.items():
                self.context.save(k, v)

        # Load template or cold-start
        steps = self._load_workflow(workflow_path)
        if not steps:
            snapshot = self.exec.snapshot()
            steps = self.planner.cold_start(goal, snapshot)
            if not steps:
                return RunReport(
                    goal=goal,
                    status="failed",
                    steps=[],
                    total_duration_ms=(time.time() - t0) * 1000,
                )

        all_results: list[StepResult] = []
        replans = 0
        seen_plans: set[str] = set()  # hash of replan steps → anti-loop

        while steps and replans <= self.max_replans:
            new_results = self.runtime.run_sequence(steps, len(all_results))
            all_results.extend(new_results)

            # Check if we hit a failure
            last = new_results[-1] if new_results else None
            if last is None or last.status == "ok":
                break

            # Failure — replan remaining steps
            replans += 1
            snapshot = self.exec.snapshot()
            remaining = self.planner.replan(
                goal, all_results[:-1], last, snapshot
            )

            if not remaining:
                break

            # Anti-loop: detect repeated plan
            plan_hash = self._hash_steps(remaining)
            if plan_hash in seen_plans:
                print(f"[WorkflowEngine] Anti-loop: same plan returned. Breaking.")
                self._record_replan(goal, all_results, last, snapshot, remaining,
                                    loop_detected=True)
                break
            seen_plans.add(plan_hash)

            # Black box: record replan event for V4 training
            self._record_replan(goal, all_results, last, snapshot, remaining)

            steps = remaining

        report = RunReport(
            goal=goal,
            status=self._compute_status(all_results),
            steps=[asdict(sr) for sr in all_results],
            replans=replans,
            total_duration_ms=(time.time() - t0) * 1000,
        )

        return report

    def _load_workflow(self, path: str) -> list[dict]:
        """Load a workflow JSON file. Supports relative paths."""
        p = Path(path)
        if not p.is_absolute():
            p = WORKFLOW_DIR / path
        if not p.exists():
            print(f"[WorkflowEngine] Template not found: {p}")
            return []
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            return data.get("steps", [])
        if isinstance(data, list):
            return data
        return []

    def _compute_status(self, results: list[StepResult]) -> str:
        if not results:
            return "failed"
        if all(r.status == "ok" for r in results):
            return "ok"
        if any(r.status == "ok" for r in results):
            return "partial"
        return "failed"

    def _hash_steps(self, steps: list[dict]) -> str:
        """Stable hash of step list for anti-loop detection."""
        raw = json.dumps(steps, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _record_replan(self, goal: str, history: list[StepResult],
                       failed_step: StepResult, snapshot: dict,
                       replan_output: list[dict], loop_detected: bool = False):
        """Black-box telemetry: persist replan event for V4 state model."""
        try:
            TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            idx = failed_step.index
            path = TELEMETRY_DIR / f"replan_{ts}_step{idx}.json"

            record = {
                "timestamp": ts,
                "goal": goal,
                "failed_step_index": idx,
                "failed_step_desc": failed_step.desc,
                "error": failed_step.error,
                "history": [asdict(s) for s in history],
                "snapshot": {
                    "url": snapshot.get("url"),
                    "title": snapshot.get("title"),
                    "node_count": snapshot.get("node_count"),
                    # Store full nodes for V4 analysis
                    "nodes": snapshot.get("nodes", [])[:50],
                },
                "replan_output": replan_output,
                "loop_detected": loop_detected,
            }
            Path(path).write_text(json.dumps(record, ensure_ascii=False, indent=2))
            print(f"[WorkflowEngine] Telemetry saved: {path}")
        except Exception as e:
            print(f"[WorkflowEngine] Telemetry write failed: {e}")


# ── CLI ──────────────────────────────────────────────

def asdict(obj) -> dict:
    """Polyfill in case dataclasses.asdict is unavailable."""
    if hasattr(obj, "__dataclass_fields__"):
        return {f: asdict(getattr(obj, f)) for f in obj.__dataclass_fields__}
    if isinstance(obj, list):
        return [asdict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: asdict(v) for k, v in obj.items()}
    return obj


# ── Quick Test ───────────────────────────────────────
if __name__ == "__main__":
    ue = UnifiedExecutor(headless=True)

    print("=== Task Planner V3 Smoke Test ===\n")

    # Test 1: ContextStore variable resolution
    print("--- ContextStore ---")
    ctx = ContextStore({"name": "M3 Ultra", "price": "28000"})
    resolved = ctx.resolve({
        "action": "fill",
        "value": "{{name}} 仅售 {{price}}"
    })
    assert resolved["value"] == "M3 Ultra 仅售 28000"
    print(f"  resolve: {resolved['value']}")

    # Test 2: Load workflow template
    print("\n--- Workflow Load ---")
    engine = WorkflowEngine(ue)
    steps = engine._load_workflow("xianyu_publish.json")
    if steps:
        print(f"  Loaded {len(steps)} steps:")
        for s in steps:
            print(f"    [{s.get('desc', '?')}] action={s.get('action',{}).get('action')}")
    else:
        print("  (template not found — expected before creation)")

    # Test 3: Run a micro-workflow (navigate + snap)
    print("\n--- Micro Run: navigate + snapshot ---")
    micro = [
        {
            "desc": "导航到 ModelScope",
            "action": {"action": "navigate", "url": "https://modelscope.cn/models"},
            "expect": {"text_contains": "ModelScope", "timeout": 8000},
        },
    ]
    results = engine.runtime.run_sequence(micro)
    for r in results:
        print(f"  [{r.index}] {r.desc}: {r.status}")
        if r.executor_result:
            er = r.executor_result
            print(f"       duration={er.get('duration_ms',0):.0f}ms expect_ok={r.expect_satisfied}")

    # Show snapshot for Planner consumption
    snap = ue.snapshot()
    print(f"\n  Snapshot: url={snap['url']}")
    print(f"  Nodes: {snap['node_count']} (first 3):")
    for n in snap["nodes"][:3]:
        print(f"    <{n['tag']}> {n['text'][:50]}  bbox={n.get('bbox',{}).get('x')},{n.get('bbox',{}).get('y')}")

    ue.close()
    print("\n=== Smoke test done ===")
