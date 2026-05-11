#!/usr/bin/env python3
"""
win_cua.py — Windows Computer Use Agent
Natural language → GUI actions on Windows.
Inspired by Mano-P architecture, built on win_agent.
"""

import sys
import json
import time
import base64
import hashlib
import random
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from win_bridge import WinBridge

CONFIG = {
    "log_file": str(Path(__file__).parent / "win_cua.log"),
    "state_dir": str(Path(__file__).parent / "win_cua_state"),
    "screenshot_dir": str(Path(__file__).parent / "win_cua_screenshots"),
    "max_steps": 10,
    "step_delay_min": 0.5,
    "step_delay_max": 2.0,
}

for d in [CONFIG["state_dir"], CONFIG["screenshot_dir"]]:
    Path(d).mkdir(parents=True, exist_ok=True)

ACTION_SCHEMA = {
    "click": {"desc": "Click at coordinates (in browser)", "params": ["x", "y"]},
    "click_selector": {"desc": "Click by CSS selector or text= keyword", "params": ["selector", "index"]},
    "fill": {"desc": "Fill input/textarea by selector", "params": ["selector", "value"]},
    "type": {"desc": "Type text into focused element", "params": ["text"]},
    "press": {"desc": "Press a key", "params": ["key"]},
    "scroll": {"desc": "Scroll page", "params": ["delta"]},
    "open_url": {"desc": "Open URL in browser", "params": ["url"]},
    "screenshot": {"desc": "Take screenshot, returns path", "params": []},
    "clipboard_get": {"desc": "Read clipboard text", "params": []},
    "clipboard_set": {"desc": "Set clipboard text", "params": ["text"]},
    "page_text": {"desc": "Get current page text content", "params": []},
    "page_eval": {"desc": "Run JS in browser page", "params": ["js"]},
    "send_message": {"desc": "Send IM message via React fiber handler chain", "params": ["text"]},
    "browser_state": {"desc": "Get current URL and title", "params": []},
    "wait": {"desc": "Wait N seconds", "params": ["seconds"]},
}

