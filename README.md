# NewAPI Issue #6994

公开报告：[QuantumNous/new-api#6994](https://github.com/QuantumNous/new-api/issues/6994)

- 状态：`closed`（2026-08-24，维护者 `seefs001`）
- 仓库：`QuantumNous/new-api`
- 核对基准：`main` @ `2d8e50bf36`（2026-08-21），最新发行 `v1.0.0-rc.25`
- 本地脚本：[`newapi_poc.py`](./newapi_poc.py)

本目录只复述公开 issue 和当前 `main` 源码。脚本默认不打上游模型，也不带任何实例口令。

## 1. 报告在说什么

报告者 `11ovoQAQ` 对自建实例（文中写了 `ctoken.top`，版本只填了「最新」）给出这条链：

1. 普通用户登录拿到 `access_token`（`role=1`, `group=default`）
2. `POST /api/token/` 请求体直接带：
   - `unlimited_quota: true`
   - `group: vip` / `claude-kiro` 等
3. `GET /api/token/` 回读，行已经落库
4. 据此声称三类利用：
   - 免费打付费模型，平台承担上游费用
   - 再 `POST /api/token/{id}/key` 把无限额度 key 发出去
   - 用「用户可用列表里的高倍率分组」绕过用户自身分组

配套判断写的是「`service/quota.go:146` 完全跳过 quota」。

维护者关 issue 时的原文：

1. 无限额度 token 是用户创建/编辑令牌本身就有的功能，限制对象是这一把令牌，不是用户钱包。
2. 分组只要放进「用户可用分组」，用户就可以创建；当前没有额外限制。之前已经有 issue 提过。

## 2. 源码结论

### 2.1 写入面：报告描述成立

`POST /api/token/`、`PUT /api/token/` 都挂 `middleware.UserAuth()`，任意已登录普通用户可调。

`controller/token.go` 的 `AddToken` / `UpdateToken`：

- 会检查名字长度、非无限时的 `remain_quota` 上下限、每用户 token 数量
- `group == "auto"` 时才走 `setTokenAutoGroups()` → `service.IsUserSelectableGroup()`
- **不会**拒绝 `unlimited_quota=true`
- **不会**在 `group != "auto"` 时校验该 group 是否属于当前用户

随后直接写入：

```go
cleanToken := model.Token{
    UserId:         c.GetInt("id"),
    RemainQuota:    token.RemainQuota,
    UnlimitedQuota: token.UnlimitedQuota,
    Group:          token.Group,
    // ...
}
```

`Update()` 的 Select 列表同样包含 `unlimited_quota` 和 `group`。

所以普通用户确实能把这两列写成任意值。这不是绑定失败，是字段信任客户端。

前端也按这个产品语义做的：`web/src/features/keys/lib/api-key-form.ts` 默认 `unlimited_quota: true`，分组下拉来自 `GET /api/user/self/groups`。UI 不会阻止用户开无限额度。

### 2.2 计费面：报告把两层额度合成了一层

NewAPI 一次转发有两套账：

| 层 | 字段 / 来源 | `unlimited_quota=true` 的效果 |
| --- | --- | --- |
| 令牌额度 | `tokens.remain_quota` / `used_quota` | `ValidateUserToken` 不再因余额 ≤ 0 拒绝；`TryReserveTokenQuota(..., unlimited=true)` 跳过余额检查，只记账 |
| 用户资金 | `users.quota` 或订阅项 | **不跳过**。`NewBillingSession` 仍要求钱包余额 > 0 且够预扣；不够返回 `ErrorCodeInsufficientUserQuota` |

相关点：

- `model/token.go` `ValidateUserToken`：`if !token.UnlimitedQuota && token.RemainQuota <= 0`
- `model/quota_reserve.go` `TryReserveTokenQuota`：unlimited 分支 `return true, DecreaseTokenQuota(...)`
- `service/billing_session.go` `tryWallet()`：先 `GetUserQuota`，`userQuota <= 0` 或不够预扣直接 403
- `service/quota.go` `PreWssConsumeQuota` 约 146 行：那是 realtime 路径上「令牌余额不够」的判断，前面已经查过 `userQuota`。不是「平台跳过全部 quota」

因此：

- 用户把 token 设成无限，只表示这把 key 不再单独限流额度
- 用户钱包 / 订阅仍会被预扣和结算
- `used_quota` / `remain_quota` 在 unlimited 路径上仍会更新；报告里「两列永远是 0」与当前 `DecreaseTokenQuota` 不符
- 平台会不会垫付上游费用，取决于**用户钱包有没有钱**，不取决于 token 是否无限

信任额度旁路（`shouldTrust`）也要求 **token 无限或 token 余额高，并且用户钱包余额高于 `GetTrustQuota()`**。单靠无限 token 不会把用户钱包预扣清零。

### 2.3 分组面：写库宽、转发按「可用分组」收

`TokenAuth`（`middleware/auth.go`）在转发前：

```go
if tokenGroup != "" {
    if _, ok := service.GetUserUsableGroups(userGroup)[tokenGroup]; !ok {
        abort ... "无权访问 %s 分组"
    }
    if !ratio_setting.ContainsGroupRatio(tokenGroup) && tokenGroup != "auto" {
        abort ... "分组 %s 已被弃用"
    }
    userGroup = tokenGroup   // 用 token.group 覆盖用户自身分组
}
```

默认内置可用分组（`setting/user_usable_group.go`）：

```go
"default": "默认分组",
"vip":     "vip分组",
```

站点还可以在系统设置里改 `UserUsableGroups`。`GetUserUsableGroups()` 还会把用户自己的 group 并进去。

所以：

- `group=vip` 在**默认配置**下对普通用户是合法可用分组，不是越权
- 报告里的 `claude-kiro` / `codex-*` 只有被该站点放进「用户可用分组」时，运行时才放行
- 若写入一个不在可用列表、也不等于用户自身 group 的名字，行能进库，但 `/v1/*` 会被 `TokenAuth` 403
- playground 路径额外用 `GroupInUserUsableGroups` 挡请求体里的 group；普通 `/v1` 不再二次校验，因为它已经信 `UsingGroup`

这和未合并的 [PR #4289](https://github.com/QuantumNous/new-api/pull/4289) 描述一致：创建阶段缺校验，主要制造无效数据和运行时 403，不是单独再开一条管理员通道。

相关后续：

- [#6980](https://github.com/QuantumNous/new-api/issues/6980)：限速组跟了 token 路由组，用户可以选更松的可用组来躲用户组限速
- [#6986](https://github.com/QuantumNous/new-api/issues/6986)：草案「严格分组隔离」，把可用组钉死成用户自身 group

## 3. 实际风险

对**按官方语义运营**的站点（可用分组 = 你允许用户选的通道，无限 token = 不单限这把 key）：

- #6994 描述的「普通用户一键免单」不成立
- 无限 token 是功能，不是 0day
- `vip` 出现在默认可用列表里，是配置问题，不是隐藏管理员组

对**自己改过可用分组 / 把高价通道塞进 `UserUsableGroups`** 的站点：

- 普通用户可以把 token.group 切到这些通道，按该组倍率走模型
- 这是运营配置被 API 忠实地执行，不是新的鉴权洞
- 若运营以为「UI 没画出来的组用户就用不了」，那是误判：API 只看可用分组表

对**误把 `unlimited_quota` 理解成用户钱包无限**的二次开发 / 文档：

- 这是产品语义坑。当前主线代码没有把用户钱包和 token 无限绑在一起
- 不要在自定义计费补丁里只看 `token.UnlimitedQuota`

仍值得修的工程问题：

1. `AddToken` / `UpdateToken` 对 `group` 只在 `auto` 子集上校验，非法组会落库
2. 创建接口成功响应不回 token id/key（[#2908](https://github.com/QuantumNous/new-api/issues/2908)、[#6990](https://github.com/QuantumNous/new-api/issues/6990)），调用方只能再 list
3. 报告里的登录路径写错了：当前是 `POST /api/user/login`，不是 `/api/user/auth`

## 4. 本地怎么验

脚本只打你指定的实例。不要拿它扫公网、不要用别人的号。

```bash
python3 newapi_poc.py \
  --base-url https://YOUR-INSTANCE \
  --username USER \
  --password PASS \
  --group vip \
  --fetch-key
```

可选：

- `--update-id ID` 再打 `PUT /api/token/`
- `--probe-model MODEL` 才会发 `/v1/chat/completions`（默认为关）
- `--skip-create` 只读列表 / 改已有 token
- `--access-token TOKEN` 跳过密码登录
- `--turnstile-token TOKEN` 只转发你在浏览器里已经完成的 Turnstile 值

成功时你应看到：

- 登录用户 `role` / `group`
- 该用户当前可用分组
- 新 token 的 `unlimited_quota=true` 以及写入的 `group`

这只证明**写库成功**。要判断有没有垫付，去看该用户钱包扣没扣、上游账单有没有出账，不要只看 token 两列额度。

依赖：Python 3.9+ 标准库，无第三方包。

## 4.1 Login 遇到 Cloudflare / Turnstile 时怎么处理

原则：**停下来，不求解、不重放、不改 UA 硬闯。** 机器人管理页和验证码都是站点边界，脚本只识别然后退出。

NewAPI 自己的登录链是：

- 边缘：站点若挂了 Cloudflare 五秒盾 / WAF / Bot Fight，`/api/user/login` 可能先回 HTML 挑战，请求根本到不了 Go
- 应用：`POST /api/user/login` 挂了 `middleware.TurnstileCheck()`。开关是 `TurnstileCheckEnabled`。它读的是 query `?turnstile=`，再由服务端拿 `TurnstileSecretKey` 去 `challenges.cloudflare.com/turnstile/v0/siteverify`
- 另：登录成功后若用户开了 2FA，响应是 `require_2fa` + `flow_token`，不是 access_token

脚本对应行为：

1. 响应头有 `cf-mitigated`，或 body 像 Cloudflare 等待页 / challenge-platform：直接退出，提示去浏览器完成
2. JSON `success=false` 且 message 含 `Turnstile`：同样退出。缺 token 时 NewAPI 原文是 `Turnstile token 为空`
3. 不内置 Turnstile 控件，不打 siteverify，不保存 `cf_clearance`
4. 你在官方页面用真人手势过完验证后，二选一：
   - 把登录响应里的 `access_token` 交给 `--access-token`（推荐，完全不再碰 `/login`）
   - 或把页面控件给出的 Turnstile 一次性值交给 `--turnstile-token`，脚本只把它原样放到 `?turnstile=`
5. 2FA 同样停。先在官方 UI 做完，再用 `--access-token`

不要做的事：换 TLS 指纹、刷 `cf_clearance`、调第三方打码、改脚本伪装成浏览器去过盾。那些是绕过，这里不做。

自建实例要避免登录被挡：本机 / 内网不要挂五秒盾；Turnstile 只对浏览器登录开，给自己的审计号发 PAT / 已有 session，而不是让脚本去解挑战。


## 5. 修复建议

按你是不是这套产品的维护者，分两层。

### 5.1 站点运营（不改代码也能做）

1. 打开管理端「用户可用分组」，**只留你真正允许所有用户自选的组**。高价 / 内部通道不要放进去。
2. 需要独占通道时，用用户自身 `users.group` + 特殊可用分组规则（`+:` / `-:`），不要指望 token 创建接口替你做 ACL。
3. 审计已有 token：

```sql
SELECT id, user_id, name, `group`, unlimited_quota, remain_quota, used_quota
FROM tokens
WHERE deleted_at IS NULL
  AND (
    unlimited_quota = 1
    OR (`group` IS NOT NULL AND `group` NOT IN ('', 'default', 'auto'))
  );
```

4. 对异常无限 token：关停或改回有限额度；对错组 token：改回用户自身组或删除。
5. 不要把「token 无限」展示成「账号无限」。文档和客服话术分开两层额度。

### 5.2 代码修复（给维护者 / 自建 fork）

最小补丁：创建和更新时按当前用户可用分组收口，与 `TokenAuth` 使用同一函数。

```go
func normalizeTokenGroup(c *gin.Context, token *model.Token) bool {
    group := token.Group
    if group == "" || group == "auto" {
        return true
    }
    userGroup, err := getTokenRequestUserGroup(c)
    if err != nil {
        common.ApiError(c, err)
        return false
    }
    if !service.GroupInUserUsableGroups(userGroup, group) && group != userGroup {
        common.ApiErrorI18n(c, i18n.MsgTokenGroupNotAllowed, map[string]any{"Group": group})
        return false
    }
    if !ratio_setting.ContainsGroupRatio(group) {
        common.ApiErrorI18n(c, i18n.MsgTokenGroupDeprecated, map[string]any{"Group": group})
        return false
    }
    return true
}
```

在 `AddToken` / `UpdateToken` 写库前调用。`group=="auto"` 继续走现有 `setTokenAutoGroups`。

可选加固：

- 若产品本意是「无限额度仅管理员可开」，在写 `UnlimitedQuota` 前检查 `c.GetInt("role") >= common.RoleAdminUser`，普通用户强制 `false`。这与当前官方 UI 默认值冲突，改之前先改前端默认。
- 创建响应带回 `id` 和掩码 key，避免客户端靠重名猜测。
- 补回归：普通用户写不在可用列表的 group 应 4xx；写可用 group + unlimited 应 200；`TokenAuth` 对库中历史脏组仍 403。
- 若要修「选更松的可用组躲用户组限速」，看 #6980，不要和本 issue 混成一个补丁。

PR #4289 已经按这个方向写过 group 校验，但还没进 `main`。#6994 本身被维护者按产品设计关闭，不代表写库缺校验不值得修。

## 6. 和报告原文的差异

| 报告原文 | 当前 main |
| --- | --- |
| `POST /api/user/auth` | `POST /api/user/login` |
| `service/quota.go:146` 跳过全部计费 | 该行只跳过**令牌**余额；用户钱包在 `NewBillingSession` |
| `used_quota` / `remain_quota` 恒为 0 | unlimited 仍走 `DecreaseTokenQuota` |
| `group=vip` 对普通用户越权 | 默认 `UserUsableGroups` 含 `vip` |
| 创建响应里能直接读到 token id | `AddToken` 成功体只有 `{success:true}`，要再 `GET /api/token/` |

## 7. 范围

脚本和本文只覆盖公开 issue 与当前 `main` 静态阅读。没有打 `ctoken.top`，没有对第三方实例做利用验证。
