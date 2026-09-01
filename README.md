## 调用 LLM

管理台在 `http://yosef-server:8817/`；局域网或直连网线上的 LLM 调用使用同一服务的 `/v1` 接口，**不需要 Client Token 或 Authorization 请求头**。不要在请求中提交 `model`；模型由 Broker 按 `intellect` 自动路由。

### 非流式调用

```bash
curl -X POST http://yosef-server:8817/v1/generate \
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
  -H "Content-Type: application/json" \
  -d '{"prompt":"解释这个概念", "intellect":"smart", "effort":"medium"}'
```

请求参数不合法返回 `400`；没有可用上游或所有竞速 Key 都失败时返回 `503`，响应中的 `attempts` 可用于排查。



## 内容选择流程

比如用 standard + medium 调用：

1. 先找 `standard` 下可路由的 Key：已启用、已校准、该 Key 有此分组模型、未被模型履约校验拉黑、且未达到单 Key 并发上限。
2. 所有这些 Key 不按模型隔离；按“该模型整合价 × Key 倍率”算路由价格，用中位数切成低价组、高价组。
3. 候选仍按低价组、高价组和 intellect 降级顺序启动；全局 `race_parallel_cap` 限制同时运行数。当前组的候选都已启动后，空闲槽位可以继续补入下一组，单个半开流不会阻塞整条路由。
4. `hedge_delay_ms` 到期仍无合格结果时启动下一个候选；候选失败会立即补位。收到首段文本后若持续一个有效首输出窗口都没有新的文本或 final，则按不完整流失败并释放槽位。
5. 所有唯一候选首轮结束后，可重试的瞬态失败会轮转重试一次，但总启动数严格受 `BROKER_ROUTE_ATTEMPT_BUDGET` 和请求 `deadline_ms` 约束。
6. `structured_output_invalid` 不会被当作成功；它可以在剩余预算内重试，最终仍无合格输出时返回 503，并包含按启动顺序稳定排列的完整 `attempts`。
7. `effort` 不参与价格分组；它会透传给 OpenAI 兼容上游，并调整有效首输出窗口：未指定/low 为 1 倍、medium 为 2 倍、high 为 3 倍。

### 生产形状结构化回放

短 smoke 不能代表长上下文和复杂 JSON Schema。发布验收应额外运行：

```bash
/data/provider-broker/current/venv/bin/python \
  /data/provider-broker/current/production_shape_smoke.py --runs 3
```

该脚本使用约 72k-token 级别的合成文本和复杂 Draft 2020-12 Schema，只输出输入长度、Schema 哈希、模型和 attempt 状态统计，不输出 prompt 或响应正文。