def log(msg):
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(CONFIG["log_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

class WinCUA:
    """Windows Computer Use Agent."""

    def __init__(self, token="jarvis8848", timeout=30):
        self.wb = WinBridge(token=token, timeout=timeout)
        self.task_id = None
        self.steps = []

    # ── Core Actions ────────────────────────────────

    def execute(self, action, params=None):
        params = params or {}
        result = {"action": action, "status": "ok"}

        try:
            if action == "click":
                x, y = int(params["x"]), int(params["y"])
                js = (
                    "(function(){"
                    "var e=document.elementFromPoint(" + str(x) + "," + str(y) + ");"
                    "if(e){e.click();return 'clicked ' + e.tagName + '.' + (e.className||'').substring(0,20);}"
                    "return 'no element at point';"
                    "})()"
                )
                r = self.wb.browser_eval(js)
                result["detail"] = "clicked (" + str(x) + "," + str(y) + "): " + str(r.get("result", r))

            elif action == "click_selector":
                selector = params["selector"]
                index = int(params.get("index", 0))
                navigate = bool(params.get("navigate", False))
                js = self._make_click_js(selector, index, navigate)
                r = self.wb.browser_eval(js)
                result["detail"] = str(r.get("result", r))[:200]
                try:
                    parsed = json.loads(str(r.get("result", r)))
                    result["element"] = parsed
                except Exception:
                    pass

            elif action == "fill":
                selector = params["selector"]
                value = (params.get("value") or params.get("text") or "")
                js = self._make_fill_js(selector, value)
                self.wb.browser_eval(js)
                result["detail"] = "filled: " + selector + " = " + value[:60]

            elif action == "type":
                text = params["text"]
                escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
                js = (
                    "(function(){"
                    "var el=document.activeElement||document.querySelector('input,textarea,[contenteditable=true]');"
                    "if(!el)return 'no input';"
                    "var s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
                    "s.call(el,'" + escaped + "');"
                    "el.dispatchEvent(new Event('input',{bubbles:true}));"
                    "return 'typed';"
                    "})()"
                )
                self.wb.browser_eval(js)
                result["detail"] = "typed: " + text[:60]

            elif action == "press":
                key = params["key"]
                js = (
                    "(function(){"
                    "var keyCodes={'Enter':13,'Tab':9,'Escape':27,'Backspace':8,'ArrowUp':38,'ArrowDown':40,'ArrowLeft':37,'ArrowRight':39};"
                    "var kc=keyCodes['" + key + "']||0;"
                    "var el=document.activeElement||document.body;"
                    "var opts={key:'" + key + "',keyCode:kc,which:kc,code:'" + key + "',bubbles:true,cancelable:true};"
                    "el.dispatchEvent(new KeyboardEvent('keydown',opts));"
                    "if(kc===13||(kc>=32&&kc<=126)) el.dispatchEvent(new KeyboardEvent('keypress',opts));"
                    "el.dispatchEvent(new KeyboardEvent('keyup',opts));"
                    "return 'pressed:'+el.tagName;"
                    "})()"
                )
                self.wb.browser_eval(js)
                result["detail"] = "pressed: " + key

            elif action == "scroll":
                delta = int(params.get("delta", 300))
                self.wb.browser_eval("window.scrollBy(0," + str(delta) + ")")
                result["detail"] = "scrolled " + str(delta) + "px"

            elif action == "open_url":
                self.wb.browser_open(params["url"])
                result["detail"] = "opened: " + params["url"]

            elif action == "screenshot":
                r = self.wb.browser_screenshot()
                if r.get("data_base64"):
                    fname = CONFIG["screenshot_dir"] + "/shot_" + str(int(time.time())) + ".png"
                    Path(fname).write_bytes(base64.b64decode(r["data_base64"]))
                    result["screenshot_path"] = fname
                    result["detail"] = "screenshot: " + fname
                else:
                    result["status"] = "error"
                    result["detail"] = "screenshot failed"

            elif action == "clipboard_get":
                text = self.wb.clipboard_get()
                if isinstance(text, dict):
                    text = text.get("text", "")
                result["text"] = text
                result["detail"] = "clipboard: " + (text[:50] if text else "empty")

            elif action == "clipboard_set":
                self.wb.clipboard_set(params["text"])
                result["detail"] = "clipboard set"

            elif action == "page_text":
                r = self.wb.browser_eval(
                    "document.body?document.body.innerText.substring(0,10000):''"
                )
                text = r.get("result", str(r)) if isinstance(r, dict) else str(r)
                result["text"] = text
                result["detail"] = "page_text: " + str(len(text)) + " chars"

            elif action == "page_eval":
                r = self.wb.browser_eval(params["js"])
                result["output"] = str(r.get("result", r))[:2000]
                result["detail"] = "eval done"

            elif action == "browser_state":
                try:
                    url_r = self.wb.browser_eval("window.location.href")
                    title_r = self.wb.browser_eval("document.title")
                    result["url"] = url_r.get("result", "") if isinstance(url_r, dict) else str(url_r)
                    result["title"] = title_r.get("result", "") if isinstance(title_r, dict) else str(title_r)
                except Exception:
                    result["url"] = "unknown"
                    result["title"] = "unknown"
                result["detail"] = "state: " + result.get("url", "")

            elif action == "send_message":
                text = params.get("text") or params.get("value") or ""
                js = self._make_send_js(text)
                r = self.wb.browser_eval(js)
                result["detail"] = "sent: " + text[:60]
                try:
                    parsed = json.loads(str(r.get("result", r)))
                    result["send_result"] = parsed
                except Exception:
                    pass

            elif action == "wait":
                sec = float(params.get("seconds", 1))
                time.sleep(sec)
                result["detail"] = "waited " + str(sec) + "s"

            else:
                result["status"] = "error"
                result["detail"] = "unknown action: " + action

        except Exception as e:
            result["status"] = "error"
            result["detail"] = str(e)

        return result

    # ── Selector Helpers ────────────────────────────

    def _make_click_js(self, selector, index=0, navigate=False):
        nav_js = "if(el.href&&el.tagName==='A'){window.location.href=el.href;}" if navigate else ""
        common = (
            "var r=el.getBoundingClientRect();"
            "var cx=Math.round(r.left+r.width/2);"
            "var cy=Math.round(r.top+r.height/2);"
            "var opts={bubbles:true,cancelable:true,clientX:cx,clientY:cy};"
            "el.dispatchEvent(new MouseEvent('mousedown',opts));"
            "el.dispatchEvent(new MouseEvent('mouseup',opts));"
            "el.dispatchEvent(new MouseEvent('click',opts));"
            "el.click();" + nav_js +
            "return JSON.stringify({ok:true,tag:el.tagName,"
            " href:(el.href||'').substring(0,80),"
            " text:(el.innerText||el.textContent||'').substring(0,30).trim(),"
            " cx:cx,cy:cy,w:Math.round(r.width),h:Math.round(r.height)});"
        )
        if selector.startswith("text="):
            text_val = json.dumps(selector[5:])
            return (
                "(function(){"
                "var target=" + text_val + ";"
                "var exact=target.indexOf('*')===-1;"
                "var pattern=exact?null:new RegExp(target.replace(/\\*/g,'.*'));"
                "var idx=" + str(index) + ";"
                "var el=null;"
                "var candidatePools=["
                  "'a,button',"
                  "'[role=\"button\"],[class*=\"btn\"],[class*=\"action\"]',"
                  "'span,[class*=\"tab\"],[class*=\"item\"],[class*=\"nav\"]'"
                "];"
                "for(var p=0;p<candidatePools.length&&el===null;p++){"
                  "var els=document.querySelectorAll(candidatePools[p]);"
                  "for(var i=0;i<els.length;i++){"
                    "var n=els[i];"
                    "var t=(n.innerText||n.textContent||'').trim();"
                    "if(!t||t.length>60) continue;"
                    "var matched=exact?(t===target):pattern.test(t);"
                    "if(!matched) continue;"
                    "var r2=n.getBoundingClientRect();"
                    "if(r2.width===0&&r2.height===0) continue;"
                    "if(r2.x<0||r2.y<0) continue;"
                    "el=n;"
                    "if(idx===0) break;"
                    "idx--;"
                  "}"
                "}"
                "if(!el) return JSON.stringify({ok:false,error:'no match for text='+target});"
                "var climb=el;"
                "for(var d=0;d<5&&climb;d++){"
                  "if(climb.tagName==='A'&&climb.href){el=climb;break;}"
                  "if(climb.tagName==='BUTTON'){el=climb;break;}"
                  "climb=climb.parentElement;"
                "}"
                + common +
                "})()"
            )
        else:
            sel_esc = selector.replace("\\", "\\\\").replace("'", "\\'").replace("%", "%%")
            return (
                "(function(){"
                "var els=document.querySelectorAll('%s');"
                "if(!els.length) return JSON.stringify({ok:false,error:'no match for: %s'});"
                "var idx=%d;"
                "if(idx>=els.length) idx=els.length-1;"
                "var el=els[idx];"
                "el.focus();"
                + common +
                "})()"
            ) % (sel_esc, sel_esc, index)

    def _make_fill_js(self, selector, value):
        sel_esc = selector.replace("\\", "\\\\").replace("'", "\\'")
        val_esc = json.dumps(value)
        return (
            "(function(){"
            "var el=document.querySelector('%s');"
            "if(!el) return 'no match: %s';"
            "var nativeSetter=Object.getOwnPropertyDescriptor("
            "  el.tagName==='TEXTAREA'||el.tagName==='INPUT'"
            "    ? HTMLInputElement.prototype"
            "    : HTMLElement.prototype"
            "  ,'value').set;"
            "nativeSetter.call(el,%s);"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));"
            "return 'filled';"
            "})()"
        ) % (sel_esc, sel_esc, val_esc)

    def _make_send_js(self, text):
        val_esc = json.dumps(text)
        js_lines = [
            "(function(){",
            "var t=document.querySelector('textarea');",
            "if(!t)return JSON.stringify({ok:false,error:'no textarea'});",
            # Set value via native setter (React needs this for controlled components)
            "var ns=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;",
            "ns.call(t," + val_esc + ");",
            # Update React state: input event chain
            "t.dispatchEvent(new InputEvent('input',{bubbles:true,cancelable:true,inputType:'insertText',data:" + val_esc + "}));",
            "t.dispatchEvent(new Event('change',{bubbles:true}));",
            # Find fiber and call onKeyDown with Enter key (proven working approach)
            "var fk=Object.keys(t).find(function(k){return k.startsWith('__reactFiber');});",
            "if(!fk)return JSON.stringify({ok:false,error:'no fiber key'});",
            "var fiber=t[fk];",
            "var depth=0,target=null;",
            "while(fiber&&depth<20){",
            "  var p=fiber.memoizedProps||{};",
            "  if(p.onKeyDown){target=fiber;break;}",
            "  fiber=fiber.return;depth++;",
            "}",
            "if(!target)return JSON.stringify({ok:false,error:'no onKeyDown handler'});",
            "var handler=target.memoizedProps.onKeyDown;",
            "var e=new KeyboardEvent('keydown',{key:'Enter',keyCode:13,which:13,code:'Enter',shiftKey:false,ctrlKey:false,altKey:false,metaKey:false,bubbles:true,cancelable:true});",
            "handler(e);",
            "return JSON.stringify({ok:true,text:" + val_esc + ".substring(0,50),fiberDepth:depth});",
            "})()",
        ]
        return "".join(js_lines)

    # ── Convenience Methods ─────────────────────────

    def screenshot(self, full_page=False):
        try:
            r = self.wb.browser_screenshot(full_page=full_page)
            if r.get("data_base64"):
                fname = CONFIG["screenshot_dir"] + "/shot_" + str(int(time.time())) + ".png"
                Path(fname).write_bytes(base64.b64decode(r["data_base64"]))
                return fname
        except Exception as e:
            log("Screenshot error: " + str(e))
        return None

    def page_content(self):
        try:
            r = self.wb.browser_eval(
                "document.body?document.body.innerText.substring(0,10000):''"
            )
            return r.get("result", str(r)) if isinstance(r, dict) else str(r)
        except Exception as e:
            return "[Error: " + str(e) + "]"

    def browser_state(self):
        try:
            url_r = self.wb.browser_eval("window.location.href")
            title_r = self.wb.browser_eval("document.title")
            return {
                "url": url_r.get("result", "") if isinstance(url_r, dict) else str(url_r),
                "title": title_r.get("result", "") if isinstance(title_r, dict) else str(title_r),
            }
        except Exception:
            return {"url": "unknown", "title": "unknown"}

    def analyze_screen(self, task, screenshot_path=None):
        """Take screenshot and return context for LLM analysis."""
        if not screenshot_path:
            screenshot_path = self.screenshot()
            if not screenshot_path:
                return {"error": "screenshot failed"}

        actions_list = "\n".join(
            "- " + name + ": " + info["desc"]
            for name, info in sorted(ACTION_SCHEMA.items())
        )
        prompt = (
            "## Screen Analysis Request\n\n"
            "**Task**: " + task + "\n\n"
            "**Available Actions**:\n" + actions_list + "\n\n"
            "**Screenshot**: " + screenshot_path + "\n\n"
            "Look at the screenshot and decide the next action.\n"
            "Reply with JSON: {\"action\": \"<name>\", \"params\": {...}}\n"
            "Or if done: {\"done\": true, \"summary\": \"...\"}\n"
            "Or if need more info: {\"action\": \"page_text\"} or {\"action\": \"screenshot\"}"
        )

        return {
            "screenshot_path": screenshot_path,
            "task": task,
            "prompt": prompt,
            "actions": sorted(ACTION_SCHEMA.keys()),
        }

    # ── Vision-Powered Task Runner ──────────────────

    def run_task_with_vision(self, task, max_steps=None):
        """
        Multi-step task with screenshot→vision→action loop.
        Uses Zhipu GLM-4V-Plus to see the screen and decide actions.
        """
        try:
            from vision import Vision
        except ImportError:
            return {"error": "vision module not available"}

        max_steps = max_steps or CONFIG["max_steps"]
        self.task_id = hashlib.md5(
            (task + str(time.time())).encode()
        ).hexdigest()[:12]
        results = []

        log(f"TASK-V [{self.task_id}]: {task}")

        try:
            vision = Vision(backend="glm")
        except Exception as e:
            return {"error": f"Vision backend unavailable: {e}"}

        for step_num in range(max_steps):
            log(f"  Step {step_num + 1}/{max_steps}")

            # 1. Take screenshot
            r = self.execute("screenshot")
            if r["status"] == "error":
                results.append(r)
                break
            screenshot_path = r.get("screenshot_path", "")

            # 2. Get page text as context
            page = self.page_content()
            context = task
            if page and len(page) > 10:
                context = f"Page text: {page[:2000]}\n\nTask: {task}"

            # 3. Ask vision model
            analysis = vision.look_and_act(screenshot_path, context)
            log(f"    Vision: {json.dumps(analysis, ensure_ascii=False)[:300]}")

            if analysis.get("error"):
                log(f"    Vision error: {analysis['error']}")
                results.append(analysis)
                # Try one more time with page text only
                if step_num == 0:
                    continue
                break

            if analysis.get("done"):
                log(f"  Done: {analysis.get('summary', '')}")
                results.append(analysis)
                break

            action = analysis.get("action")
            params = analysis.get("params", {})

            if not action:
                log("    No action from vision, stopping")
                results.append(analysis)
                break

            # 4. Execute action
            log(f"    Execute: {action}({params})")
            result = self.execute(action, params)
            result["step"] = step_num + 1
            result["vision_analysis"] = analysis.get("analysis", "")
            results.append(result)

            if result["status"] == "error":
                log(f"    Action error: {result.get('error', '')}")
                break

            time.sleep(random.uniform(
                CONFIG["step_delay_min"], CONFIG["step_delay_max"]
            ))

        # Save state
        save_path = Path(CONFIG["state_dir"]) / ("taskv_" + self.task_id + ".json")
        save_path.write_text(
            json.dumps({
                "task_id": self.task_id,
                "task": task,
                "mode": "vision",
                "steps": len([r for r in results if r.get("status") == "ok"]),
                "results": results,
                "ts": datetime.now().isoformat(),
            }, ensure_ascii=False, indent=2)
        )
        log(f"SAVED: {save_path}")
        return {"task_id": self.task_id, "mode": "vision", "steps": len(results), "results": results}

    # ── Text-Driven Task Runner (Callback) ───────────

    def run_task(self, task, max_steps=None, step_callback=None):
        """
        Run a multi-step task.
        step_callback(step_number, results_so_far, task) -> action_dict or None
        Returns {"task_id": ..., "steps": ..., "results": [...]}
        """
        max_steps = max_steps or CONFIG["max_steps"]
        self.task_id = hashlib.md5(
            (task + str(time.time())).encode()
        ).hexdigest()[:12]
        self.steps = []

        log("TASK [" + self.task_id + "]: " + task)
        results = []

        for step_num in range(max_steps):
            log("  Step " + str(step_num + 1) + "/" + str(max_steps))

            if step_callback:
                action = step_callback(step_num, results, task)
                if action is None:
                    log("  Stopped by callback")
                    break
                if isinstance(action, dict) and action.get("done"):
                    log("  Done: " + str(action.get("summary", "")))
                    results.append(action)
                    break
                if isinstance(action, dict) and action.get("error"):
                    log("  Error: " + str(action["error"]))
                    results.append(action)
                    break
            else:
                # Without callback, take screenshot and return for external analysis
                screenshot_r = self.execute("screenshot")
                results.append(screenshot_r)
                break

            if isinstance(action, dict) and "action" in action:
                result = self.execute(action["action"], action.get("params", {}))
                result["step"] = step_num + 1
                self.steps.append(result)
                results.append(result)

                if result["status"] == "error":
                    break

                time.sleep(random.uniform(
                    CONFIG["step_delay_min"], CONFIG["step_delay_max"]
                ))

        # Save state
        save_path = Path(CONFIG["state_dir"]) / ("task_" + self.task_id + ".json")
        save_path.write_text(
            json.dumps({
                "task_id": self.task_id,
                "task": task,
                "steps": len(self.steps),
                "results": results,
                "ts": datetime.now().isoformat(),
            }, ensure_ascii=False, indent=2)
        )

        log("SAVED: " + str(save_path))
        return {"task_id": self.task_id, "steps": len(self.steps), "results": results}


