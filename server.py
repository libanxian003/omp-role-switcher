# -*- coding: utf-8 -*-
"""omp 设置中心 / omp Settings Web（原 omp Settings Center，更早是模型角色切换器）。

- 模型角色：两级分组选择器，点击切换 modelRoles。
- 全部设置：读取 `omp config list --json` 全量目录（键/值/类型/描述），
  标量项（布尔/数字/字符串/枚举）定向编辑 config.yml；数组与嵌套块只读展示。
- 用量：透传 `omp usage --json` 的额度/用量报告（各供应商窗口限额、
  已用/剩余、重置时间、重置额度券），供「用量」页签做可视化。

安全设计：不用 `omp config set`（会重写整文件并剥离注释）；按字节读写
~/.omp/agent/config.yml，只替换/插入目标行并沿用原行尾；每次写入前自动备份。
"""
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import time
import webbrowser
import sqlite3
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
DB = Path.home() / ".omp" / "agent" / "agent.db"

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
        f"config.yml.bak-settings-web-{time.strftime('%Y%m%d-%H%M%S')}")
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


# ---------- 模型显示（enabledModels） ----------

ENABLED_HEAD = re.compile(r'^enabledModels:[ \t]*(\[\s*\])?[ \t]*(#.*)?(\r?\n)?$')
LIST_ITEM = re.compile(r'^[ \t]+-[ \t]+(.*?)[ \t]*(\r?\n)?$')


def enabled_block(lines):
    """定位顶层 enabledModels 段，返回 (首行, 尾后行, 条目) 或 None；含 path 作用域条目时条目为 None。"""
    for i, line in enumerate(lines):
        m = ENABLED_HEAD.match(line)
        if not m:
            continue
        if m.group(1):  # enabledModels: []
            return i, i + 1, []
        entries, j = [], i + 1
        while j < len(lines):
            ln = lines[j]
            if not ln.strip():
                break
            if not ln[0].isspace():
                break
            item = LIST_ITEM.match(ln)
            if item is None:
                return i, None, None  # 嵌套/作用域结构，不托管
            v, _ = split_comment(item.group(1))
            if v.startswith(('{', 'path', 'paths')) or v.endswith(':'):
                return i, None, None
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                v = v[1:-1]
            entries.append(v)
            j += 1
        return i, j, entries
    return None


def pattern_matches(pat, model):
    p = pat.lower()
    sel = model["selector"].lower()
    if any(c in p for c in "*?["):
        return fnmatch.fnmatchcase(sel, p) or fnmatch.fnmatchcase(sel.split('/', 1)[1], p)
    return p == sel or p == sel.split('/', 1)[1]


def visibility_state():
    lines = CONFIG.read_bytes().decode('utf-8').splitlines(keepends=True)
    blk = enabled_block(lines)
    models = list_models()
    patterns = blk[2] if blk else []
    managed = not (blk and blk[2] is None)
    if not patterns:  # 空名单 = 全部显示
        visible = [m["selector"] for m in models]
    else:
        visible = [m["selector"] for m in models
                   if any(pattern_matches(p, m) for p in patterns)]
    return {"models": models, "visible": visible, "patterns": patterns or [],
            "managed": managed, "default": parse_roles().get("default")}


def set_visible(selected):
    if not isinstance(selected, list) or not all(isinstance(s, str) for s in selected):
        return "参数格式错误"
    models = list_models()
    by_sel = {m["selector"]: m for m in models}
    chosen = {s for s in selected if s in by_sel}
    if not chosen:
        return "至少要保留一个可见模型（空名单在 omp 中等于全部显示）"
    raw = CONFIG.read_bytes().decode('utf-8')
    eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.splitlines(keepends=True)
    blk = enabled_block(lines)
    if blk and blk[2] is None:
        return "config.yml 的 enabledModels 含 path 作用域等复杂结构，请手工编辑"
    old = blk[2] if blk else []
    # 当前匹配不到任何模型的条目（供应商离线等）原样保留，避免上线后被意外隐藏
    dormant = [p for p in old if not any(pattern_matches(p, m) for m in models)]
    providers = {}
    for m in models:
        providers.setdefault(m["provider"], []).append(m["selector"])
    out = []
    default = parse_roles().get("default")
    if default and default in by_sel:  # 启动模型取名单首项：把 default 角色模型放第一位且保持可见
        chosen.add(default)
        out.append(default)
    for prov in sorted(providers):
        sels = providers[prov]
        if all(s in chosen for s in sels):
            out.append(f"{prov}/*")
        else:
            out.extend(s for s in sorted(sels) if s in chosen and s != default)
    out.extend(p for p in dormant if p not in out)
    block = [f"enabledModels:{eol}"] + [f"  - {json.dumps(p)}{eol}" for p in out]
    if blk:
        lines[blk[0]:blk[1]] = block
    else:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += eol
        lines.extend(block)
    write_backup()
    CONFIG.write_bytes(''.join(lines).encode('utf-8'))
    return None


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
            "desc_en": info.get('desc_en'),
            "options": info.get('options'),
            "group": groups.get(key.split('.')[0], key.split('.')[0]),
            "custom": key in idx,
            "editable": typ in EDITABLE_TYPES,
        })
    items.sort(key=lambda x: (x["key"].split('.')[0], x["key"]))
    return {"items": items, "groups": groups, "groupmap": zh.get("__groupmap", {}),
            "groups_en": zh.get("__groups_en", {})}


