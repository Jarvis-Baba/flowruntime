#!/usr/bin/env python3
"""Quick demo: self-healing + workflow execution in one command."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unified_executor import UnifiedExecutor
from task_planner import WorkflowEngine


def main():
    ue = UnifiedExecutor(headless=True)
    engine = WorkflowEngine(ue)

    print("=" * 60)
    print("  FlowRuntime Demo")
    print("=" * 60)

    # Demo 1: Self-healing selector
    print("\n[1/3] Self-healing: broken selector")
    print("      Action: click('text=Modelz') — deliberate typo")
    r = ue.execute({"action": "navigate", "url": "https://modelscope.cn/models"})
    print(f"      Navigate: {r.status} ({r.duration_ms:.0f}ms)")

    r = ue.execute({"action": "click", "selector": "text=Modelz"})
    if r.fix_applied:
        print(f"      Healed: {r.fix_applied}")
    print(f"      Result: {r.status} (retries={r.retries})")

    # Demo 2: Workflow execution
    print("\n[2/3] Workflow: browse + collect")
    report = engine.run("modelscope_browse.json", goal="Browse DeepSeek model")
    for s in report.steps:
        icon = "+" if s["status"] == "ok" else "!"
        print(f"      [{icon}] {s['desc']}: {s['status']}")
        if s.get("collected"):
            print(f"           collected: {s['collected']}")

    # Demo 3: Snapshot for cold-start planning
    print("\n[3/3] Snapshot: Actionable DOM for planner")
    snap = ue.snapshot()
    print(f"      URL: {snap['url']}")
    print(f"      Nodes: {snap['node_count']} actionable elements")
    buttons = [n for n in snap["nodes"] if n["tag"] == "button"]
    links = [n for n in snap["nodes"] if n["tag"] == "a"]
    inputs = [n for n in snap["nodes"] if n["tag"] == "input"]
    print(f"      Breakdown: {len(buttons)} buttons, "
          f"{len(links)} links, {len(inputs)} inputs")

    ue.close()
    print(f"\n{'=' * 60}")
    print("  Demo complete.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
