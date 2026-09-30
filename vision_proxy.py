#!/usr/bin/env python3
"""
vision-proxy —— 给纯文本模型(DeepSeek 等)装一双眼睛

链路:  Claude Code  ->  本代理(:8787)  ->  cc-switch(:15721)  ->  DeepSeek

原理:拦截 Anthropic 格式 /v1/messages 请求体里的 image 块,
     发给一个支持视觉的 OpenAI 兼容模型转成文字,
     再把文字以 text 块塞回原位转发给上游。
     同一张图按 sha256 缓存,多轮对话不会重复计费。
"""
import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------- 配置(全部走环境变量) ----------------
UPSTREAM     = os.environ.get("UPSTREAM_BASE_URL", "http://127.0.0.1:15721").rstrip("/")
VISION_URL   = os.environ.get("VISION_BASE_URL", "").rstrip("/")
VISION_KEY   = os.environ.get("VISION_API_KEY", "")
VISION_MODEL = os.environ.get("VISION_MODEL", "")
PORT         = int(os.environ.get("PORT", "8787"))
CONCURRENCY  = int(os.environ.get("VISION_CONCURRENCY", "4"))
MAX_TOKENS   = int(os.environ.get("VISION_MAX_TOKENS", "3000"))
TIMEOUT      = float(os.environ.get("VISION_TIMEOUT", "120"))
CACHE_FILE   = Path(os.environ.get(
    "VISION_CACHE", str(Path.home() / ".cache" / "vision-proxy" / "cache.json")))

# 模型映射:把 Claude 桌面端发来的模型名映射成上游真实模型名。
# 格式 "claude-opus-4-8=xopdeepseekv4pro,claude-haiku-4-5=xopglm52"。
# 设了 UPSTREAM 直指真实网关时,这替代了 cc-switch 原本做的映射,避免环路。
MODEL_MAP = {}
for _pair in os.environ.get("MODEL_MAP", "").split(","):
    _pair = _pair.strip()
    if "=" in _pair:
        k, v = _pair.split("=", 1)
        MODEL_MAP[k.strip()] = v.strip()