# ---------- 用量（omp usage --json） ----------

def usage_report():
    """跑 `omp usage --json` 并透传报告；失败返回 {"error": ...}。
    报告中 metadata.email 属本机账号信息，本地面板原样展示；
    但凭据/token 从不进入该 JSON（omp 自己也不输出）。"""
    try:
        p = subprocess.run(
            ["omp", "usage", "--json", "--no-extensions"],
            capture_output=True, text=True, shell=False,
            encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "omp usage 超时（120s）"}
    except OSError as e:
        return {"error": f"无法执行 omp: {e}"}
    if p.returncode != 0 and not p.stdout.strip():
        err = (p.stderr or "").strip().splitlines()
        return {"error": err[0] if err else f"omp usage 退出码 {p.returncode}"}
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"error": "omp usage 输出不是合法 JSON"}

def invalidate_usage(provider):
    """跑 `omp usage invalidate`（可选 --provider）。provider 只允许
    'all' 或已认证账号列表里的 provider id，白名单校验防注入。"""
    args = ["omp", "usage", "invalidate", "--no-extensions"]
    if provider not in (None, "", "all"):
        provider = provider.strip().lower()
        known = {str(r.get("provider", "")).lower()
                 for r in (usage_report().get("reports") or [])}
        if provider not in known:
            return {"error": f"未知供应商: {provider}"}
        args += ["--provider", provider]
    try:
        p = subprocess.run(args, capture_output=True, text=True, shell=False,
                           encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        return {"error": "omp usage invalidate 超时（60s）"}
    except OSError as e:
        return {"error": f"无法执行 omp: {e}"}
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return {"error": err[0] if err else f"omp usage invalidate 退出码 {p.returncode}"}
    return {"ok": True, "message": (p.stdout or "").strip()}

def usage_with_resettable():
    """usage_report() 外加 resettable 标记：仅 anthropic/openai-codex
    且 resetCredits.availableCount>0 时可重置（不透 token）。"""
    r = usage_report()
    if "error" in r:
        return r
    for rep in r.get("reports") or []:
        rc = rep.get("resetCredits")
        rep["resettable"] = rep.get("provider") in REDEEM_PROVIDERS \
            and isinstance(rc, dict)
    return r


# ---------- 额度券重置（复刻 omp auth/resets.ts + usage/openai-codex-reset.ts） ----------
# 仅支持 anthropic / openai-codex（omp 的 resets.list 也是这个白名单）。
# token 只在本进程内存与出站 Authorization 头里出现，绝不写入响应/日志/文件。

REDEEM_PROVIDERS = {"anthropic", "openai-codex"}
ANTHROPIC_API = "https://api.anthropic.com"
ANTHROPIC_BETA = "oauth-2025-04-20"
ANTHROPIC_CLI_VER = "2.1.280"  # 对齐 omp 内嵌的 claude-cli 版本
CODEX_API = "https://chatgpt.com/backend-api"
RE_CREDIT_ID = re.compile(r"^[a-z0-9_-]{1,40}$")   # omp 侧 _ks
RE_ORG_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")  # omp 侧 Tvt

# 与 omp 一致的「同 request_id 待确认」状态：首扣拿到响应后缓存，
# 重复点击用同一 request_id 重放（服务端幂等），直到确认成功/终态。
_redeem_pending = {}  # provider -> {"credit_id","request_id","program","remaining"}


def _cred(provider):
    """从 ~/.omp/agent/agent.db 读该 provider 的 oauth 凭据（只读）。"""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        row = con.execute(
            "SELECT data FROM auth_credentials "
            "WHERE provider=? AND credential_type='oauth' "
            "AND disabled_cause IS NULL ORDER BY id", (provider,)).fetchone()
        con.close()
    except sqlite3.Error as e:
        return None, f"读取凭据库失败: {e}"
    if not row:
        return None, f"{provider} 无 oauth 凭据"
    try:
        d = json.loads(row[0])
    except json.JSONDecodeError:
        return None, "凭据数据不是合法 JSON"
    access = d.get("access")
    if not isinstance(access, str) or not access:
        return None, "凭据缺少 access token"
    if isinstance(d.get("expires"), (int, float)) and d["expires"] <= time.time() * 1000:
        return None, "access token 已过期，请在 omp 中重新登录后再试"
    return {
        "access": access,
        "accountId": d.get("accountId") if isinstance(d.get("accountId"), str) else None,
        "orgId": d.get("orgId") if isinstance(d.get("orgId"), str) else None,
    }, None


def _http(method, url, access, extra_headers=None, body=None, timeout=25):
    """最小 HTTPS 调用，返回 (status, json_or_None, error_str_or_None)。
    任何异常/响应体都不会把 access token 带出。"""
    headers = {"Authorization": f"Bearer {access}",
               "User-Agent": "omp-settings-web/1.0"}
    for k, v in (extra_headers or {}).items():  # 大小写不敏感覆盖
        headers = {h: x for h, x in headers.items() if h.lower() != k.lower()}
        headers[k] = v
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _json_or_none(raw), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:2000]
        return e.code, _json_or_none(raw), None
    except Exception as e:  # URLError/timeout/ssl 等
        return 0, None, str(e)


