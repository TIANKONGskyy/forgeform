# 封装规范清单 —— 一句一条

> 铸想把本机能力开成 API 时，**照抄现成标准**，不自己发明。
> 下面每条都是「一句话 + 我们怎么落地的」。验收时逐条对照。

## 一、接口描述

1. **OpenAPI 3.1.0** —— 接口描述用 `GET /openapi.json` 输出，字段名照规范，不另造一套。
2. **JSON Schema 2020-12** —— 请求体/响应体里的类型定义用 JSON Schema 写，不写自然语言当契约。
3. **语义化版本（SemVer 2.0.0）** —— `info.version` 用 `主.次.修订`，破坏性改动才动主版本。
4. **`servers` 写真实地址** —— 至少给 `http://127.0.0.1:7863`，别人不看文档也知道往哪打。
5. **每个 endpoint 必须有 `summary`** —— 一行说清"干什么"，不写"处理请求"这种废话。

## 二、认证与授权

6. **RFC 6750（Bearer Token）** —— 认证头固定 `Authorization: Bearer <token>`，不用自定义头。
7. **RFC 6749（OAuth 2.0）** —— 暂不做授权码流程；只保留 Bearer，避免"半个 OAuth"更难用。
8. **RFC 7519（JWT）** —— 本机 key 不用 JWT：对称签发、可随时撤销，比自签 JWT 安全且简单。
9. **密钥不可回读** —— 服务端只存 `sha256`，明文只在签发那一刻显示一次（`tools/api_key.py`）。
10. **密钥静态加密** —— 云厂商 key 用 Windows DPAPI（`backend/secrets.py`），绑定用户+机器。
11. **不读环境变量取 key** —— 环境变量会被子进程继承，等于把 key 递给每一个被调起的程序。

## 三、错误

12. **RFC 9457（Problem Details）** —— 错误体统一 `application/problem+json`，字段 `type/title/status/detail`。
13. **HTTP 状态码按语义用** —— 400 参数错、401 未认证、403 无权、404 不存在、409 冲突、413 太大、422 语义错、429 限流、500 内部错。
14. **错误里不泄露内部路径** —— `detail` 只给"哪里错了"，不贴堆栈和磁盘路径。

## 四、限流与背压

15. **RFC 6585（429 + `Retry-After`）** —— 超限返回 429 并带 `Retry-After`，让客户端知道等多久。
16. **读/写分档限流** —— 生成类 6 次/分钟，查询类 240 次/分钟（轮询是客户端义务，不该被同一档打死）。
17. **并发上限显式** —— 同时最多 2 个生成任务，多出来的排队而不是拒绝，队列深度在 `/v1/health` 可见。

## 五、长任务

18. **202 Accepted + `Location`** —— 提交生成立即返回 202 和任务 URL，不在 HTTP 里同步等十分钟。
19. **任务状态机固定** —— `queued → running → succeeded | failed`，客户端只认这四个值。
20. **进度可读** —— 返回 `stage`（image/mesh/stl）+ `percent` + `elapsed_sec`，前端不用猜。
21. **产物用独立 URL 取** —— `GET /v1/jobs/{id}/files/{kind}`，带 `Content-Disposition: attachment`。
22. **`Content-Type` 说真话** —— 图片给 `image/png`，模型给 `model/gltf-binary`，不一律 `octet-stream`。

## 六、兼容

23. **OpenAI Chat Completions 形状** —— `/v1/chat/completions` 按 OpenAI 的 `choices[0].message.content` 返回，现成 SDK 直接能用。
24. **OpenAI Images 形状** —— `/v1/images/generations` 返回 `data[0].b64_json`，和官方一致。
25. **`/v1/models` 用 OpenAI 的列表形状** —— `{"object":"list","data":[{id,object,owned_by}]}`，多余字段放扩展键。
26. **不发明新的 MIME** —— 模型给 `model/gltf-binary`，不用 `application/x-forgeform-mesh` 这种私有类型。

## 七、时间与单位

27. **RFC 3339 时间** —— 需要可读时间的地方给 `2026-11-15T19:30:00+08:00`；接口里同时给 `ts`（Unix 秒）便于计算。
28. **单位写进字段名** —— `elapsed_sec`、`size_mm`、`vram_used_mb`，不写 `size`/`elapsed` 让调用方猜。
29. **`size_mm` 用毫米** —— 3D 打印域的唯一单位，英寸换算交给客户端。

## 八、跨域与安全

30. **默认只监听 `127.0.0.1`** —— 要对外再显式 `--host 0.0.0.0`，默认不把服务暴露到局域网。
31. **CORS 默认不放行** —— 浏览器跨域调用需要显式配置，不写 `Access-Control-Allow-Origin: *`。
32. **`X-Content-Type-Options: nosniff`** —— 防浏览器把 JSON 当 HTML 执行。
33. **`Cache-Control: no-store`** —— 生成的产物状态不缓存，避免拿到上一轮的结果。
34. **请求体上限 32 MB** —— 超了直接 413，不给内存打爆的机会。

## 九、幂等与重试

35. **IETF `Idempotency-Key`（草案）** —— 同一 key 重复 POST `/v1/generate` 视为同一任务（规划中，见下方待办）。
36. **客户端重试只对 429/5xx** —— 401/404/422 重试没有意义，文档里写清楚。

## 十、可观测

37. **`/v1/health` 免鉴权** —— 探活不要求 key，返回 `{ok,queue,running,jobs,ts}`。
38. **访问日志一行一请求** —— `[api] 127.0.0.1 "POST /v1/generate HTTP/1.1" 202`。
39. **运行信息滚动留存** —— GPU/内存/CPU/磁盘每 3 分钟落一行 `logs/metrics-*.jsonl`，保留 14 天。
40. **产物与作品分开清理** —— `outputs/` 中间产物 30 天可清；`projects/` 作品永不清理。

## 十一、文档

41. **`/docs` 给人看** —— 手写 HTML 页，一屏看清"提交 → 查状态 → 取文件"三步。
42. **`/openapi.json` 给机器看** —— 可被 Swagger UI / Postman / 代码生成器直接吃。
43. **示例用 `$KEY` 占位** —— 文档里的 curl 示例不写真实 key，避免复制泄漏。

## 待办（还没做，别假装做了）

- **`Idempotency-Key`**（第 35 条）：需要服务端存 24h 的 key→job 映射表。
- **SSE 流式进度**（`text/event-stream`）：现在只能轮询，流式更省请求。
- **多 key 配额**：现在所有 key 共用 6 次/分钟，按 key 单独配额还没做。
- **审计日志落盘**：调用记录只在内存计数，没有落 `logs/api-*.jsonl`。

## 怎么验

```bash
PYTHONPATH= python tools/api_key.py issue --label me     # 拿一把 ff_ key
PYTHONPATH= python tools/api_server.py                    # 起服务（127.0.0.1:7863）

curl -s http://127.0.0.1:7863/v1/health                   # 免鉴权
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:7863/v1/models
curl -s http://127.0.0.1:7863/openapi.json | python -m json.tool | head
```

第 1–43 条里，**1–34 已实现并实测**（假云端 + 真本机 LLM 都跑过）；**35–36、SSE、配额、审计**在待办里。
