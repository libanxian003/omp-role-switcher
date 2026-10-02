# -*- coding: utf-8 -*-
"""omp 模型角色切换器 — 本地 web 小程序。

只读展示 modelRoles 与模型目录；仅在用户点击切换时，
以定向正则方式编辑 ~/.omp/agent/config.yml（保留注释与其余内容），
写入前自动备份。不使用 `omp config set`（会剥离注释）。
"""
import json
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path.home() / ".omp" / "agent" / "config.yml"
PORT = 8788

ROLE_COMMENT = {
    "default": "主会话模型",
    "smol": "轻量快速任务",
    "slow": "深度推理",
    "plan": "规划模式",
    "vision": "图像理解",
    "task": "子代理",
    "commit": "提交信息生成",
    "tiny": "极小分类/标题",
    "web": "网络搜索",
    "advisor": "顾问评审",
}


def run(args):
    p = subprocess.run(args, capture_output=True, text=True, shell=False,
                       encoding="utf-8", errors="replace")
    return p.stdout


def parse_roles():
    """直接解析 config.yml 的 modelRoles 块，避免依赖 omp 进程。"""
    roles = {}
    in_block = False
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if re.match(r"^modelRoles:\s*$", line):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^  (\S+):\s*(\S+)\s*$", line)
            if m:
                roles[m.group(1)] = m.group(2)
            elif line and not line.startswith(" "):
                break
    return roles


def list_models():
    out = run(["omp", "models", "--json"])
    try:
        models = json.loads(out)["models"]
    except (json.JSONDecodeError, KeyError):
        return []
    keep = []
    for m in models:
        if m.get("kind") != "chat":
            continue
        keep.append({
            "selector": m["selector"],
            "name": m.get("name") or m["id"],
            "provider": m.get("provider"),
            "contextWindow": m.get("contextWindow"),
            "reasoning": bool(m.get("reasoning")),
        })
    keep.sort(key=lambda x: (x["provider"] or "", x["name"].lower()))
    return keep


def set_role(role, value):
    role = role.strip()
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_@:,\.\-\/\*]+", role) or \
       not re.fullmatch(r"[A-Za-z0-9_@:,\.\-\/\*\?\[\]]+", value):
        return "illegal role or value"
    raw = CONFIG.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    backup = CONFIG.with_name(
        f"config.yml.bak-role-switcher-{time.strftime('%Y%m%d-%H%M%S')}")
    pat = re.compile(rf"^(  {re.escape(role)}:)[^\r\n]*(\r?\n)?$")
    replaced = False
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"  {role}: {value}{eol}"
            replaced = True
            break
    if not replaced:
        # 在 modelRoles: 行后插入
        for i, line in enumerate(lines):
            if re.match(r"^modelRoles:\s*(\r?\n)?$", line):
                lines.insert(i + 1, f"  {role}: {value}{eol}")
                replaced = True
                break
        if not replaced:
            return "modelRoles block not found"
    shutil.copy2(CONFIG, backup)
    CONFIG.write_bytes("".join(lines).encode("utf-8"))
    return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (Path(__file__).parent / "index.html")
                       .read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, json.dumps(
                {"roles": parse_roles(), "models": list_models(),
                 "config": str(CONFIG)}, ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if self.path != "/api/set":
            self._send(404, "{}")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
            err = set_role(payload["role"], payload["value"])
        except Exception as e:
            err = str(e)
        if err:
            self._send(400, json.dumps({"error": err}, ensure_ascii=False))
        else:
            self._send(200, json.dumps(
                {"ok": True, "roles": parse_roles()}, ensure_ascii=False))

    def log_message(self, format, *args):
        pass


def main():
    addr = ("127.0.0.1", PORT)
    srv = ThreadingHTTPServer(addr, Handler)
    url = f"http://{addr[0]}:{PORT}"
    print(f"omp role switcher -> {url}")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