def get_skill_prompt():
    """Generate skill description for Claude Code integration."""
    actions = "\n".join(
        "| " + name + " | " + info["desc"] + " | `" + json.dumps(info["params"]) + "` |"
        for name, info in sorted(ACTION_SCHEMA.items())
    )
    return (
        "# Win-CUA: Windows Computer Use Agent\n\n"
        "Control a Windows PC through win_agent. "
        "Natural language → GUI actions.\n\n"
        "## Available Actions\n\n"
        "| Action | Description | Params |\n"
        "|--------|-------------|--------|\n" + actions + "\n\n"
        "## Usage\n\n"
        "```python\n"
        "from win_cua import WinCUA\n"
        "cua = WinCUA()\n\n"
        "# Single action\n"
        "cua.execute('open_url', {'url': 'https://goofish.com/im'})\n\n"
        "# Screenshot for visual analysis\n"
        "path = cua.screenshot()\n\n"
        "# Multi-step task with LLM-driven callback\n"
        "cua.run_task('reply to all unread Xianyu messages', step_callback=my_analyzer)\n"
        "```\n\n"
        "## Visual Analysis Pattern\n"
        "1. `screenshot` → save to file\n"
        "2. Read image, decide next action\n"
        "3. `execute(action, params)`\n"
        "4. Repeat until done\n\n"
        "## Safety\n"
        "- Confirm destructive actions with user\n"
        "- 0.5-2s delay between actions\n"
        "- Stop on unexpected errors"
    )