def _json_or_none(raw):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def redeem_reset(provider):
    """消耗一张额度券重置该 provider 用量。返回结构化结果 dict。"""
    provider = (provider or "").strip().lower()
    if provider not in REDEEM_PROVIDERS:
        return {"ok": False, "code": "unsupported_provider",
                "error": "仅 anthropic / openai-codex 支持额度券重置"}
    cred, err = _cred(provider)
    if err:
        return {"ok": False, "code": "no_account", "error": err}
    if provider == "anthropic":
        return _redeem_anthropic(cred)
    return _redeem_codex(cred)


# ----- anthropic：两步确认（同 request_id 重放），对齐 omp #r 逻辑 -----

def _anthropic_headers():
    return {"accept": "application/json, text/plain, */*",
            "anthropic-beta": ANTHROPIC_BETA,
            # 服务端按 client surface 判定券资格（ineligible_reason:"surface"），
            # 必须与 omp 一致伪装成 claude-cli
            "user-agent": f"claude-cli/{ANTHROPIC_CLI_VER} (external, cli)"}


def _anthropic_list(cred):
    """GET /api/oauth/usage?cedar_ember=1&skip_spend=1 → 规范化券列表。"""
    url = f"{ANTHROPIC_API}/api/oauth/usage?cedar_ember=1&skip_spend=1"
    status, body, err = _http("GET", url, cred["access"],
                              _anthropic_headers())
    if err:
        return None, {"ok": False, "code": "network_error", "error": err}
    if status in (401, 403):
        return None, {"ok": False, "code": "auth_error",
                      "error": f"鉴权失败（HTTP {status}），请在 omp 中重新登录"}
    if status != 200 or not isinstance(body, dict):
        return None, {"ok": False, "code": f"http_{status}",
                      "error": f"列券接口返回 HTTP {status}"}
    blk = body.get("cedar_ember")
    if blk is None:
        return {"eligible": False, "grants": [], "nextGrantId": None,
                "cooldownUntil": None}, None
    if not isinstance(blk, dict) or not isinstance(blk.get("eligible"), bool):
        return None, {"ok": False, "code": "malformed_response",
                      "error": "列券响应格式不符"}
    grants = []
    for g in blk.get("grants") or []:
        if not isinstance(g, dict) or not RE_CREDIT_ID.match(str(g.get("id", ""))):
            continue
        grants.append({
            "id": g["id"],
            "label": g.get("label"),
            "remaining": g.get("resets_left", 0),
            "expiresAt": g.get("ends_at"),
            "usableNow": bool(g.get("usable_now")),
            "paused": bool(g.get("paused")),
            "clears": g.get("clears") or [],
        })
    nxt = blk.get("next_grant_id")
    nxt = nxt if isinstance(nxt, str) and any(g["id"] == nxt for g in grants) else None
    # cedar_ember 无可用券时回退 juniper_tide（对齐 omp e4 的第二条探测）
    if nxt is None or not blk["eligible"]:
        jt = _anthropic_list_juniper(cred)
        if jt and (jt["nextGrantId"] or jt["grants"]):
            return jt, None
    return {"eligible": blk["eligible"], "grants": grants, "nextGrantId": nxt,
            "cooldownUntil": blk.get("cooldown_until"),
            "reason": blk.get("ineligible_reason")}, None

