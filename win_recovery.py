#!/usr/bin/env python3
"""
win_recovery.py — Persistent Agent 自愈回路
失败检测 → 现场取证 → 飞书求援 → 异步等待 → 断点续传

用法:
  from win_recovery import RecoveryGuard
  guard = RecoveryGuard(cua)
  result = guard.guarded_execute("click", {"x": 100, "y": 200})
"""

import json
import time
import os
import threading
from pathlib import Path
from datetime import datetime

# OC 原生通知模块
from oc_notify import rescue_notify as _oc_rescue_notify

# Phase 2: 策略路由表
from strategy_rules import match_rule as _strategy_match, apply_strategy as _strategy_apply
from strategy_rules import verify_action_effect as _verify_action, capture_pre_verify_state as _capture_pre_verify
from strategy_rules import overlay_safe_click as _overlay_safe_click, detect_overlays as _detect_overlays, clear_overlays as _clear_overlays
from decision_gate import DecisionGate
_decision_gate = DecisionGate()  # 跨任务共享,积累历史学习

# ── 配置 ──
STATE_DIR = Path(__file__).parent / "win_cua_state"
RESCUE_DIR = Path(__file__).parent / "win_cua_rescues"
SCREENSHOT_DIR = Path(__file__).parent / "win_cua_screenshots"
OC_INBOX = Path.home() / "cc-workspace" / "oc_inbox.jsonl"  # deprecated, migrated to signals.jsonl
SIGNALS_FILE = Path.home() / "cc-workspace" / "signals.jsonl"
OC_CURSOR = Path.home() / "cc-workspace" / "oc.cursor"
WIN_TOKEN = "jarvis8848"

for d in [STATE_DIR, RESCUE_DIR, SCREENSHOT_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[recovery {_ts()}] {msg}")


# ── 失败快照 ──

def create_failure_snapshot(task_context, action_name, params, result,
                             pre_state, screenshot_path, step_history):
    """生成结构化失败快照。"""
    return {
        "timestamp": datetime.now().isoformat(),
        "task": task_context,
        "failure": {
            "step_id": len(step_history) + 1,
            "action": action_name,
            "params": params,
            "error": result.get("detail", str(result)),
        },
        "pre_state": pre_state,
        "screenshot": screenshot_path,
        "step_history": step_history[-5:],
        "retry_count": 0,
        "status": "awaiting_rescue",
    }


def save_rescue_state(snapshot):
    """保存救援状态到文件，供后续恢复。"""
    rescue_id = f"rescue_{int(time.time())}"
    rescue_file = RESCUE_DIR / f"{rescue_id}.json"
    rescue_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    log(f"救援状态已保存: {rescue_file.name}")
    return rescue_id, rescue_file


# ── 飞书求援 ──

def _format_rescue_message(snapshot, rescue_id):
    """生成飞书求援消息。"""
    task = snapshot.get("task", "未知任务")
    failure = snapshot.get("failure", {})
    step = failure.get("step_id", "?")
    action = failure.get("action", "?")
    error = failure.get("error", "?")

    return json.dumps({
        "text": (
            f"⚠️ Agent 任务阻塞\n"
            f"任务: {task}\n"
            f"故障点: 步骤{step} {action} → {error}\n"
            f"状态: 已进入等待模式\n"
            f"救援ID: {rescue_id}\n"
            f"请回复指令 (如: 重试 / 跳过 / 点击xxx / 按F5刷新 / ...)"
        ),
        "at_users": [],
        "at_all": False,
        "rescue_request": True,
        "rescue_id": rescue_id,
    }, ensure_ascii=False)


def push_rescue_request(snapshot, rescue_id):
    """多通道推送：OC原生飞书(主/WS) + CC飞书(备) + Control UI + OC收件箱。"""
    data = json.loads(_format_rescue_message(snapshot, rescue_id))
    text = data["text"]
    delivered = False

    chat_id = _load_chat_id()

    # 通道1(主): OC原生飞书 + Control UI (via Gateway WS)
    if chat_id:
        try:
            result = _oc_rescue_notify(
                to=chat_id, text=text, rescue_id=rescue_id,
                screenshot_path=snapshot.get("screenshot", ""),
            )
            if result.get("feishu", {}).get("ok"):
                log(f"→ OC飞书(主/WS): rescue_id={rescue_id}")
                delivered = True
            else:
                feishu_err = result.get("feishu", {}).get("error", "?")
                log(f"OC飞书(WS)失败: {feishu_err}")
        except Exception as e:
            log(f"OC飞书(WS)异常: {e}")
    else:
        log("OC飞书跳过: 无chat_id")

    # 通道2(备): 信号总线 (via signals.jsonl)
    try:
        import uuid
        from datetime import datetime, timezone
        signal = {
            "id": f"sig_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": "alert",
            "topic": "win-rescue",
            "from": "OC",
            "to": "闪闪",
            "ball_with": "闪闪",
            "priority": "critical",
            "summary": data.get("summary", f"Win rescue: {rescue_id}"),
            "evidence": [{"rescue_id": rescue_id, "type": data.get("type", "?")}],
            "expires_at": None,
        }
        SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SIGNALS_FILE, "a") as f:
            f.write(json.dumps(signal, ensure_ascii=False) + "\n")
        log(f"→ 信号总线(备): rescue_id={rescue_id}")
        delivered = True
    except Exception as e:
        log(f"信号总线失败: {e}")

    # 通道3: OC收件箱 (供OC轮询/恢复)
    try:
        oc_msg = json.dumps({
            "ts": time.time(),
            "text": text,
            "rescue_request": True,
            "rescue_id": rescue_id,
            "source": "win_recovery",
        }, ensure_ascii=False)
        with open(OC_INBOX, "a") as f:
            f.write(oc_msg + "\n")
    except Exception as e:
        log(f"OC inbox失败: {e}")

    return delivered


