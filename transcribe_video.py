#!/usr/bin/env python3
"""
transcribe_video.py — Transcribe YouTube audio via Gemini API, then deep-analyze.
Usage: python3 transcribe_video.py /tmp/claude_code_tutorial.mp3
"""

import sys, os, json, base64, time
from pathlib import Path
from urllib.request import Request
from socks5_patch import socks5_urlopen

API_KEY = os.environ.get("GEMINI_API_KEY", "")

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
OUT_DIR = Path("/tmp/video_transcript")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _get_audio_path():
    return Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/claude_code_tutorial.mp3")


def call_gemini(parts, model="gemini-2.5-flash", max_tokens=8192,
                system_prompt="", temperature=0.3):
    """Send parts list to Gemini. Each part is a dict: {text} or {inline_data}."""
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }
    if system_prompt:
        body["system_instruction"] = {"parts": [{"text": system_prompt}]}

    url = f"{API_BASE}/{model}:generateContent?key={API_KEY}"
    req = Request(url, data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json"})

    for attempt in range(3):
        try:
            resp = socks5_urlopen(req, timeout=300)
            data = json.loads(resp.read())
            text = (data.get("candidates", [{}])[0]
                    .get("content", {}).get("parts", [{}])[0]
                    .get("text", ""))
            usage = data.get("usageMetadata", {})
            return {"ok": True, "text": text, "usage": usage}
        except Exception as e:
            err = str(e)[:200]
            if attempt < 2:
                print(f"  Retry {attempt+2}/3 after: {err}")
                time.sleep(5)
            else:
                return {"ok": False, "error": err}


# ── Phase 1: Full Transcription ──

AUDIO_PATH = _get_audio_path()

print("=" * 60)
print("Phase 1: Full Audio Transcription via Gemini 2.5 Flash")
print(f"Audio: {AUDIO_PATH} ({AUDIO_PATH.stat().st_size / 1024 / 1024:.1f} MB)")
print("=" * 60)

# Read and encode audio
audio_b64 = base64.b64encode(AUDIO_PATH.read_bytes()).decode()
print(f"Base64 encoded: {len(audio_b64) / 1024 / 1024:.1f} MB")

# Transcribe in one shot. Gemini 2.5 Flash has 1M token context — enough for ~56 min audio.
print("\nSending to Gemini 2.5 Flash for full transcription...")
print("(This may take 2-4 minutes for a 56-minute audio)\n")

transcribe_result = call_gemini(
    parts=[
        {"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}},
        {"text": "请将这段音频完整转写为中文文字。要求：\n1. 逐字逐句转写，不要省略任何内容\n2. 如果音频中有英文术语，保留原文并标注中文含义\n3. 按自然段落分段，标注大致时间点（如 [00:00]、[10:30] 等）\n4. 保留演讲者的语气词和重复强调的内容\n5. 输出完整的文字记录，不要总结或缩写"},
    ],
    model="gemini-2.5-flash",
    max_tokens=65536,  # 64K output
    system_prompt="你是一个专业的中文转录员。你的任务是将音频完整逐字转写为中文文字。不要总结，不要省略。保留所有细节。",
    temperature=0.1,
)

if not transcribe_result["ok"]:
    print(f"ERROR: {transcribe_result['error']}")
    sys.exit(1)

transcript = transcribe_result["text"]
usage = transcribe_result.get("usage", {})

transcript_path = OUT_DIR / "full_transcript.md"
transcript_path.write_text(transcript, encoding="utf-8")
print(f"\nTranscript saved: {transcript_path}")
print(f"Length: {len(transcript)} chars, ~{len(transcript)//1000}k chars")
print(f"Tokens: {usage}")

# ── Phase 2: Deep Analysis via Gemini 2.5 Pro ──

print("\n" + "=" * 60)
print("Phase 2: Deep Analysis via Gemini 2.5 Pro")
print("=" * 60)

analysis_prompt = f"""你是一位资深技术作者和AI工具专家。请对以下 Claude Code 教程视频的完整文字记录进行深度阅读和分析。

要求：
1. **核心内容梳理**：视频讲了什么？结构是怎样的？用 3-5 句话概括。
2. **知识点清单**：列出视频中提到的所有 Claude Code 功能、命令、配置项和使用技巧。每个知识点附带简要说明和最佳使用场景。
3. **重点详解**：对视频中着重讲解的 3-5 个核心功能做详细展开，包括原理、用法、注意事项。
4. **进阶技巧**：提取视频中提到的高级用法、隐藏技巧、常见陷阱。
5. **完整命令索引**：整理视频中出现的所有命令和快捷键，做成速查表。
6. **个人评价**：这个教程的质量如何？适合什么水平的用户？有什么遗漏值得补充？

完整文字记录：
---
{transcript}
---"""

analysis_result = call_gemini(
    parts=[{"text": analysis_prompt}],
    model="gemini-2.5-pro",
    max_tokens=16384,
    system_prompt="你是一位资深技术作者和AI工具专家。请深入分析这份 Claude Code 教程的文字记录，输出结构化的阅读笔记。",
    temperature=0.4,
)

if analysis_result["ok"]:
    analysis_path = OUT_DIR / "deep_analysis.md"
    full_report = f"""# Claude Code 全面掌握教程 — 深度阅读笔记

> 视频: 全网最全！60分钟全面掌握Claude Code～【附完整文档】
> YouTube: https://www.youtube.com/watch?v=kdTh2BujX8Q
> 分析时间: {time.strftime('%Y-%m-%d %H:%M')}
> 转写模型: Gemini 2.5 Flash
> 分析模型: Gemini 2.5 Pro

---
## 完整文字记录
> 共 {len(transcript)} 字符

{transcript}

---
## 深度分析

{analysis_result['text']}

---
*由 Gemini 自动生成并人工整理*
"""
    analysis_path.write_text(full_report, encoding="utf-8")
    print(f"\nFull report saved: {analysis_path}")
    print(f"Report size: {analysis_path.stat().st_size / 1024:.1f} KB")
else:
    print(f"Analysis ERROR: {analysis_result['error']}")

print("\nDone.")
