# omp 模型角色切换器 / omp Model Role Switcher

A tiny local web UI to switch [oh-my-pi (omp)](https://omp.sh) model roles by clicking — no more digging through the TUI `/model` hub.

一个本地 Web 小程序：在浏览器里点击切换 omp 的 `modelRoles`，替代 TUI 内 `/model`（Alt+M）的多层操作。改动即时生效，无需重启会话。

![screenshot](docs/screenshot.png)

## Features / 功能

- **Role matrix** — lists every configured role (`default`, `smol`, `slow`, `plan`, `vision`, `task`, `commit`, `tiny`, `web`, …) with a one-line purpose hint. 列出全部已配置角色与用途注释。
- **Two-level picker** — first pick a provider group (with model count), then a model within it; no scrolling through a 270+ item list. 两级选择：先选供应商分组（带数量），再选组内模型。
- **Auto-positioning** — opening the picker jumps to the current value's group and selects it. 打开时自动定位当前值所在分组。
- **Alias fallback** — values not in the catalog (`@role` aliases, custom selectors) are kept as a "(current)" entry and never lost. 别名与目录外写法兜底保留。
- **Safe writes** — byte-precise targeted edit of `~/.omp/agent/config.yml` with automatic backup; comments and line endings preserved. 见下文安全设计。

## Requirements / 依赖

- Python 3.8+ (standard library only, zero dependencies / 纯标准库，零第三方依赖)
- [omp](https://github.com/can1357/oh-my-pi) installed and on `PATH` (used for `omp models --json`)

## Usage / 使用

```bash
python server.py            # starts http://127.0.0.1:8788 and opens your browser
python server.py --no-browser
```

Open [http://127.0.0.1:8788](http://127.0.0.1:8788), click **切换** on a role, pick a provider group, then a model, **保存**. The change applies immediately — running omp sessions pick it up, no restart needed.

打开页面 → 角色行点「切换」→ 选分组 → 选模型 → 保存。配置写入后 omp **即时生效**，运行中的会话无需重启。

## How it stays safe / 安全设计

`omp config set` rewrites the whole file and strips your comments. This tool never calls it. Instead:

1. **Targeted line edit** — reads `~/.omp/agent/config.yml` and regex-replaces only the target role's line.
2. **Byte-precise I/O** — `read_bytes`/`write_bytes` with original line-ending detection (LF vs CRLF), so everything except the edited line stays byte-identical.
3. **Automatic backup** — every write first copies the config to `config.yml.bak-role-switcher-<timestamp>`.

Server binds to `127.0.0.1` only. Value/role inputs are validated against a whitelist regex before touching the file.

> ⚠️ Pitfall worth knowing (and the reason for #2): on Windows, Python's `Path.write_text()` silently translates LF to CRLF, which would rewrite your entire config's line endings.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/state` | GET | Current roles (parsed from config.yml) + full model catalog (`omp models --json`) |
| `/api/set` | POST | `{"role": "...", "value": "..."}` — backup + targeted write |

## License

[MIT](LICENSE) — not affiliated with the omp project.