def _load_chat_id():
    """尝试获取飞书群 chat_id"""
    # 从 handoff 文件读取
    handoff = Path.home() / "cc-workspace" / "session_handoff.json"
    if handoff.exists():
        try:
            data = json.loads(handoff.read_text())
            return data.get("chat_id", "")
        except Exception:
            pass
    return ""


# ── 等待救援（轮询 signals.jsonl via oc.cursor）──

def wait_for_rescue(rescue_id, timeout=600):
    """轮询信号总线 (signals.jsonl via oc.cursor)，等待用户回复救援指令。

    返回: (instruction_text, sender) 或 (None, None) 超时
    """
    log(f"进入等待救援模式: {rescue_id} (超时={timeout}s)")

    # 记录当前 cursor 位置
    cursor = 0
    if OC_CURSOR.exists():
        try:
            cursor = int(OC_CURSOR.read_text().strip() or 0)
        except ValueError:
            cursor = 0
    except Exception:
        init_pos = 0

    deadline = time.time() + timeout
    check_interval = 2.0

    while time.time() < deadline:
        time.sleep(check_interval)

        try:
            if not SIGNALS_FILE.exists():
                continue

            # 读取 signals.jsonl，解析行数
            line_count = sum(1 for _ in open(SIGNALS_FILE))
            if line_count <= cursor:
                continue

            # 读取新增信号
            with open(SIGNALS_FILE) as f:
                for i, line in enumerate(f):
                    if i < cursor:
                        continue
                    try:
                        entry = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue

                    # 只处理球在 OC 的信号
                    if entry.get("ball_with") != "OC":
                        continue

                    summary = entry.get("summary", "")
                    sender = entry.get("from", "?")
                    decision = _parse_rescue_reply(summary, rescue_id, sender)
                    if decision:
                        OC_CURSOR.write_text(str(line_count))
                        log(f"收到救援指令: [{sender}] {decision['action']} (P0)")
                        return decision, sender

            # 更新 cursor 到最新
            cursor = line_count
            OC_CURSOR.write_text(str(cursor))

        except Exception as e:
            log(f"信号轮询异常: {e}")
            continue

    log(f"救援超时: {rescue_id}")
    return None, None


