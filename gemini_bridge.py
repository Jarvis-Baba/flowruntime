#!/usr/bin/env python3
"""
gemini_bridge.py — CC 调用 Gemini API 的桥接工具
支持文本生成、搜索接地、长上下文、视觉理解。
Google AI Studio 免费层，不绑卡。

用法:
  python3 gemini_bridge.py "你的问题"
  python3 gemini_bridge.py --search "今天AI领域有什么新闻"   # 带Google搜索
  python3 gemini_bridge.py --model gemini-2.5-pro --file report.pdf "总结"
  python3 gemini_bridge.py --json "列出3个Python最佳实践"    # JSON输出
"""

import sys, os, json, argparse
from pathlib import Path
from urllib.request import Request
from urllib.error import HTTPError
from socks5_patch import socks5_urlopen

def _load_keys():
    """从 cc.env 加载 CC 专用 Gemini Key（两把）。
    硬边界：CC 绝不读取 gateway.env（那是 OC 的 Key），
    一方额度耗尽不借调另一方。"""
    keys = []
    key_file = Path.home() / ".claude" / "cc.env"
    if key_file.exists():
        for line in key_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("GOOGLE_API_KEY"):
                k = line.split("=", 1)[1].strip()
                if k and k not in keys:
                    keys.append(k)
    return keys

_API_KEYS = _load_keys()
API_KEY = _API_KEYS[0] if _API_KEYS else ""

# 模型别名
# 免费层可用: flash/3flash/3.1lite/flash-lite/gemma (所有 Flash 变体)
# 需付费: pro/3pro/3.1pro/research/computer (Pro 系列 + 特殊能力)
# bidi 端点: audio 模型只支持 bidiGenerateContent, 不走 generateContent
MODELS = {
    # === 免费层主力 ===
    "flash":     "gemini-2.5-flash",           # 最成熟的 Flash, 多模态+搜索
    "flash-lite":"gemini-2.5-flash-lite",      # 更省额度
    "3flash":    "gemini-3-flash-preview",     # 第三代 Flash
    "3.1lite":   "gemini-3.1-flash-lite",      # 最新轻量
    "gemma":     "gemma-4-31b-it",             # 开源, 无配额限制
    # === 付费层 (目前不可用, 预留) ===
    "pro":       "gemini-2.5-pro",
    "3pro":      "gemini-3-pro-preview",
    "3.1pro":    "gemini-3.1-pro-preview",
    "research":  "deep-research-max-preview-04-2026",
    "computer":  "gemini-2.5-computer-use-preview-10-2025",
    # === bidi 端点 (需 WebSocket, 当前桥暂不支持) ===
    "audio":     "gemini-2.5-flash-native-audio-latest",
}

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def call_gemini(prompt, model="gemini-2.5-flash", search=False, json_mode=False,
                system_prompt="", image_path=None, max_tokens=4096, api_key=None):
    tools = []
    if search:
        tools.append({"google_search": {}})

    contents = []
    parts = [{"text": prompt}]

    if image_path:
        import base64
        img_data = base64.b64encode(Path(image_path).read_bytes()).decode()
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        }
        mime = mime_map.get(ext, "image/jpeg")
        parts.insert(0, {"inline_data": {"mime_type": mime, "data": img_data}})

    contents.append({"role": "user", "parts": parts})

    body = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.7,
        }
    }

    if json_mode:
        body["generationConfig"]["response_mime_type"] = "application/json"

    if system_prompt:
        body["system_instruction"] = {"parts": [{"text": system_prompt}]}

    if tools:
        body["tools"] = tools

    keys = [api_key] if api_key else _API_KEYS
    if not keys:
        return {"error": "NO_KEY", "detail": "未找到有效的 Google API Key"}

    last_err = None
    for idx, key in enumerate(keys):
        url = f"{API_BASE}/{model}:generateContent?key={key}"
        req = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})

        try:
            resp = socks5_urlopen(req, timeout=120)
            data = json.loads(resp.read())

            if "error" in data:
                err = data["error"]
                if err.get("code") == 429 and idx + 1 < len(keys):
                    print(f"[gemini] Key#{idx+1} 限流, 降级到 Key#{idx+2}", file=sys.stderr)
                    continue
                return {"error": f"API {err.get('code')}", "detail": err.get("message", "")[:500]}

            if "candidates" not in data or not data["candidates"]:
                return {"error": "EMPTY_RESPONSE", "detail": "模型返回空 candidates"}

            candidate = data["candidates"][0]
            content = candidate.get("content", {})
            resp_parts = content.get("parts", [])
            if not resp_parts:
                finish = candidate.get("finishReason", "?")
                return {"error": "NO_OUTPUT", "detail": f"输出为空 (finish={finish})"}

            text = resp_parts[0].get("text", "")

            grounding = candidate.get("groundingMetadata", {})
            sources = []
            for chunk in grounding.get("groundingChunks", []):
                web = chunk.get("web", {})
                if web.get("uri"):
                    sources.append({"title": web.get("title", ""), "uri": web["uri"]})

            result = {"text": text, "model": model, "key_index": idx}
            if sources:
                result["sources"] = sources
            if search:
                search_queries = grounding.get("webSearchQueries", [])
                if search_queries:
                    result["search_queries"] = search_queries

            return result

        except HTTPError as e:
            body_raw = e.read().decode()
            if e.code == 429 and idx + 1 < len(keys):
                print(f"[gemini] Key#{idx+1} 限流 (HTTP 429), 降级到 Key#{idx+2}", file=sys.stderr)
                continue
            last_err = {"error": f"HTTP {e.code}", "detail": body_raw[:500]}
        except Exception as ex:
            last_err = {"error": "NETWORK", "detail": str(ex)[:500]}

    return last_err or {"error": "UNKNOWN", "detail": "所有 Key 均失败"}


def main():
    parser = argparse.ArgumentParser(description="Gemini API Bridge for CC")
    parser.add_argument("prompt", nargs="?", help="提问内容")
    parser.add_argument("--model", "-m", default="gemini-2.5-flash",
                        help=f"模型名或别名: {', '.join(MODELS.keys())}")
    parser.add_argument("--search", "-s", action="store_true", help="启用Google搜索接地")
    parser.add_argument("--json", "-j", action="store_true", help="要求JSON输出")
    parser.add_argument("--system", help="系统提示词")
    parser.add_argument("--image", help="图片路径（视觉理解）")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--raw", action="store_true", help="只输出文本，不包装")
    args = parser.parse_args()

    if not args.prompt:
        parser.print_help()
        print(f"\n可用模型别名: {json.dumps(MODELS, indent=2, ensure_ascii=False)}")
        return

    model = MODELS.get(args.model, args.model)

    result = call_gemini(
        prompt=args.prompt,
        model=model,
        search=args.search,
        json_mode=args.json,
        system_prompt=args.system or "",
        image_path=args.image,
        max_tokens=args.max_tokens,
    )

    if "error" in result:
        print(f"❌ {result['error']}: {result['detail'][:300]}", file=sys.stderr)
        sys.exit(1)

    if args.raw:
        print(result["text"])
    else:
        print(result["text"])
        if result.get("sources"):
            print("\n--- 参考来源 ---")
            for s in result["sources"]:
                print(f"  {s['title']}: {s['uri']}")


if __name__ == "__main__":
    main()
