# -*- coding: utf-8 -*-
"""omp 设置中文 Web 界面（由模型角色切换器扩展而来）。

- 模型角色：两级分组选择器，点击切换 modelRoles。
- 全部设置：读取 `omp config list --json` 全量目录（键/值/类型/描述），
  标量项（布尔/数字/字符串/枚举）定向编辑 config.yml；数组与嵌套块只读展示。

安全设计：不用 `omp config set`（会重写整文件并剥离注释）；按字节读写
~/.omp/agent/config.yml，只替换/插入目标行并沿用原行尾；每次写入前自动备份。
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

HERE = Path(__file__).parent
CONFIG = Path.home() / ".omp" / "agent" / "config.yml"
PORT = 8788
EDITABLE_TYPES = {"string", "boolean", "number", "integer", "enum"}

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

KEY_LINE = re.compile(r'^([ \t]*)(.+?):(?:[ \t]([^\r\n]*))?(\r?\n)?$')
# 纯标量安全字符；其余一律双引号（JSON 转义是 YAML 双引号风格的子集）
PLAIN_SCALAR = re.compile(r'[A-Za-z0-9_@.,/\-]+(?::[A-Za-z0-9_@.,/\-]+)*')


def run(args):
    p = subprocess.run(args, capture_output=True, text=True, shell=False,
                       encoding="utf-8", errors="replace")
    return p.stdout


# ---------- config.yml 行级解析 ----------

def split_comment(v):
    """剥掉引号外的行尾注释，返回 (值, 注释)。"""
    inq = None
    for i, ch in enumerate(v):
        if inq:
            if ch == inq:
                inq = None
        elif ch in '"\'':
            inq = ch
        elif ch == '#' and (i == 0 or v[i - 1] in ' \t'):
            return v[:i].rstrip(), v[i:]
    return v.rstrip(), ''


def index_config(lines):
    """按缩进栈扫描，返回 {点路径: {line, kind: scalar|block}}。"""
    idx, stack = {}, []
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        m = KEY_LINE.match(line)
        if m is None:
            continue
        indent = len(m.group(1))
        key = m.group(2).strip().strip('"\'')
        val = (m.group(3) or '').strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = '.'.join(k for _, k in stack + [(indent, key)])
        idx[path] = {'line': i, 'kind': 'scalar' if val else 'block'}
        stack.append((indent, key))
    return idx


def yaml_scalar(s):
    if PLAIN_SCALAR.fullmatch(s) and \
       not re.fullmatch(r'(?i:true|false|null|yes|no|on|off|~|-?\d+(\.\d+)?)', s):
        return s
    return json.dumps(s, ensure_ascii=False)


def coerce_value(typ, s):
    s = s.strip()
    if typ == 'boolean':
        low = s.lower()
        if low in ('true', '是', '1', 'yes'):
            return 'true'
        if low in ('false', '否', '0', 'no'):
            return 'false'
        raise ValueError('布尔项只接受 true/false')
    if typ in ('number', 'integer'):
        if re.fullmatch(r'-?\d+(\.\d+)?([eE][-+]?\d+)?', s) is None:
            raise ValueError('该项要求数字')
        return s
    if s == '':
        raise ValueError('空值请用 null 表示')
    if s.lower() == 'null':
        return 'null'
    return yaml_scalar(s)


def write_backup():
    backup = CONFIG.with_name(
        f"config.yml.bak-role-switcher-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONFIG, backup)
    return backup


def set_config_key(key, formatted):
    raw = CONFIG.read_bytes().decode('utf-8')
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    idx = index_config(lines)
    if key in idx:
        info = idx[key]
        if info['kind'] != 'scalar':
            return '该项在 config.yml 中是块值（数组/嵌套），请手工编辑'
        m = KEY_LINE.match(lines[info['line']])
        if m is None:
            return '内部错误：行格式变化'
        comment = (m.group(4) or '')
        _, c = split_comment(m.group(3) or '')
        lines[info['line']] = f"{m.group(1)}{m.group(2)}: {formatted}{c}{comment}"
        if not lines[info['line']].endswith('\n'):
            lines[info['line']] += eol
    else:
        parts = key.split('.')
        target = None
        for depth in range(len(parts) - 1, 0, -1):
            p = '.'.join(parts[:depth])
            if p in idx:
                if idx[p]['kind'] != 'block':
                    return '父级是标量，无法插入子键'
                target = (idx[p]['line'], depth)
                break
        new = []
        base = target[1] if target else 0
        remaining = parts[base:] if target else parts
        for j, part in enumerate(remaining):
            ind = '  ' * (base + j)
            last = j == len(remaining) - 1
            new.append(f"{ind}{part}:{f' {formatted}' if last else ''}{eol}")
        if target:
            lines[target[0] + 1:target[0] + 1] = new
        else:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += eol
            lines.extend(new)
    write_backup()
    CONFIG.write_bytes(''.join(lines).encode('utf-8'))
    return None


# ---------- 读取 ----------

def parse_roles():
    roles, in_block = {}, False
    for line in CONFIG.read_bytes().decode('utf-8').splitlines():
        if re.match(r'^modelRoles:\s*$', line):
            in_block = True
            continue
        if in_block:
            m = re.match(r'^  (\S+):\s*(\S+)\s*$', line)
            if m:
                roles[m.group(1)] = m.group(2)
            elif line and not line.startswith(' '):
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


def list_settings():
    out = run(["omp", "config", "list", "--json"])
    try:
        catalog = json.loads(out)
    except json.JSONDecodeError:
        return {"error": "omp config list --json 解析失败", "items": []}
    raw = CONFIG.read_bytes().decode('utf-8')
    idx = index_config(raw.splitlines(keepends=True))
    try:
        zh = json.loads((HERE / "zh.json").read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        zh = {}
    groups = zh.get("__groups", {})
    items = []
    for key, meta in catalog.items():
        typ = meta.get('type', 'string')
        info = zh.get(key, {})
        items.append({
            "key": key,
            "value": meta.get('value'),
            "type": typ,
            "desc": meta.get('description', ''),
            "name": info.get('name'),
            "zh": info.get('desc'),
            "options": info.get('options'),
            "group": groups.get(key.split('.')[0], key.split('.')[0]),
            "custom": key in idx,
            "editable": typ in EDITABLE_TYPES,
        })
    items.sort(key=lambda x: (x["key"].split('.')[0], x["key"]))
    return {"items": items, "groups": groups, "groupmap": zh.get("__groupmap", {}),
            "groups_en": zh.get("__groups_en", {})}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split('?', 1)[0]  # 允许 /?v=... 查询串（缓存穿透），路由只看路径
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(
                {"roles": parse_roles(), "models": list_models(),
                 "config": str(CONFIG)}, ensure_ascii=False))
        elif path == "/api/settings":
            self._send(200, json.dumps(list_settings(), ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send(400, '{"error": "bad json"}')
            return
        if self.path == "/api/set":
            err = set_role(payload.get("role", ""), payload.get("value", ""))
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(
                    {"ok": True, "roles": parse_roles()}, ensure_ascii=False))
        elif self.path == "/api/set-key":
            err = self._set_key(payload)
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
        else:
            self._send(404, "{}")

    def _set_key(self, payload):
        key = payload.get("key", "").strip()
        catalog = json.loads(run(["omp", "config", "list", "--json"]))
        meta = catalog.get(key)
        if meta is None:
            return f'未知设置项: {key}'
        if meta.get('type') not in EDITABLE_TYPES:
            return f'类型 {meta.get("type")} 不支持在线编辑'
        try:
            formatted = coerce_value(meta['type'], str(payload.get("value", "")))
        except ValueError as e:
            return str(e)
        return set_config_key(key, formatted)

    def log_message(self, format, *args):
        pass


def set_role(role, value):
    role, value = role.strip(), value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_@:,\.\-\/\*]+", role) or \
       not re.fullmatch(r"[A-Za-z0-9_@:,\.\-\/\*\?\[\]]+", value):
        return "illegal role or value"
    raw = CONFIG.read_bytes().decode('utf-8')
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    pat = re.compile(rf"^(  {re.escape(role)}:)[^\r\n]*(\r?\n)?$")
    replaced = False
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"  {role}: {value}{eol}"
            replaced = True
            break
    if not replaced:
        for i, line in enumerate(lines):
            if re.match(r"^modelRoles:\s*(\r?\n)?$", line):
                lines.insert(i + 1, f"  {role}: {value}{eol}")
                replaced = True
                break
        if not replaced:
            return "modelRoles block not found"
    write_backup()
    CONFIG.write_bytes("".join(lines).encode('utf-8'))
    return None


def main():
    addr = ("127.0.0.1", PORT)
    srv = ThreadingHTTPServer(addr, Handler)
    url = f"http://{addr[0]}:{PORT}"
    print(f"omp settings web ui -> {url}")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
