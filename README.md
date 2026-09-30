# vision-proxy

给 DeepSeek 这类**纯文本模型**装一双眼睛。在 Claude 桌面端里正常粘贴/拖拽截图,
代理会自动把图片交给视觉模型转成文字,再把文字喂给 DeepSeek —— DeepSeek 全程无感。

## 当前架构(2026-08-25 验证通过)

```
Claude 桌面端
   ↓  base_url = 127.0.0.1:15721/claude-desktop (CC Switch 注入,不可改)
CC Switch (15721)
   ↓  当前选「星火」provider, base_url 改成 http://127.0.0.1:8787
vision-proxy (8787)
   ↓  1. 拦截 image block → 调 xopqwen35v35b 转文字
   ↓  2. 模型映射 claude-opus-4-8 → xopdeepseekv4pro (替代 CC Switch 的映射)
   ↓  3. 透传 x-api-key 鉴权头
星火真实端点 https://maas-api.cn-huabei-1.xf-yun.com/anthropic
   ↓
DeepSeek
```

**为什么不走 cc-switch 做映射了?** 因为如果 vision-proxy 上游指回 cc-switch(15721),
而 cc-switch 的「星火」provider base_url 又指向 8787,会成环死循环。所以让 vision-proxy
直连星火真实端点,并自己兼做模型映射,绕开 cc-switch 这一层。

## .env 关键配置

复制 `.env.example` 为 `.env`，再填写自己的 API 地址和密钥。`.env` 不应提交到 Git。

```bash
# 视觉模型(星火 maas 上的 Qwen3.5-VL-35B,已实测可用)
VISION_BASE_URL=https://maas-api.cn-huabei-1.xf-yun.com/v2
VISION_API_KEY=your-api-key
VISION_MODEL=xopqwen35v35b

# 上游直连星火真实 Anthropic 端点
UPSTREAM_BASE_URL=https://maas-api.cn-huabei-1.xf-yun.com/anthropic
# 模型映射(替代 cc-switch 的 claudeDesktopModelRoutes)
MODEL_MAP=claude-opus-4-8=xopdeepseekv4pro,claude-haiku-4-5=xopglm52,claude-sonnet-5=xopdeepseekv4flash0731,claude-fable-5=xopglm52
PORT=8787
```

## 启动

```bash
cd /path/to/vision-proxy && ./start.sh
```

后台常驻:

```bash
cd /path/to/vision-proxy && nohup ./start.sh > proxy.log 2>&1 &
```

自检:

```bash
curl -s http://127.0.0.1:8787/__vision/health
```

应返回 `"vision_configured": true`。

## 让 Claude 桌面端走代理(最后一步,会断当前会话)

⚠️ **这步必须在 CC Switch UI 里做,改 settings.json 对桌面端无效!**
   桌面端 base_url 被 app 进程注入(`CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`),
   不读 `~/.claude/settings.json`。

步骤:
1. 打开 CC Switch app
2. 找到当前选中的「星火」provider(claude-desktop 类型)
3. 编辑它,把 base_url 从
   `https://maas-api.cn-huabei-1.xf-yun.com/anthropic`
   改成 `http://127.0.0.1:8787`
4. 保存,**重启 Claude 桌面端**

重启后新会话即走 vision-proxy。

## 验证

在 Claude 桌面端新会话里贴张截图问"这图里写了什么"。
代理终端会打印:

```
🔄 模型映射 claude-opus-4-8 -> xopdeepseekv4pro
👁  转写 1 张图片,耗时 3.2s
```

## 回滚

如果改完桌面端起不来:
1. CC Switch 里把「星火」base_url 改回
   `https://maas-api.cn-huabei-1.xf-yun.com/anthropic`
2. 重启 Claude 桌面端

链路恢复原样,不影响已建立的会话。

## 设计说明

- **模型映射**: `MODEL_MAP` 环境变量,格式 `a=b,c=d`。替代 cc-switch 的 claudeDesktopModelRoutes,
  让 vision-proxy 直连星火真实端点时也能正确路由到 DeepSeek。
- **缓存**: 按图片内容 sha256 缓存转写结果,存 `~/.cache/vision-proxy/cache.json`。
  同一张图多轮对话只调一次视觉 API。
- **上下文提示**: 同一条消息里的文字作为提示传给视觉模型,让它知道该重点看什么。
- **覆盖 tool_result**: Claude Code 的 `Read` 工具读图产生的是 `tool_result` 里的嵌套 image 块,
  递归遍历同样能抓到。
- **失败降级**: 视觉 API 挂了就把图片替换成 `[图片识别失败:xxx]`,请求照常转发。
- **透明转发**: 除 `/v1/messages` 外的所有路径原样透传,流式 SSE 逐块转发不缓冲。

## 调参

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 8787 | 代理监听端口 |
| `UPSTREAM_BASE_URL` | 星火真实端点 | 上游(直连星火,不走 cc-switch) |
| `MODEL_MAP` | 见上 | Claude 模型名 → 星火模型名映射 |
| `VISION_CONCURRENCY` | 4 | 一条消息多张图时的并发 |
| `VISION_MAX_TOKENS` | 3000 | 单图转写最大长度 |
| `VISION_TIMEOUT` | 120 | 视觉 API 超时(秒) |
| `VISION_CACHE` | `~/.cache/vision-proxy/cache.json` | 缓存文件位置 |

转写质量不满意就改 `vision_proxy.py` 里的 `PROMPT` 常量。
