#!/bin/bash
# cc-switch 每次切换供应商会把 ~/.claude/settings.json 里的 ANTHROPIC_BASE_URL
# 重写回 15721。跑这个脚本把它改回 vision-proxy 的端口。
set -e
PORT="${PORT:-8787}"
S="$HOME/.claude/settings.json"

python3 - "$S" "$PORT" <<'PY'
import json, sys, pathlib
path, port = pathlib.Path(sys.argv[1]), sys.argv[2]
cfg = json.loads(path.read_text("utf-8"))
env = cfg.setdefault("env", {})
old = env.get("ANTHROPIC_BASE_URL")
new = f"http://127.0.0.1:{port}"
if old == new:
    print(f"已经是 {new},无需改动")
else:
    env["ANTHROPIC_BASE_URL"] = new
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    print(f"{old}  ->  {new}")
    print("请重启 Claude Code 生效")
PY
