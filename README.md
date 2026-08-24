# NewAPI Issue #6994

公开报告：[QuantumNous/new-api#6994](https://github.com/QuantumNous/new-api/issues/6994)

- 状态：`closed as not planned`（2026-08-24，维护者 `seefs001`）
- 核对基准：上游 `main` @ `2d8e50bf36`，发行标签当时最新为 `v1.0.0-rc.25`
- 本地脚本：[`newapi_poc.py`](./newapi_poc.py)

本目录复述公开 issue 与上游源码。脚本**没有默认目标**，不带实例口令，默认不打 `/v1/chat/completions`。

## 评价

这不是 0day，是一份没把两套账分开的 vibe 审计稿。

报告把「自己账号能给自己写一把 `unlimited_quota=true` 的 API key」写成 Critical / CVSS 9.1 / 平台垫付。官方前端创建令牌时默认就是无限；维护者关 issue 时写得很清楚：无限额度是**令牌自己的限额**，不是管理员专属，也不是用户钱包。同一份稿子还把登录打成不存在的 `POST /api/user/auth`，把 `service/quota.go:146` 的令牌余额判断说成整站跳过计费。

附件 Word 比网页多一句实话：计费绕过需要账号金额不为 0。有余额就会走钱包预扣，这句话等于自己把「免单」拆掉了。网页 issue 把这句删了。

网络安全如果只靠 vibe、还交给垃圾模型出「审计报告」，就会得到这种东西：字段能写入库，就被写成越权加计费绕过。维护者后补的评论也点了同一件事——至少该拿去给靠谱模型再审一遍，不该把令牌无限当成管理员开关。

剩下的工程问题仍然在：创建/更新接口对 `group` 几乎不校验，能造出一堆运行时 403 的脏行。那是缺校验，不是免费打模型。

## 1. 报告在说什么

报告者 `11ovoQAQ` 的复现只走到写库：

1. 普通用户登录拿到 dashboard `access_token`（`role=1`, `group=default`）
2. `POST /api/token/` 请求体带 `unlimited_quota: true` 和任意 `group`
3. `GET /api/token/` 回读，行已经落库

据此声称三类利用：

- 免费打付费模型，平台承担上游费用
- 再 `POST /api/token/{id}/key` 把无限额度 key 发出去
- 用「用户可用列表里的分组」绕过用户自身分组

配套判断写的是「`service/quota.go:146` 完全跳过 quota」。正文里的登录路径是 `POST /api/user/auth`。公开附件是 issue 上的 Word 和一张 token 列表截图，截图只证明写库成功，没有 completions、没有钱包扣费前后对比。

维护者关 issue 的原文：

1. 无限额度 token 是用户创建/编辑令牌本身就有的功能，限制对象是这一把令牌。
2. 分组只要放进「用户可用分组」，用户就可以创建；当前没有额外限制。之前已经有 issue 提过。

## 2. 源码结论

### 2.1 写入面：报告描述成立

`POST /api/token/`、`PUT /api/token/` 都挂 `middleware.UserAuth()`，已登录普通用户可调。

`controller/token.go` 的 `AddToken` / `UpdateToken`：

- 会检查名字长度、非无限时的 `remain_quota` 上下限、每用户 token 数量
- `group == "auto"` 时才走 `setTokenAutoGroups()` → `service.IsUserSelectableGroup()`
- **不会**拒绝 `unlimited_quota=true`
- **不会**在 `group != "auto"` 时校验该 group 是否属于当前用户

随后把客户端的 `UnlimitedQuota` 和 `Group` 写进 `model.Token`。`Update()` 的 Select 列表同样包含这两列。

前端按这个产品语义做：`web/src/features/keys/lib/api-key-form.ts` 默认 `unlimited_quota: true`，分组下拉来自 `GET /api/user/self/groups`。

### 2.2 计费面：两套账被合成了一套

| 层 | 字段 / 来源 | `unlimited_quota=true` 的效果 |
| --- | --- | --- |
| 令牌额度 | `tokens.remain_quota` / `used_quota` | `ValidateUserToken` 不再因余额 ≤ 0 拒绝；`TryReserveTokenQuota(..., unlimited=true)` 跳过令牌余额检查，只记账 |
| 用户资金 | `users.quota` 或订阅项 | **不跳过**。`NewBillingSession` / `WalletFunding.PreConsume` 仍 `TryReserveUserQuota`；不够返回 `ErrorCodeInsufficientUserQuota` |

`service/quota.go` 约 146 行是 realtime 路径上「令牌余额不够」的判断，前面已经查过用户钱包。不是「平台跳过全部 quota」。unlimited 仍走 `DecreaseTokenQuota`，两列额度不会恒为 0。

信任额度旁路（`shouldTrust`）要求令牌无限**并且**用户钱包高于 `GetTrustQuota()`。单靠无限 token 不会把钱包预扣清零。

### 2.3 分组面：写库宽，转发按可用分组收

`TokenAuth` 在转发前：token.group 必须落在 `GetUserUsableGroups(user.group)` 里，且该组还在倍率表中。默认内置可用分组是 `default` 和 `vip`（`setting/user_usable_group.go`）。站点可以改 `UserUsableGroups`。

因此：

- 默认配置下 `group=vip` 对普通用户是合法可用组，不是隐藏管理员组
- 报告里点名的高价组，只有被该站点放进「用户可用分组」时，运行时才放行
- 写入不在可用列表的名字，行能进库，`/v1/*` 会被 403
- 0 倍率组打付费模型，前提是定价表把该模型的 `enable_groups` 挂进这个组。改 token 组名不会继承别的组的模型目录

这和未合并的 [PR #4289](https://github.com/QuantumNous/new-api/pull/4289) 一致：创建阶段缺校验，主要制造无效数据和运行时 403。

### 2.4 `POST /api/user/auth` 不是登录

上游 `router/api-router.go` 没有这个 handler。现仓只有：

| 路径 | 作用 |
| --- | --- |
| `POST /api/user/login` | 密码登录，`controller.Login` |
| `POST /api/user/auth/refresh` | 用 HttpOnly refresh cookie 换新 access token |
| `POST /api/user/auth/logout` | 撤销当前会话 |

前端登录打的是 `/api/user/login?turnstile=`。Refresh cookie 的 Path 也钉在 `/api/user/auth`，只服务于 refresh/logout。`GET /api/user/auth` 会掉进需要登录的 catch-all，未带 token 时是 `401 AUTH_UNAUTHORIZED`。`POST /api/user/auth` 在未改路由的实例上是 `404 Invalid URL`。

## 3. 实际风险

按官方语义运营的站点（可用分组 = 允许用户自选的通道，无限 token = 不单限这把 key）：

- 「普通用户一键免单」不成立
- 无限 token 是功能
- 把 key 发出去也能追溯到 `user_id`，也能关停

自己把高价通道塞进 `UserUsableGroups` 的站点：

- 用户可以把 token.group 切到这些通道，按该组倍率走模型
- 这是运营配置被 API 执行，不是新的鉴权洞
- 若以为「UI 没画出来的组 API 也选不了」，会被打脸

仍值得修：

1. `AddToken` / `UpdateToken` 对非 `auto` 的 `group` 不校验，脏行会落库
2. 创建成功体不回 token id/key
3. 文档和客服不要把「令牌无限」说成「账号无限」

## 4. 本地怎么验

只打你自己有权测试的实例。不要扫公网、不要用别人的号。

```bash
python3 newapi_poc.py \
  --base-url https://YOUR-INSTANCE \
  --username USER \
  --password PASS \
  --group GROUP_NAME \
  --fetch-key
```

可选：

- `--access-token TOKEN` 跳过密码登录（推荐，遇到 Turnstile / 2FA 时用）
- `--turnstile-token TOKEN` 只转发你在浏览器里已经完成的 Turnstile 值
- `--update-id ID` 再打 `PUT /api/token/`
- `--probe-model MODEL` 才会发 `/v1/chat/completions`（默认关）
- `--skip-create` 只读列表

成功只证明**写库成功**。有没有垫付，看用户钱包和上游账单，不要只看 token 两列额度。

依赖：Python 3.9+ 标准库。

### 4.1 遇到 Cloudflare / Turnstile

停下来。不求解、不重放、不改 UA 硬闯。

- 边缘五秒盾：请求到不了 Go，脚本识别 `cf-mitigated` / challenge HTML 后退出
- 应用内 Turnstile：`TurnstileCheck()` 读 `?turnstile=`，缺 token 时返回 `Turnstile token 为空`
- 2FA：响应是 `require_2fa`，先在官方 UI 做完，再用 `--access-token`

## 5. 修复建议

运营侧：用户可用分组只留真正允许自选的组；高价 / 内部通道不要放进去。独占通道用 `users.group` 加 `+:` / `-:` 规则。不要指望创建接口替你做 ACL。

代码侧最小补丁：写库前按 `GetUserUsableGroups` / `IsUserSelectableGroup` 收口，与 `TokenAuth` 同一函数。`group=="auto"` 继续走现有 auto 子集逻辑。若产品本意是「无限仅管理员可开」，还要改前端默认值，否则和官方 UI 冲突。

PR #4289 已经按 group 校验写过，还没进 `main`。#6994 被按产品设计关闭，不代表写库缺校验不值得修。

## 6. 和报告原文的差异

| 报告原文 | 当前 main |
| --- | --- |
| `POST /api/user/auth` 登录 | 不存在；登录是 `POST /api/user/login` |
| `service/quota.go:146` 跳过全部计费 | 该行只跳过令牌余额；用户钱包在 `NewBillingSession` |
| `used_quota` / `remain_quota` 恒为 0 | unlimited 仍走 `DecreaseTokenQuota` |
| `group=vip` 对普通用户越权 | 默认 `UserUsableGroups` 含 `vip` |
| 无限额度是管理员专属 | 官方 UI 默认开启，维护者已说明 |
| 创建响应能直接读到 token id | 成功体只有 `{success:true}` |

## 7. 范围

脚本和本文覆盖公开 issue、公开附件文本与上游 `main` 静态阅读。不收录第三方实例主机名、账号、口令、会话或账单数字。