def _parse_rescue_reply(text, rescue_id, sender="?"):
    """从用户回复中提取救援指令，返回结构化决策记录。

    返回格式 (Phase 2 seed — 为 Decision Gate 预留插槽):
      {"action": "retry", "source": "Boss_Feishu", "priority": "P0",
       "raw_reply": "...", "sender": "...", "timestamp": ...}
      或 None (无法识别)

    优先级规则 (当前):
      - 人类飞书回复 → P0 (Boss)
      - 未来: P1 预设规则, P2 OC自主决策
    """
    t = text.strip().lower()

    if not t:
        return None

    rescue_keywords = ("重试", "重来", "retry", "跳过", "skip",
                       "继续", "点", "按", "输入", "刷新", "f5",
                       "取消", "放弃", "回退", "back", "上面的",
                       "左边", "右边", "下面", "最小化", "最大化",
                       "关掉", "关闭", "重新打开", "重启")

    if not any(kw in t for kw in rescue_keywords):
        return None

    # 映射标准指令
    if any(w in t for w in ("重试", "重来", "retry")):
        action = "retry"
    elif any(w in t for w in ("跳过", "skip", "继续")):
        action = "skip"
    elif any(w in t for w in ("取消", "放弃")):
        action = "abort"
    elif any(w in t for w in ("刷新", "f5")):
        action = "press:F5"
    elif any(w in t for w in ("回退", "back")):
        action = "press:Alt+Left"
    elif any(w in t for w in ("关闭", "关掉")):
        action = "press:Ctrl+W"
    elif any(w in t for w in ("最小化",)):
        action = "press:Win+Down"
    elif any(w in t for w in ("最大化",)):
        action = "press:Win+Up"
    else:
        action = text.strip()

    # ── Decision Gate 种子：结构化决策记录 ──
    return {
        "action": action,
        "source": f"{sender}_Feishu",          # Phase 2: 可扩展 source 枚举
        "priority": "P0",                        # Phase 3: 裁决层权重比较
        "raw_reply": text.strip(),
        "sender": sender,
        "timestamp": time.time(),
    }



# ── 执行救援指令 ──

def execute_rescue(cua, decision, snapshot):
    """执行救援决策并返回是否应重试原操作。

    decision: _parse_rescue_reply 返回的结构化 dict
      或兼容旧格式的字符串 (向后兼容)
    """
    # 向后兼容：如果是旧格式字符串，包装为 dict
    if isinstance(decision, str):
        decision = {"action": decision, "source": "unknown", "priority": "P2",
                    "raw_reply": decision, "sender": "?", "timestamp": time.time()}

    action = decision.get("action", decision)
    log(f"执行救援: {action} (source={decision.get('source','?')} priority={decision.get('priority','?')})")

    if action == "retry":
        return {"action": "retry", "resume": True}

    if action == "skip":
        return {"action": "skip", "resume": True}

    if action == "abort":
        return {"action": "abort", "resume": False}

    if action.startswith("press:"):
        key = action.split(":", 1)[1]
        log(f"  按键: {key}")
        from win_bridge import WinBridge
        wb = WinBridge(token="jarvis8848")
        wb.shell(f'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys(\'{key}\')"',
                 timeout=10)
        return {"action": "press", "key": key, "resume": True}

    # 未识别的指令：原样记录，让外部决策
    return {"action": "unknown", "instruction": action, "resume": False}


# ── 前置状态采集 ──

