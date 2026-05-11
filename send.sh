#!/bin/bash
# 主动发飞书群消息 — 通过信号总线投递
# 用法: ./send.sh "消息"                 # 不@
#       ./send.sh @闪闪 "消息"           # @闪闪
#       ./send.sh @闪闪 @OC "消息"       # @多人
#       ./send.sh --all "消息"           # @所有人
set -euo pipefail

SIGNAL_BUS="$HOME/cc-workspace/signal_bus.py"
all_flag=""
at_users=()
text=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all) all_flag="--all"; shift ;;
        @*) at_users+=("$1"); shift ;;
        *) text="$1"; shift; break ;;
    esac
done

[ -z "$text" ] && { echo "用法: $0 [--all] [@用户...] <消息>"; exit 1; }

# 构建 summary（@信息拼入消息体）
if [ ${#at_users[@]} -gt 0 ]; then
    text="${at_users[*]} $text"
fi
[ -n "$all_flag" ] && text="@所有人 $text"

python3 "$SIGNAL_BUS" write \
    --type status \
    --topic cc-outgoing \
    --from CC \
    --to OC \
    --ball-with OC \
    --priority normal \
    --summary "$text"

echo "✓ 已入队: $text"