# ── CLI ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Win-CUA: Windows Computer Use Agent")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("screenshot", help="Take screenshot")
    sub.add_parser("page", help="Get page text")
    sub.add_parser("state", help="Get browser state")
    sub.add_parser("skill", help="Print skill prompt")
    sub.add_parser("health", help="Health check")

    run_p = sub.add_parser("run", help="Execute single action")
    run_p.add_argument("action", help="Action name")
    run_p.add_argument("--params", default="{}", help="JSON params string")

    task_p = sub.add_parser("task", help="Run multi-step task")
    task_p.add_argument("description", help="Task description")
    task_p.add_argument("--max-steps", type=int, default=10)

    args = p.parse_args()
    cua = WinCUA()

    if args.cmd == "screenshot":
        path = cua.screenshot()
        print("SCREENSHOT:", path)

    elif args.cmd == "page":
        print(cua.page_content())

    elif args.cmd == "state":
        print(json.dumps(cua.browser_state(), indent=2))

    elif args.cmd == "run":
        params = json.loads(args.params)
        print(json.dumps(cua.execute(args.action, params), indent=2, ensure_ascii=False))

    elif args.cmd == "task":
        r = cua.run_task(args.description, max_steps=args.max_steps)
        print(json.dumps(r, indent=2, ensure_ascii=False))

    elif args.cmd == "skill":
        print(get_skill_prompt())

    elif args.cmd == "health":
        try:
            ping = cua.wb.ping()
            state = cua.browser_state()
            print(json.dumps({"status": "healthy", "agent": ping, "browser": state}, indent=2, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"status": "unhealthy", "error": str(e)}))

    else:
        p.print_help()
