# Claude Vision Proxy

为 Claude Desktop 和 Claude Code 增加图片输入支持。代理把 Claude 请求中的图片交给任意支持图片输入的 **OpenAI Chat Completions 兼容接口**转换成文字，再将图片替换为转写内容并转发给上游。

## 接口协议

- `VISION_BASE_URL`：视觉模型服务的 OpenAI 兼容 API 根地址。代理会请求其 `/chat/completions` 路径。
- `UPSTREAM_BASE_URL`：Claude 请求的上游，必须兼容 Anthropic Messages API。可以是 CC Switch，也可以是其他 Anthropic 兼容服务。

Claude 客户端发送的是 Anthropic Messages 格式，因此上游仍需支持该协议。本项目不会把 Claude 请求转换成 OpenAI Chat Completions 格式。

```text
Claude Desktop / Claude Code
  -> vision-proxy
     -> 图片转写：OpenAI Chat Completions 兼容视觉接口
     -> 原请求转发：Anthropic Messages 兼容上游
```

## 配置

复制 `.env.example` 为 `.env`，填写自己的服务地址、模型名和密钥。`.env` 不应提交到 Git。

```bash
cp .env.example .env
```

关键配置：

```dotenv
# OpenAI Chat Completions 兼容接口根地址，不要附加 /chat/completions
VISION_BASE_URL=https://your-openai-compatible-provider.example/v1
VISION_API_KEY=replace-with-your-api-key
VISION_MODEL=your-image-capable-model

# Anthropic Messages 兼容上游；默认指向本机 CC Switch
UPSTREAM_BASE_URL=http://127.0.0.1:15721

# 可选：将 Claude 请求中的模型名映射为上游实际模型名
MODEL_MAP=claude-sonnet-4-5=your-upstream-model
PORT=8787
```

`VISION_MODEL` 必须支持图片输入。`MODEL_MAP` 格式为 `请求模型名=上游模型名`，多组用逗号分隔；上游模型名相同时可以留空。

## 启动

```bash
./start.sh
```

代理默认监听本机 `127.0.0.1:8787`。在 Claude 客户端或 CC Switch 中，将 Anthropic API 的 `base_url` 指向：

```text
http://127.0.0.1:8787
```

不要让 CC Switch 同时指向代理、又让代理的 `UPSTREAM_BASE_URL` 指回 CC Switch，以免形成循环。

健康检查：

```bash
curl -s http://127.0.0.1:8787/__vision/health
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VISION_BASE_URL` | 空 | OpenAI 兼容视觉 API 根地址 |
| `VISION_API_KEY` | 空 | 视觉 API 密钥 |
| `VISION_MODEL` | 空 | 支持图片输入的模型名 |
| `UPSTREAM_BASE_URL` | `http://127.0.0.1:15721` | Anthropic Messages 兼容上游 |
| `MODEL_MAP` | 空 | 请求模型名到上游模型名的映射 |
| `PORT` | `8787` | 本地代理端口 |
| `VISION_CONCURRENCY` | `4` | 单个请求中图片识别并发数 |
| `VISION_MAX_TOKENS` | `3000` | 单图转写最大长度 |
| `VISION_TIMEOUT` | `120` | 视觉 API 超时秒数 |
| `VISION_CACHE` | `~/.cache/vision-proxy/cache.json` | 图片转写缓存路径 |

## 工作方式

- 按图片内容的 SHA-256 缓存转写结果，同一张图不会重复调用视觉 API。
- 同一条消息中的文字会作为视觉提示，帮助模型聚焦用户的问题。
- Claude Code 的 `tool_result` 中嵌套图片也会被递归处理。
- 图片转写失败时会替换为错误提示，并继续转发请求。
- 除消息接口外，其余路径和流式响应会透明转发。

可在 `vision_proxy.py` 中调整图片转写提示词 `PROMPT`。
