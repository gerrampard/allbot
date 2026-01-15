import shutil
import time
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

import requests
import tomllib
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/github-proxy", tags=["github-proxy"])

_check_auth = None

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "main_config.toml"
_UPSTREAM_NODES_API = "https://api.akams.cn/github"

_NODES_CACHE_TTL_SECONDS = 600
_nodes_cache: Dict[str, Any] = {"ts": 0, "nodes": []}

_TEST_GITHUB_URL = "https://raw.githubusercontent.com/microsoft/vscode/refs/heads/main/extensions/markdown-math/icon.png"


async def _require_auth(request: Request) -> Optional[str]:
    if _check_auth is None:
        return None
    username = await _check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return username


def _normalize_proxy_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("代理地址必须以 http:// 或 https:// 开头")
    if not url.endswith("/"):
        url += "/"
    return url


def _read_current_github_proxy() -> str:
    try:
        with open(_CONFIG_PATH, "rb") as f:
            config = tomllib.load(f)
        value = config.get("XYBot", {}).get("github-proxy", "")
        return _normalize_proxy_url(value) if value else ""
    except Exception as e:
        logger.warning(f"读取当前 github-proxy 失败: {e}")
        return ""


def _write_github_proxy(value: str) -> None:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError("main_config.toml 不存在")

    normalized = _normalize_proxy_url(value) if value else ""

    backup_path = _CONFIG_PATH.with_suffix(_CONFIG_PATH.suffix + ".bak")
    try:
        shutil.copy2(_CONFIG_PATH, backup_path)
    except Exception as e:
        logger.warning(f"备份 main_config.toml 失败: {e}")

    content = _CONFIG_PATH.read_text(encoding="utf-8")
    value_literal = normalized.replace('"', '\\"')
    replacement_line = f'github-proxy = "{value_literal}"'

    section_re = re.compile(r"(?ms)^\[XYBot\]\s*$.*?(?=^\[|\Z)")
    match = section_re.search(content)
    if match:
        section = match.group(0)
        if re.search(r"(?m)^\s*github-proxy\s*=", section):
            section = re.sub(
                r"(?m)^(\s*github-proxy\s*=\s*).*$",
                lambda m: m.group(1) + f'"{value_literal}"',
                section,
            )
        else:
            lines = section.splitlines(True)
            if lines:
                lines.insert(1, replacement_line + "\n")
                section = "".join(lines)
            else:
                section = f"[XYBot]\n{replacement_line}\n"

        content = content[: match.start()] + section + content[match.end() :]
    else:
        suffix = "\n" if content and not content.endswith("\n") else ""
        content = content + suffix + f"\n[XYBot]\n{replacement_line}\n"

    _CONFIG_PATH.write_text(content, encoding="utf-8")


def _fetch_nodes_from_upstream() -> List[Dict[str, Any]]:
    now = int(time.time())
    cached_ts = int(_nodes_cache.get("ts") or 0)
    if cached_ts and now - cached_ts < _NODES_CACHE_TTL_SECONDS:
        nodes = _nodes_cache.get("nodes")
        if isinstance(nodes, list) and nodes:
            return nodes

    response = requests.get(_UPSTREAM_NODES_API, timeout=8)
    response.raise_for_status()
    payload = response.json()

    if payload.get("code") != 200:
        raise RuntimeError(f"上游返回异常: {payload.get('msg') or payload}")

    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("上游返回数据结构异常")

    nodes: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_url = (item.get("url") or "").strip()
        if not raw_url:
            continue
        try:
            proxy_url = _normalize_proxy_url(raw_url)
        except Exception:
            continue
        nodes.append(
            {
                "url": proxy_url,
                "latency": item.get("latency"),
                "speed": item.get("speed"),
                "tag": item.get("tag"),
            }
        )

    nodes.sort(key=lambda x: (x.get("latency") is None, x.get("latency") or 10**9))
    _nodes_cache["ts"] = now
    _nodes_cache["nodes"] = nodes
    return nodes


def _probe_proxy(proxy_url: str) -> Dict[str, Any]:
    proxy_url = _normalize_proxy_url(proxy_url)
    test_url = f"{proxy_url}{_TEST_GITHUB_URL}"
    headers = {"Range": "bytes=0-2048"}

    start = time.perf_counter()
    try:
        resp = requests.get(test_url, headers=headers, timeout=8, allow_redirects=True)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        ok = resp.status_code == 200
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "elapsed_ms": elapsed_ms,
            "final_url": resp.url,
        }
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "ok": False,
            "status_code": None,
            "elapsed_ms": elapsed_ms,
            "error": str(e),
        }


@router.get("/current")
async def get_current_proxy(request: Request):
    await _require_auth(request)
    return JSONResponse({"success": True, "data": {"github_proxy": _read_current_github_proxy()}})


@router.get("/nodes")
async def get_nodes(request: Request, refresh: bool = False):
    await _require_auth(request)

    if refresh:
        _nodes_cache["ts"] = 0
        _nodes_cache["nodes"] = []

    try:
        nodes = await run_in_threadpool(_fetch_nodes_from_upstream)
        return JSONResponse({"success": True, "data": {"nodes": nodes}})
    except Exception as e:
        logger.error(f"获取 GitHub 反代节点失败: {e}")
        return JSONResponse({"success": False, "error": f"获取节点失败: {e}"}, status_code=500)


@router.post("/check")
async def check_node(request: Request):
    await _require_auth(request)

    payload = await request.json()
    url = payload.get("url") if isinstance(payload, dict) else ""
    try:
        result = await run_in_threadpool(_probe_proxy, url)
        return JSONResponse({"success": True, "data": {"url": _normalize_proxy_url(url), **result}})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@router.post("/apply")
async def apply_node(request: Request):
    await _require_auth(request)

    payload = await request.json()
    url = payload.get("url") if isinstance(payload, dict) else ""
    try:
        normalized = _normalize_proxy_url(url) if url else ""
        await run_in_threadpool(_write_github_proxy, normalized)
        return JSONResponse(
            {
                "success": True,
                "message": "已更新 github-proxy 配置（需要重启服务生效）",
                "data": {"github_proxy": normalized},
            }
        )
    except Exception as e:
        logger.error(f"更新 github-proxy 失败: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


def register_github_proxy_routes(app, check_auth):
    global _check_auth
    _check_auth = check_auth
    app.include_router(router)
    logger.info("GitHub 反代节点 API 路由已注册")