PROMPT = """你是一个图片转文字助手。你的输出会作为**唯一信息源**交给一个看不见图片的模型,
所以必须完整、准确,不能省略。请把图中全部有效信息写成文字:

- 代码/终端/报错截图:逐字转录,保留缩进、行号、完整报错栈
- 网页或 App 界面:描述整体布局,逐个列出可见文字、按钮、输入框、菜单项及其位置关系
- 设计稿:说明结构层级、配色(尽量给出色值)、字号字重、间距、组件状态
- 图表:说明图表类型、坐标轴含义与刻度、每条数据系列的名称/趋势/关键数值、图例
- 表格:用 Markdown 表格还原
- 照片/示意图:描述主体、场景,并转录所有可见文字

只输出转写内容本身,不要"这张图片显示了"之类的开场白,也不要评价或建议。
图里有文字的话,一个字都不要漏。"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- 缓存 ----------------
def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text("utf-8"))
    except Exception:
        return {}


CACHE = _load_cache()


def _save_cache() -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(CACHE, ensure_ascii=False), "utf-8")
    except Exception as e:
        log(f"缓存写入失败: {e}")


# ---------------- 找出请求体里的图片块 ----------------
def _find_images(node):
    """递归遍历,产出 (所在列表, 下标)。

    图片块在 Anthropic 格式里恒定住在列表中,包括 messages[].content[]
    和 tool_result.content[](Read 工具读图产生的就是后者)。
    """
    if isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, dict) and item.get("type") == "image":
                yield node, i
            else:
                yield from _find_images(item)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _find_images(v)


def _to_image_url(block):
    """把 Anthropic 的 image 块转成 OpenAI 的 image_url,顺带算缓存键。"""
    src = block.get("source") or {}
    if src.get("type") == "base64":
        data = src.get("data", "")
        media = src.get("media_type", "image/png")
        return f"data:{media};base64,{data}", hashlib.sha256(data.encode()).hexdigest()
    if src.get("type") == "url":
        url = src.get("url", "")
        return url, hashlib.sha256(url.encode()).hexdigest()
    return None, None


def _hint(parent) -> str:
    """同一条消息里的文字当提示,让视觉模型知道该重点看什么。"""
    texts = [b.get("text", "") for b in parent
             if isinstance(b, dict) and b.get("type") == "text"]
    return " ".join(texts).strip()[:400]


def _last_user_text(messages) -> str:
    """整个请求里最后一段用户文字。给相邻没有文字的图片兜底 ——
    Read 工具读图产生的 tool_result 里只有孤零零一个 image 块。"""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        c = msg.get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()[:400]
        if isinstance(c, list):
            t = _hint(c)
            if t:
                return t
    return ""


# ---------------- 调视觉模型 ----------------
async def _describe(client: httpx.AsyncClient, image_url: str, hint: str) -> str:
    prompt = PROMPT
    if hint:
        prompt += f"\n\n用户此刻的问题是:「{hint}」,请确保与之相关的细节写全。"
    r = await client.post(
        f"{VISION_URL}/chat/completions",
        headers={"Authorization": f"Bearer {VISION_KEY}",
                 "Content-Type": "application/json"},
        json={
            "model": VISION_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]}],
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


async def transform(body: dict) -> int:
    """就地把 body 里所有图片块换成文字块,返回处理张数。"""
    messages = body.get("messages", [])
    targets = list(_find_images(messages))
    if not targets:
        return 0
    if not (VISION_URL and VISION_KEY and VISION_MODEL):
        for parent, idx in targets:
            parent[idx] = {"type": "text",
                           "text": "[图片已忽略:vision-proxy 未配置视觉模型]"}
        log("⚠️  收到图片但未配置视觉模型,已替换为占位文字")
        return len(targets)

    fallback = _last_user_text(messages)

    # 先按图片内容分组。同一个请求里重复出现的图(粘两次、或 Read 又读了一遍)
    # 只调一次视觉 API —— 否则并发的协程会一起穿透缓存,重复计费。
    groups: dict[str, dict] = {}
    for parent, idx in targets:
        image_url, key = _to_image_url(parent[idx])
        if not image_url:
            parent[idx] = {"type": "text", "text": "[图片格式无法解析]"}
            continue
        g = groups.setdefault(key, {"url": image_url, "spots": [], "hint": ""})
        g["spots"].append((parent, idx))
        g["hint"] = g["hint"] or _hint(parent) or fallback

    sem = asyncio.Semaphore(CONCURRENCY)
    dirty = False

    def fill(spots, text):
        for parent, idx in spots:
            parent[idx] = {"type": "text", "text": text}

    async def work(client, key, g):
        nonlocal dirty
        desc = CACHE.get(key)
        if desc is None:
            async with sem:
                try:
                    desc = await _describe(client, g["url"], g["hint"])
                except httpx.HTTPStatusError as e:
                    log(f"❌ 视觉API返回 {e.response.status_code}: "
                        f"{e.response.text[:300]}")
                    fill(g["spots"], f"[图片识别失败:HTTP {e.response.status_code}]")
                    return
                except Exception as e:
                    log(f"❌ 视觉模型调用失败 ({VISION_URL}/chat/completions): "
                        f"{type(e).__name__}: {e}")
                    fill(g["spots"], f"[图片识别失败:{type(e).__name__}]")
                    return
                CACHE[key] = desc
                dirty = True
        fill(g["spots"], f'<image id="{key[:8]}">\n{desc}\n</image>')

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(work(client, k, g) for k, g in groups.items()))
    if dirty:
        _save_cache()
    return len(targets)


# ---------------- 代理转发 ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=None)
    log(f"vision-proxy 已启动  :{PORT}  ->  {UPSTREAM}")
    if VISION_URL and VISION_MODEL:
        log(f"视觉模型: {VISION_MODEL} @ {VISION_URL}")
    else:
        log("⚠️  未配置视觉模型,图片会被替换成占位文字。请设置 VISION_* 环境变量")
    log(f"缓存: {CACHE_FILE} (已有 {len(CACHE)} 条)")
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan)


async def forward(request: Request, path: str, raw: bytes):
    hop = {"host", "content-length", "accept-encoding", "connection"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in hop}
    client: httpx.AsyncClient = app.state.client
    req = client.build_request(request.method, f"{UPSTREAM}{path}",
                               params=request.query_params,
                               content=raw or None, headers=headers)
    try:
        resp = await client.send(req, stream=True)
    except Exception as e:
        log(f"❌ 上游连接失败: {e}")
        return JSONResponse({"type": "error", "error": {
            "type": "api_error",
            "message": f"vision-proxy 无法连接上游 {UPSTREAM}: {e}"}}, status_code=502)

    drop = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    out = {k: v for k, v in resp.headers.items() if k.lower() not in drop}

    async def body_iter():
        # 必须用 aiter_bytes(已解压)而不是 aiter_raw —— 上面把 content-encoding
        # 头去掉了,再吐原始 gzip 字节会让 Claude Code 收到一堆乱码。
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(body_iter(), status_code=resp.status_code,
                             headers=out,
                             media_type=resp.headers.get("content-type"))


async def _handle_with_vision(request: Request, path: str):
    raw = await request.body()
    try:
        body = json.loads(raw)
    except Exception:
        body = None
    if isinstance(body, dict) and body.get("messages"):
        # 模型映射:把 Claude 模型名换成上游真实模型名(替代 cc-switch 的映射)
        if MODEL_MAP:
            orig = body.get("model")
            mapped = MODEL_MAP.get(orig)
            if mapped:
                body["model"] = mapped
                log(f"🔄 模型映射 {orig} -> {mapped}")
        t0 = time.time()
        n = await transform(body)
        if n or MODEL_MAP:
            raw = json.dumps(body, ensure_ascii=False).encode()
            if n:
                log(f"👁  转写 {n} 张图片,耗时 {time.time() - t0:.1f}s")
    return await forward(request, path, raw)


@app.post("/v1/messages")
async def v1_messages(request: Request):
    return await _handle_with_vision(request, "/v1/messages")


@app.post("/v1/messages/count_tokens")
async def v1_count_tokens(request: Request):
    return await _handle_with_vision(request, "/v1/messages/count_tokens")


@app.get("/__vision/health")
async def health():
    return {"ok": True,
            "upstream": UPSTREAM,
            "vision_model": VISION_MODEL or None,
            "vision_configured": bool(VISION_URL and VISION_KEY and VISION_MODEL),
            "cached_images": len(CACHE)}


@app.api_route("/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def catch_all(request: Request, path: str):
    return await forward(request, "/" + path, await request.body())


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
