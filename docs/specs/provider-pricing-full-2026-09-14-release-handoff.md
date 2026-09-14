# Provider Pricing Full Cleanup — Release Handoff

日期：2026-09-14
验收基线：`3d1c6dbe3a3027a4c74e5233ffa6f64d782dd1c6`

## 结论

本地代码与自动化验收已通过，可交付部署前环境 smoke。未执行真实生产环境请求，因此最终发布仍需在目标环境完成一次受控启动与公共接口检查。

## 本次收口

- 管理面移除旧 Model directory、独立 health/probe/provider-key-test 路由；Stage 测试保留为唯一操作，并只返回计数摘要。
- Provider+Model 价格种子收敛为单一 `official_output_price_cny`；运行时输入、缓存、输出成本均从同一 CNY output price 推导。
- 保留必要的历史观测、迁移表和兼容数据库列，但旧数据不再进入公共管理契约或可路由价格解析。
- Stage 资源继续动态从可用 Provider+Model 组合生成，低/高价带按 output price 划分；未知价格展示为 `n/a`。
- Provider pricing identity 继续限制为六个批准类型：OpenAI、Anthropic、DeepSeek、Qwen、Doubao、DeepInfra。

## 验收证据

| 检查 | 结果 |
|---|---|
| `python -m pytest -q` | 177 passed |
| `node --check provider_broker/web/app.js` | passed |
| `python -m compileall -q provider_broker scripts` | passed |
| `git diff --check` | passed |
| 旧管理路由 404 合同测试 | passed |
| Web E2E：无旧 probe/model-directory 控件 | passed |
| 真实生产服务 smoke | 未执行：当前工作区无目标服务地址/授权上下文 |

## Review 结论

Standards：未发现新的、需要阻断交付的编码规范问题。
Spec：未发现当前 SPEC #85 / Ticket #95 要求之外的遗漏；数据库中的历史兼容结构是有意保留，并已与公共契约和运行时路径隔离。

## 交接动作

1. 在目标部署环境启动服务。
2. 验证 `/admin/v1/providers`、`/admin/v1/stages`、`/admin/v1/pricing`、`/admin/v1/analytics`、`/admin/v1/routes` 的状态码和字段白名单。
3. 验证 `/admin/v1/models`、`/admin/v1/health`、`/admin/v1/probes`、`/admin/v1/providers/test` 均不再作为管理契约暴露。
4. 以一条真实受控请求确认费用记录使用 CNY output price，随后再执行发布。

本次未提交或推送 Git；工作区中既有用户改动均保留。
