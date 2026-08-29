## 调用 LLM

管理台在 `http://yosef-server:8817/`；LLM 调用使用同一服务的 `/v1` 接口。调用方先向管理员获取 Client Token，并将其放入 `BROKER_CLIENT_TOKEN` 环境变量。不要使用管理 Token，也不要在请求中提交 `model`；模型由 Broker 按 `intellect` 自动路由。

### 非流式调用

```bash
curl -X POST http://yosef-server:8817/v1/generate \
  -H "Authorization: Bearer $BROKER_CLIENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "用三点总结这份材料：……",
    "intellect": "standard",
    "effort": "medium",
    "deadline_ms": 60000,
    "output_token_limit": 1024
  }'
```

必填字段为 `prompt` 和 `intellect`。`intellect` 只能是 `standard`、`smart` 或 `expert`；`effort`、`deadline_ms`、`output_token_limit` 为可选字段。成功响应含有 `output_text`、实际使用的 `actual_model`、获胜 Provider、`attempts`、用量和 `cost_estimate`。

### 流式调用

将路径换为 `/v1/generate/stream`，服务会返回 Server-Sent Events：每个文本片段是 `event: delta`，结束信息是 `event: final`。

```bash
curl -N -X POST http://yosef-server:8817/v1/generate/stream \
  -H "Authorization: Bearer $BROKER_CLIENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"解释这个概念", "intellect":"smart", "effort":"medium"}'
```

认证失败返回 `401`；没有可用上游或所有竞速 Key 都失败时返回 `503`，响应中的 `attempts` 可用于排查。



## 内容选择流程

比如用 standard + medium 调用：

1. 先找 `standard` 下可路由的 Key：已启用、已校准、该 Key 有此分组模型、未被模型履约校验拉黑、且未达到单 Key 并发上限。
2. 所有这些 Key 不按模型隔离；按“该模型整合价 × Key 倍率”算路由价格，用中位数切成低价组、高价组。
3. 先从低价组随机抽取全局设定的 N 个 Key 并发竞速。谁先返回、且实际模型与该 Key 要求模型一致，谁获胜；其余请求取消。
4. 本批全部失败或模型不符，才进入高价组；当前批中未被抽到的低价 Key 不会继续补抽。
5. `standard` 两个价格组都失败后，依次降级尝试 `smart`、`expert`，每个 stage 同样遵循低价组再高价组。
6. 全部失败则返回 503。
7. `effort=medium` 不参与 Key 选择、价格分组或竞速；它仅透传给 OpenAI 兼容上游的 `reasoning.effort`。Anthropic 兼容调用目前不使用它。