def capture_pre_state(cua):
    """采集操作前的状态快照。"""
    try:
        state = cua.browser_state()
        return {
            "url": state.get("url", ""),
            "title": state.get("title", ""),
            "time": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"error": str(e), "time": datetime.now().isoformat()}


# ── 失败检测 ──

def detect_failure(action_name, result, pre_state=None, cua=None):
    """检测动作是否失败。

    失败条件:
    - result status 为 error
    - 浏览器 URL 变成 about:blank 或错误页
    - 页面无响应
    - 点击/输入类操作后，页面内容无变化（detected_stall）
    """
    if result.get("status") == "error":
        return True, result.get("detail", "action error")

    detail = str(result.get("detail", "")).lower()
    if any(w in detail for w in ("error", "fail", "not found", "timeout", "no element")):
        return True, detail

    # 检测页面是否崩溃
    if cua and action_name not in ("browser_state", "screenshot", "page_eval",
                                     "page_text", "clipboard_get", "wait"):
        try:
            state = cua.browser_state()
            url = state.get("url", "")
            if url and ("error" in url.lower() or "about:blank" in url):
                return True, f"page crashed: {url}"
        except Exception:
            pass

    # 交互类动作：检测是否产生页面变化
    interactive_actions = ("click", "type", "press", "scroll")
    if action_name in interactive_actions and result.get("status") == "ok":
        result["_may_be_noop"] = True
        # 标记为"待验证"——实际落地效果需由调用方确认
        # 单一动作无法自动判断是否真的"点到了东西"

    return False, None


# ═══════════════════════════════════════════════════════
# 自愈守卫（主类）
# ═══════════════════════════════════════════════════════

