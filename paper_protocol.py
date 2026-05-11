#!/usr/bin/env python3
"""OC x CC 论文研究闭环 — 通信协议 & Prompt 构建"""

import json
from typing import Any

# ── OC → CC Task Schema ──
TASK_SCHEMA = {
    "type": "paper_deep_read",
    "paper_id": str,
    "title": str,
    "source": str,
    "priority": float,
    "tags": list,
    "context_hint": str,
}

# ── CC → OC Result Schema ──
RESULT_SCHEMA = {
    "paper_id": str,
    "summary": {"problem": str, "method": str, "contribution": str},
    "analysis": {"novelty": float, "technical_depth": float, "credibility": float},
    "insight": list,
    "system_action": {
        "should_store": bool,
        "should_replicate": bool,
        "should_trigger_code": bool,
        "affected_module": str,
        "action_note": str,
    },
}

# ── CC Paper Analysis System Prompt ──
SYSTEM_PROMPT = """你是 OpenClaw 系统的首席科学家。你的任务是解构式阅读论文，输出可被系统消费的结构化 JSON。

## 阅读原则
- 你不是在写论文笔记，你是在评估这篇论文对 OC+CC 双脑系统的潜在贡献
- 如果论文只是堆砌 Trick 或刷榜，在 novelty 上打低分
- 优先关注：新机制、新架构、可复现的实验方法

## 输出格式（必须严格 JSON，禁止任何开场白或尾注）
{
  "summary": {
    "problem": "论文解决的核心问题（一句话）",
    "method": "核心方法（人话，不超过3句）",
    "contribution": "本质贡献是什么"
  },
  "analysis": {
    "novelty": 0.0,
    "technical_depth": 0.0,
    "credibility": 0.0
  },
  "insight": [
    "对我们的系统有什么启发",
    "具体可以用在哪个模块"
  ],
  "system_action": {
    "should_store": false,
    "should_replicate": false,
    "should_trigger_code": false,
    "affected_module": "",
    "action_note": ""
  }
}

## 评分锚定
- novelty: 0=完全已知, 0.3=微改进, 0.5=新组合, 0.7=新范式, 1.0=开山之作
- technical_depth: 0=纯描述, 0.5=有推导, 1.0=理论+实验双深
- credibility: 0=无实验, 0.5=实验存在但不够, 1.0=充分消融+开源代码

## system_action 判断标准
- should_store: contribution 有价值 → true
- should_replicate: 方法可落地且对我们有用 → true
- should_trigger_code: 明确能改进现有模块 → true
- affected_module: task_bus | memory | rag | none
- action_note: 一句话说明如何落地"""


def validate_task(task: dict) -> tuple[bool, str]:
    """验证 OC 发来的任务格式"""
    if task.get("type") != "paper_deep_read":
        return False, "type must be paper_deep_read"
    if not task.get("paper_id"):
        return False, "missing paper_id"
    if not task.get("title"):
        return False, "missing title"
    return True, "ok"


def validate_result(result: dict) -> tuple[bool, list[str]]:
    """验证 CC 输出的结果格式，返回缺失/错误字段列表"""
    errors = []
    if "summary" not in result:
        errors.append("missing summary")
    else:
        for f in ["problem", "method", "contribution"]:
            if f not in result["summary"]:
                errors.append(f"missing summary.{f}")
    if "analysis" not in result:
        errors.append("missing analysis")
    else:
        for f in ["novelty", "technical_depth", "credibility"]:
            if f not in result["analysis"]:
                errors.append(f"missing analysis.{f}")
    if "insight" not in result:
        errors.append("missing insight")
    if "system_action" not in result:
        errors.append("missing system_action")
    return len(errors) == 0, errors


def build_prompt(paper_text: str, context_hint: str = "", max_chars: int = 8000) -> str:
    """构建发给 LLM 的完整提示词"""
    truncated = paper_text[:max_chars]
    if len(paper_text) > max_chars:
        truncated += f"\n\n[文本已截断，原文 {len(paper_text)} 字符，显示前 {max_chars}]"

    hint = f"\n系统上下文提示: {context_hint}" if context_hint else ""

    return f"""{SYSTEM_PROMPT}
{hint}

── 以下是要分析的论文 ──
{truncated}

请输出上述 JSON 格式的分析结果。"""