def _anthropic_list_juniper(cred):
    """omp e4 的第二条探测：GET .../usage?at_wall=1&skip_spend=1 → juniper_tide 键。
    返回规范化结构（program='juniper_tide' 的虚拟券）或 None。"""
    url = f"{ANTHROPIC_API}/api/oauth/usage?at_wall=1&skip_spend=1"
    status, body, err = _http("GET", url, cred["access"],
                              _anthropic_headers(), timeout=15)
    if err or status != 200 or not isinstance(body, dict):
        return None
    blk = body.get("juniper_tide")
    if not isinstance(blk, dict) or not isinstance(blk.get("eligible"), bool):
        return None
    usable = blk["eligible"] and blk.get("arm") == "reset" and bool(blk.get("available"))
    grants = [{
        "id": "juniper_tide", "label": "Claude session limit reset",
        "program": "juniper_tide",
        "remaining": 1 if usable else 0,
        "expiresAt": blk.get("weekly_resets_at"),
        "usableNow": usable, "paused": False,
        "clears": ["anthropic:5h"],
    }] if blk.get("arm") == "reset" else []
    return {"eligible": blk["eligible"], "grants": grants,
            "nextGrantId": "juniper_tide" if usable else None,
            "cooldownUntil": blk.get("next_available_at"),
            "reason": blk.get("ineligible_reason"),
            "program": "juniper_tide"}


def _redeem_anthropic(cred):
    listed, err = _anthropic_list(cred)
    if err or listed is None:
        return err or {"ok": False, "code": "credit_list_failed",
                       "error": "列券失败"}
    program = listed.get("program") or "cedar_ember"
    credit = next((g for g in listed["grants"] if g["id"] == listed["nextGrantId"]), None)
    if not listed["eligible"] or not credit or not credit["usableNow"] \
            or credit["remaining"] < 1:
        code = "ineligible" if listed["grants"] else "no_credit"
        return {"ok": False, "code": code,
                "error": listed.get("reason") or "当前没有可用额度券"}
    # orgId：凭据里有就直接用，否则 GET /profile
    org = cred.get("orgId")
    if not (isinstance(org, str) and RE_ORG_ID.match(org)):
        status, body, err = _http("GET", f"{ANTHROPIC_API}/api/oauth/profile",
                                  cred["access"], _anthropic_headers(), timeout=5)
        org = None
        if isinstance(body, dict):
            o = body.get("organization")
            org = (o.get("uuid") if isinstance(o, dict) else None) \
                  or body.get("organization_uuid")
        if not (isinstance(org, str) and RE_ORG_ID.match(org)):
            return {"ok": False, "code": "organization_unavailable",
                    "error": "无法解析组织 ID"}
    # 两步确认：pending 里已有同券同 request_id 才视为确认；否则首扣并缓存
    pend = _redeem_pending.get("anthropic")
    if pend and pend["credit_id"] != credit["id"]:
        pend = None  # 券变了 → 重新开始
    if pend is None:
        pend = {"credit_id": credit["id"], "request_id": str(uuid.uuid4()),
                "remaining": credit["remaining"]}
        _redeem_pending["anthropic"] = pend
    if program == "juniper_tide":
        body = {"program": "juniper_tide"}
    else:
        body = {"program": "cedar_ember", "grant_id": credit["id"],
                "request_id": pend["request_id"]}
    status, resp, err = _http(
        "POST", f"{ANTHROPIC_API}/api/organizations/{org}/reset_rate_limits",
        cred["access"], _anthropic_headers(), body=body)
    if err:
        return {"ok": False, "code": "network_error", "error": err}
    if status != 200 or not isinstance(resp, dict) or not isinstance(resp.get("result"), str):
        code = {401: "auth_error", 403: "auth_error", 429: "rate_limited"}.get(
            status, f"http_{status}")
        _redeem_pending.pop("anthropic", None)
        return {"ok": False, "code": code, "error": f"HTTP {status}"}
    result = resp["result"].strip()
    code = {"already_used": "already_redeemed",
            "not_limited": "nothing_to_reset"}.get(result, result)
    if result == "reset":
        _redeem_pending.pop("anthropic", None)
        invalidate_usage("anthropic")
        return {"ok": True, "code": "reset", "cleared": resp.get("cleared") or []}
    if code in ("already_redeemed", "nothing_to_reset", "ineligible"):
        _redeem_pending.pop("anthropic", None)
    return {"ok": False, "code": code,
            "error": resp.get("reason") or f"result={result}"}


