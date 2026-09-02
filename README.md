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
3. 将低价组中健康且有容量的 Key 先随机打散，再按真实成功证据、平滑成功率和 TTFT 稳定排序；同质量候选仍保持随机负载均衡。`race_parallel_cap`（N）限制同时运行数。`hedge_delay_ms` 到期仍无合格结果时启动下一个候选，失败会立即补位；当前组全部启动后，空闲槽位可继续补入下一组。候选必须产生非空文本 token，且实际模型满足请求档位才能获胜；其余已启动请求取消。`unknown` 与 `suspect` 仍可参与路由，`open` 不参与正常路由。
4. 当前低价组的候选队列耗尽、达到整请求尝试预算（默认 32）或 deadline 后，才进入高价组。
5. `standard` 两个价格组都失败后，依次降级尝试 `smart`、`expert`，每个 stage 同样遵循低价组再高价组。
6. 全部失败则返回 503。
7. `effort=medium` 不参与 Key 选择、价格分组或竞速；它仅透传给 OpenAI 兼容上游的 `reasoning.effort`。Anthropic 兼容调用目前不使用它。

## 自适应健康探针与熔断

`/models` 仍然只负责发现 Provider 声明的模型库存；它不代表某个 API Key 能真实完成生成。Broker 因此为每个 **Provider + model** 维护独立健康状态，并将业务调用的被动证据与主动探针严格分开。

- 正常生成请求内部统一请求上游流式响应，记录真实的首个非空文本 token 延迟（TTFT）、实际模型和调用结果。普通调用继续写入调用记录、质量统计和费用统计。
- 新同步到的 Provider/model 会进入 `unknown`，并被安排验证；已有真实业务成功时会转为 `healthy`，不需要为每次请求额外付费探测。
- 一次可归因失败会转为 `suspect`，仍可参与路由；连续三次失败会转为 `open`，从正常路由中排除。实际模型不匹配会保留原有的 route block，并立即打开该 model 的健康熔断。
- `open` 状态在冷却期后由恢复探针检查。成功探针进入 `half_open`，随后一个真实成功调用恢复为 `healthy`；失败则按 2、5、15、30、60 分钟的退避序列再次冷却。
- 后台调度器只探测新目标、过期的被动证据、异常目标和到期的熔断恢复项，并增加少量抖动；不会每分钟扫描并付费请求全部 Key。
- 探针使用固定提示词 `只输出1`、禁用工具、一个可见输出 token、低推理强度与流式协议。探针事件单独存储，绝不写入普通调用、质量汇总、普通成本或 route block。

### 中转站余额

管理台新增“中转站余额”区，支持凉热葵、可乐AI、WawAPI 和 Top-API。每个站点点击“登录”后输入账号和密码；凭据、会话令牌和告警 Webhook 仅以 Broker 的 AES 密钥加密保存，接口和页面不会回显它们。

若站点要求 Turnstile 等人机验证，使用“网页登录”：管理页会打开小电脑上的远程 Chrome（仅直连网线可访问）。在该浏览器中完成网站自身的登录和验证码后，回到管理页点击“验证完成”。会话留在小电脑的浏览器配置中；Broker 只在浏览器内请求余额，不导出 Cookie 或令牌。网页查看器为 `http://yosef-server:8818/vnc.html`，并仅允许直连客户端 `192.168.50.1` 访问。

如果 Cloudflare 拒绝小电脑浏览器，可点击“导入 Cookie”，由操作者手动粘贴本地 Chrome 同一站点请求的 `Cookie` 与 `User-Agent` 请求头。该会话只会加密写入小电脑，导入页不会回显它；适用于 New API 站点。Cloudflare 会话可能绑定浏览器和网络，失效后需要重新导入。

已登录站点每 15 分钟刷新一次（可用 `BROKER_BALANCE_SCHEDULER_SECONDS` 调整），也可在管理台立即更新。默认低余额阈值为凉热葵 ¥20、其余站点 $5，均可逐站修改。首次低于阈值时会向配置的 HTTPS Webhook 发送 JSON 告警；余额恢复到阈值以上后自动重新布防。

### 管理接口

查看某个 Stage 的最新健康证据：

```bash
curl 'http://yosef-server:8817/admin/v1/health?stage=standard'
```

手动进行一次与生产价格组选择相同的竞速探针：

```bash
curl -X POST http://yosef-server:8817/admin/v1/probes \
  -H 'Content-Type: application/json' \
  -d '{"stage":"standard","mode":"race"}'
```

`mode: "all"` 会逐个检查指定 Stage 的所有可探测 Provider/model；两个模式都可附加 `fingerprint`、`model`、`timeout_ms` 和 `concurrency` 以缩小范围或控制执行上限。管理台的 Stage 视角提供相同的竞速探针、全量探针和最新结果表，并保存选定 Stage 与表格排序。

可用环境变量：`BROKER_HEALTH_STALE_SECONDS`（默认 1800）、`BROKER_PROBE_TIMEOUT_MS`（默认 15000）、`BROKER_PROBE_CONCURRENCY`（默认 2）、`BROKER_HEALTH_SCHEDULER_SECONDS`（默认 60）、`BROKER_FIRST_EVENT_TIMEOUT_MS`（默认 30000）、`BROKER_STREAM_IDLE_TIMEOUT_MS`（默认 90000）、`BROKER_ATTEMPT_TIMEOUT_MS`（默认 180000）、`BROKER_RESPONSE_RESERVE_MS`（默认 5000）和 `BROKER_ROUTE_ATTEMPT_BUDGET`（默认 32）。首事件窗口按 effort 调整：low 或未指定为 1 倍、medium 为 2 倍、high 为 3 倍；reasoning 事件只表示活性，不会拼入输出。

长结构化输出可用 `/data/provider-broker/current/venv/bin/python /data/provider-broker/current/production_shape_smoke.py --runs 3` 做生产形态验证。该脚本使用长输入、复杂 Draft 2020-12 Schema 和精确 source span，只输出长度、结构计数、Schema 哈希、模型和安全状态统计，不输出正文。

路由设置接口 `GET/PATCH /admin/v1/routing` 同时管理 `race_parallel_cap` 与 `hedge_delay_ms`；两项可单独更新，后者的可选范围为 0–10000 ms。