class RecoveryGuard:
    """包装 WinCUA，注入自愈能力。"""

    def __init__(self, cua, task_context=""):
        self.cua = cua
        self.task_context = task_context
        self.step_history = []
        self.rescues = []

    def guarded_execute(self, action_name, params=None):
        """执行单个动作，失败时触发自愈回路 + 事后效果验证。"""
        params = params or {}
        step_num = len(self.step_history) + 1

        # 1. 采集前置状态 (含文本快照用于事后对比)
        pre_state = capture_pre_state(self.cua)
        pre_verify = _capture_pre_verify(self.cua)

        # 1.5 ── Phase 2 场景C: 弹窗遮挡预检 (click类动作) ──
        overlay_result = {}
        if action_name in ("click", "click_selector"):
            overlay_check = _detect_overlays(self.cua)
            if overlay_check.get("count", 0) > 0:
                log(f"  [弹窗预检] 检测到 {overlay_check['count']} 个浮层元素")
                for ov in overlay_check.get("overlays", [])[:3]:
                    log(f"    z={ov['zIndex']} {ov['tag']}.{ov.get('cls','')[:30]} "
                        f"({ov['rect']['w']}x{ov['rect']['h']}) fullScreen={ov.get('fullScreen')}")
                # 尝试清理
                clear_result = _clear_overlays(self.cua)
                if clear_result.get("cleared"):
                    log(f"  [弹窗清理] ✓ 已清除 (策略: {clear_result.get('strategy_used')})")
                else:
                    log(f"  [弹窗清理] ✗ 无法清除 (已尝试: {clear_result.get('strategies_tried',[])})")
                overlay_result = {"overlays_found": overlay_check["count"],
                                  "cleared": clear_result.get("cleared"),
                                  "strategy": clear_result.get("strategy_used")}
            else:
                overlay_result = {"overlays_found": 0}

        # 2. 执行
        log(f"步骤{step_num}: {action_name}({params})")
        result = self.cua.execute(action_name, params)

        # 3. 失败检测
        is_failed, reason = detect_failure(action_name, result, pre_state, self.cua)

        if not is_failed:
            result["step"] = step_num
            result["_overlay"] = overlay_result
            # ── Phase 2 场景B: 事后效果验证 ──
            verify_result = _verify_action(action_name, params, pre_verify, self.cua)
            result["_verify"] = verify_result
            if verify_result.get("ghost_success"):
                log(f"  ⚠ 假阳性检测: {verify_result.get('evidence','')[:100]}")
                result["_ghost_success"] = True
                self.step_history.append({
                    "step": step_num, "action": action_name,
                    "params": params, "status": "ghost_success",
                    "verify": verify_result
                })
            elif verify_result.get("weak"):
                log(f"  ~ 弱信号: {verify_result.get('evidence','')[:80]}")
                self.step_history.append({
                    "step": step_num, "action": action_name,
                    "params": params, "status": "ok_weak", "verify": verify_result
                })
            else:
                log(f"  ✓ 验证通过: {verify_result.get('evidence','')[:80]}")
                self.step_history.append({
                    "step": step_num, "action": action_name,
                    "params": params, "status": "ok", "verify": verify_result
                })
            return result

        # 4. 失败处理：现场取证
        log(f"  检测到失败: {reason}")
        result["step"] = step_num
        result["failed"] = True
        self.step_history.append({
            "step": step_num, "action": action_name,
            "params": params, "status": "failed", "reason": reason
        })

        # 截图取证
        screenshot_path = None
        try:
            r = self.cua.execute("screenshot")
            screenshot_path = r.get("screenshot_path", "")
        except Exception:
            pass

        # 构建快照
        snapshot = create_failure_snapshot(
            self.task_context, action_name, params,
            result, pre_state, screenshot_path, self.step_history
        )

        # ── Phase 2.5: Decision Gate —— 先裁决，再行动 ──
        domain = pre_state.get("url", "")
        rule, score = _strategy_match(
            action=action_name, domain=domain, error_msg=reason,
        )
        strategy_match = (rule, score) if rule else (None, 0)

        verify_result = None  # 失败时没有post-verify，但有pre-state可用于分析
        retry_count = snapshot.get("retry_count", 0)

        gate_decision = _decision_gate.decide(
            failure_snapshot=snapshot,
            strategy_match=strategy_match if rule else None,
            verify_result=verify_result,
            overlay_result=overlay_result if overlay_result.get("overlays_found", 0) > 0 else None,
            retry_count=retry_count,
            error_msg=reason,
            action_name=action_name,
            domain=domain,
        )

        log(f"  [Decision Gate] → {gate_decision['decision']} "
            f"(P{gate_decision['priority'][1]} conf={gate_decision['confidence']:.2f})")
        for r_line in gate_decision.get("reasoning", []):
            log(f"    ↳ {r_line}")

        snapshot["gate_decision"] = gate_decision
        result["_gate_decision"] = gate_decision

        # ── 执行裁决 ──
        if gate_decision["decision"] == "auto_apply_strategy":
            # P1: 已知策略 → 自动执行，不求援
            strategy_result = _strategy_apply(self.cua, rule, snapshot)
            log(f"  策略自动执行: {rule['id']} → {strategy_result.get('action')} ok={strategy_result.get('ok')}")

            if strategy_result.get("ok"):
                strat_verify = _verify_action(action_name, params, pre_verify, self.cua)
                strategy_result["_verify"] = strat_verify
                _decision_gate.record_outcome(gate_decision, True,
                                              context=f"{rule['id']} @ {domain}")
                self.step_history[-1]["status"] = "recovered_via_strategy"
                self.step_history[-1]["strategy"] = rule["id"]
                self.step_history[-1]["verify"] = strat_verify
                result["recovered"] = True
                result["_strategy"] = rule["id"]
                result["_verify"] = strat_verify
                return result
            else:
                _decision_gate.record_outcome(gate_decision, False,
                                              context=f"{rule['id']} @ {domain}")
                log(f"  策略失败: {strategy_result.get('suggestion','')} → 升级人工")
                # Fall through to escalate

        elif gate_decision["decision"] == "auto_abort":
            # P1/P2: 重试无意义 → 自动跳过
            log(f"  自动跳过: {gate_decision.get('action','abort')}")
            _decision_gate.record_outcome(gate_decision, True, context="auto_abort")
            self.step_history[-1]["status"] = "auto_aborted"
            self.step_history[-1]["gate_reason"] = gate_decision.get("reasoning", [])
            result["recovered"] = False
            result["_auto_aborted"] = True
            return result

        elif gate_decision["decision"] == "auto_retry":
            # P2: 环境清理后或瞬态错误 → 自动重试1次
            log(f"  自动重试…")
            snapshot["retry_count"] = retry_count + 1
            _decision_gate.record_outcome(gate_decision, True, context="auto_retry")
            retry_result = self.cua.execute(action_name, params)
            retry_result["step"] = step_num
            retry_result["recovered"] = True
            retry_verify = _verify_action(action_name, params, pre_verify, self.cua)
            retry_result["_verify"] = retry_verify
            self.step_history[-1]["status"] = "auto_retried"
            self.step_history[-1]["verify"] = retry_verify
            return retry_result

        # ── P0: 需要人工介入 ──
        # 5. 保存救援状态
        rescue_id, rescue_file = save_rescue_state(snapshot)

        # 6. 推送飞书求援
        push_rescue_request(snapshot, rescue_id)

        # 7. 等待救援
        instruction, sender = wait_for_rescue(rescue_id)

        # 8. 执行救援指令
        if instruction:
            rescue_result = execute_rescue(self.cua, instruction, snapshot)
            snapshot["decision"] = instruction if isinstance(instruction, dict) else {"action": instruction}
            snapshot["rescue_result"] = rescue_result
            snapshot["status"] = "rescued" if rescue_result.get("resume") else "aborted"
            rescue_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))

            if rescue_result.get("resume"):
                # 人类选择了retry → 执行原操作
                log(f"  人类指令: retry步骤{step_num}")
                retry_result = self.cua.execute(action_name, params)
                retry_result["step"] = step_num
                retry_result["recovered"] = True
                retry_verify = _verify_action(action_name, params, pre_verify, self.cua)
                retry_result["_verify"] = retry_verify
                if retry_verify.get("ghost_success"):
                    log(f"  ⚠ 重试假阳性: {retry_verify.get('evidence','')[:80]}")
                self.step_history[-1]["status"] = "recovered"
                self.step_history[-1]["verify"] = retry_verify
                return retry_result

        # 放弃或超时
        snapshot["status"] = "unresolved"
        rescue_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return result

    def get_recovery_report(self):
        """返回自愈历史报告。"""
        total = len(self.step_history)
        failures = sum(1 for s in self.step_history if s.get("status") == "failed")
        recovered = sum(1 for s in self.step_history if s.get("status") == "recovered")
        return {
            "total_steps": total,
            "failures": failures,
            "recovered": recovered,
            "recovery_rate": f"{recovered/max(failures,1)*100:.0f}%",
            "rescues": len(self.rescues),
        }


# ── 独立测试入口 ──
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from win_cua import WinCUA

    cua = WinCUA()
    guard = RecoveryGuard(cua, task_context="自愈回路测试")

    print("=== Recovery Guard 就绪 ===")
    print(f"救援目录: {RESCUE_DIR}")
    print(f"截图目录: {SCREENSHOT_DIR}")
    print()

    # 测试健康检查
    try:
        ping = cua.wb.ping()
        print(f"win_agent 连通: {ping.get('hostname', '?')}")
    except Exception as e:
        print(f"win_agent 不通: {e}")
        sys.exit(1)

    # 测试：打开网页
    result = guard.guarded_execute("open_url", {"url": "https://httpbin.org/status/404"})
    print(f"\n结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
    print(f"\n恢复报告: {json.dumps(guard.get_recovery_report(), ensure_ascii=False, indent=2)}")