# ----- openai-codex：单步（request_id 幂等），对齐 omp JHe/a4 -----

def _codex_headers(cred):
    h = {}
    if cred.get("accountId"):
        h["ChatGPT-Account-Id"] = cred["accountId"]
    return h


def _redeem_codex(cred):
    url = f"{CODEX_API}/wham/rate-limit-reset-credits"
    status, body, err = _http("GET", url, cred["access"], _codex_headers(cred))
    if err:
        return {"ok": False, "code": "network_error", "error": err}
    if status in (401, 403):
        return {"ok": False, "code": "auth_error",
                "error": f"鉴权失败（HTTP {status}），请在 omp 中重新登录"}
    if status != 200 or not isinstance(body, dict):
        return {"ok": False, "code": f"http_{status}", "error": f"HTTP {status}"}
    credits = [c for c in (body.get("credits") or [])
               if isinstance(c, dict) and (c.get("status") or "available") == "available"
               and isinstance(c.get("id"), str)]
    if not credits:
        return {"ok": False, "code": "no_credit", "error": "当前没有可用额度券"}
    # 选最早过期的（omp ZHe 逻辑）
    def expkey(c):
        try:
            return float("inf") if not c.get("expires_at") else \
                __import__("datetime").datetime.fromisoformat(
                    c["expires_at"].replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return float("inf")
    credit = min(credits, key=expkey)
    pend = _redeem_pending.get("openai-codex")
    if not pend or pend["credit_id"] != credit["id"]:
        pend = {"credit_id": credit["id"], "request_id": str(uuid.uuid4())}
        _redeem_pending["openai-codex"] = pend
    status, resp, err = _http(
        "POST", f"{CODEX_API}/wham/rate-limit-reset-credits/consume",
        cred["access"], _codex_headers(cred),
        body={"credit_id": credit["id"],
              "redeem_request_id": pend["request_id"],
              "account_id": cred.get("accountId")})
    if err:
        return {"ok": False, "code": "network_error", "error": err}
    code = (resp or {}).get("code") if isinstance(resp, dict) else None
    code = code if isinstance(code, str) else ("reset" if 200 <= status < 300 else f"http_{status}")
    if code == "reset":
        _redeem_pending.pop("openai-codex", None)
        invalidate_usage("openai-codex")
        return {"ok": True, "code": "reset"}
    _redeem_pending.pop("openai-codex", None)
    return {"ok": False, "code": code, "error": f"consume 返回 {code}"}


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
            vis = visibility_state()
            self._send(200, json.dumps(
                {"roles": parse_roles(), "models": vis["models"],
                 "visible": vis["visible"], "config": str(CONFIG)}, ensure_ascii=False))
        elif path == "/api/settings":
            self._send(200, json.dumps(list_settings(), ensure_ascii=False))
        elif path == "/api/visibility":
            self._send(200, json.dumps(visibility_state(), ensure_ascii=False))
        elif path == "/api/usage":
            self._send(200, json.dumps(usage_with_resettable(), ensure_ascii=False))
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
        elif self.path == "/api/visibility":
            err = set_visible(payload.get("visible"))
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps({"ok": True, **visibility_state()},
                                           ensure_ascii=False))
        elif self.path == "/api/usage/invalidate":
            self._send(200, json.dumps(
                invalidate_usage(payload.get("provider")), ensure_ascii=False))
        elif self.path == "/api/usage/redeem":
            self._send(200, json.dumps(
                redeem_reset(payload.get("provider")), ensure_ascii=False))
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
    srv = None
    try:
        srv = ThreadingHTTPServer(addr, Handler)
    except OSError as e:
        print(f"port {PORT} unavailable, another instance likely running: {e}")
        return
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
