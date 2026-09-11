# 中转站余额 API 调研（2026-09-02）

目标是让小电脑上的 Provider Broker 以长期 API 凭据查询余额，不依赖浏览器 Cookie 或绕过 Cloudflare。

## 结论

| 站点 | 产品/接口证据 | 可用的长期凭据方案 | 建议 |
| --- | --- | --- | --- |
| 凉热葵 `api.liangrekui.com` | New API；`GET /api/user/self` | 用户级 Access Token（PAT） | 支持，优先实现 |
| 可乐 AI `code28.ccwu.cc` | New API；`GET /api/user/self` | 用户级 Access Token（PAT） | 支持，优先实现 |
| Top-API `api-top.com` | New API；`GET /api/user/self` | 用户级 Access Token（PAT） | 支持，优先实现 |
| WawAPI `wawapii.com` | `GET /v1/usage` 要求 API Key | 专用 API Key | 支持，需用真实只读调用确认返回字段 |

三家 New API 站点的公开 `/api/status` 均返回 200，且分别报告了自己的站点名、`quota_display_type` 与 New API 功能配置。2026-09-02 的无凭据探测显示：对 `/api/user/self` 使用故意无效的 Bearer 令牌，凉热葵返回 HTTP 401；可乐 AI 和 Top-API 返回 HTTP 200 的结构化 `success:false` “invalid access token”。这证明查询路由可从小电脑访问，认证失败不是 Cloudflare 挑战页。

同日对 `GET /api/user/token` 的无凭据路由判定结果为：凉热葵、可乐 AI、Top-API 均返回 HTTP 401 的结构化“未登录/无效 access token”，因此接口实际存在且只缺已登录用户授权；WawAPI 返回 404，不具备 New API 的用户级 Access Token 路由。

WawAPI 的 `/v1/usage` 在无 Authorization 时返回 `API_KEY_REQUIRED`，使用故意无效的 Bearer 值时返回 `INVALID_API_KEY`；其 `/v1/models` 同样要求认证。这是 API Key 查询，而不是浏览器登录会话。

## New API 的标准方式

官方 New API 文档把 `GET /api/user/self` 定义为获取当前用户详细信息（含配额）的用户接口，并给出 `Authorization: Bearer <user access token>` 的调用方式；旧版本示例同时带有 `New-Api-User` 头。[官方文档](https://github.com/QuantumNous/new-api-docs/blob/main/docs/api/fei-user.md#获取个人资料)

上游当前源码显示该响应包含 `quota`、`used_quota` 等字段；其中 `quota` 是原始配额点数，不可直接当作页面显示的货币余额。[`GetSelf` 实现](https://github.com/QuantumNous/new-api/blob/main/controller/user.go#L489-L543) 官方文档说明默认换算为 `1 USD = 500,000 quota`，而当前实现会按站点的 `quota_per_unit`、`quota_display_type` 和 `usd_exchange_rate` 换算展示金额。[费率文档](https://github.com/QuantumNous/new-api-docs/blob/main/docs/en/guide/console/settings/rate-settings.md) [`/dashboard/billing/subscription` 实现](https://github.com/QuantumNous/new-api/blob/main/controller/billing.go)

用户级 Access Token 可由已登录用户调用 `GET /api/user/token` 生成。该操作会更新账户的 Access Token，因此只能在人工确认时做一次，不能由定时任务调用。[官方文档](https://github.com/QuantumNous/new-api-docs/blob/main/docs/api/fei-user.md#生成用户级别-access-token) [当前源码](https://github.com/QuantumNous/new-api/blob/main/controller/user.go#L412-L437)

当前源码还表明，用户级 Access Token 本身足以从 Bearer 头识别用户；Broker 不应保存密码或 Cookie 来刷新它。[认证中间件](https://github.com/QuantumNous/new-api/blob/main/middleware/auth.go#L131-L181)

## 建议的 Broker 接入

新增“API 凭据”方式，而不是复用现有 Cookie 导入：

```text
GET https://<site>/api/user/self
Authorization: Bearer <用户级 Access Token>
Accept: application/json
```

Broker 只加密保存该令牌，并在保存时立即验证一次；后续 15 分钟余额任务使用相同调用。每次配置/每日缓存刷新时还应读取无认证的 `/api/status`，按 `quota_per_unit` 与站点展示货币换算 `data.quota`；不能沿用目前把 `quota` 原样显示的逻辑。认证失效则记录“令牌失效”，触发已有的采集失败/余额过期告警，不回退到账号密码登录。

对于 WawAPI：

```text
GET https://wawapii.com/v1/usage
Authorization: Bearer <专用 API Key>
```

在用户提供专用 Key 后，应先只调用一次，依据真实响应确定余额字段和单位；不要用生产调用 Key 做试探性模型请求。

## 人工获取凭据的安全边界

- 用户在各站点已登录的本地 Chrome 中自行生成或复制 Access Token/API Key；不要发送到聊天。
- 凉热葵、可乐 AI、Top-API：优先从“个人中心/设置”寻找 Access Token；若站点未提供按钮，可在已登录的该站点 Console 中由用户执行 `fetch('/api/user/token', {credentials:'include'}).then(r => r.json())`，再只把返回 `data` 粘贴到 Broker 管理页。该调用可能轮换旧 PAT。
- WawAPI：新建一个仅用于余额查询、没有 IP 限制或已允许小电脑出口 IP 的 API Key。
- 不使用 Cookie 方案作为长期机制，也不尝试绕过 Turnstile 或 Cloudflare。
