# cpa-quota-warmup

## English summary

`cpa-quota-warmup` is a native plugin (Go, cgo `c-shared`) for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (CPA). At a scheduled time of day it sends a tiny chat-completion request (`"hi"`, `max_tokens: 16`) to each enabled account, using the cheapest model that provider currently exposes, to warm up the account's 5-hour quota window before the first real request of the day hits it cold. It needs no credentials of its own: it authenticates as an ordinary client using CPA's own configured `api-keys`.

### Install

**Option A — CPA plugin store (recommended).** In the management console open 插件商店 → install from GitHub repository `szxypi/cpa-quota-warmup`. Every release ships store-compatible assets: `cpa-quota-warmup_<ver>_linux_amd64.zip`, `cpa-quota-warmup_<ver>_linux_arm64.zip` and `checksums.txt` (the zip contains `cpa-quota-warmup.so` at its root, which is exactly the layout `internal/pluginstore` expects).

> If the store fails with `unexpected status 403 ... API rate limit exceeded`, that is GitHub's anonymous API limit (60 requests/hour per IP) being hit while the store refreshes *every* plugin in the registry — not a problem with this plugin. Fix it once by giving CPA a GitHub token (any classic token, no scopes needed):
>
> ```yaml
> plugins:
>   store-auth:
>     - match: "https://api.github.com/"
>       apply-to: ["registry", "artifact"]
>       type: bearer
>       token-env: "CLIPROXY_PLUGIN_STORE_TOKEN"
> ```
> and export `CLIPROXY_PLUGIN_STORE_TOKEN=ghp_...` in CPA's environment (e.g. `Environment=` in the systemd unit, or `environment:` in docker-compose), then restart CPA.

**Option B — manual.** Download `cpa-quota-warmup-v<ver>-linux-<arch>.so` from [Releases](https://github.com/szxypi/cpa-quota-warmup/releases), rename it to `cpa-quota-warmup-v<ver>.so` and place it under `<plugins.dir>/linux/<arch>/` (CPA derives the plugin id from the file name). Add `plugins.configs.cpa-quota-warmup: { enabled: true }` to `config.yaml` and restart CPA.

Binaries are built against glibc 2.34 (same baseline as the official plugins built on ubuntu-24.04); Debian 12 / Ubuntu 22.04+ / the official Docker image work out of the box. On older distros (glibc < 2.34) build from source: `CGO_ENABLED=1 scripts/build.sh` (Go 1.26+).

- `ops/deploy` and `ops/merge-config.py` are convenience scripts written for the maintainer's own local systemd deployment (they assume a `cli-proxy-api.service` and `/var/lib/cli-proxy-api/...` paths); treat them as examples and adjust the paths for your own setup, or configure/install by hand instead.
- `scripts/package-release.sh` produces the store-compatible zips + `checksums.txt` for both linux/amd64 and linux/arm64 (needs `gcc-aarch64-linux-gnu` for the arm64 cross build).

Two-step quick start (v0.4.0+):

1. Add this to `config.yaml` and restart CPA once:
   ```yaml
   plugins:
     configs:
       cpa-quota-warmup:
         enabled: true
   ```
2. The plugin generates `quota-warmup.yaml` next to `config.yaml` on its own, with one disabled section per credential file it finds. Open it, flip `enabled: false` to `enabled: true` for the accounts you want warmed up, and save -- no restart needed, the plugin picks it up within 30 seconds. Or do the same thing from the status panel (checkbox + Save button per row), described below -- or edit the whole file directly from the panel's own "编辑配置文件" (edit config file) text editor, no SSH/text editor needed.

Everything else -- timezone, base URL, api-key, message, rounds, ... -- is optional and auto-defaulted, or can be set under `config.yaml`'s `advanced:` block; see "高级设置（一般不用改）" below. Timezone defaults to the host process's own local timezone; no configuration needed.

Status/config panel (self-contained HTML, no external assets, follows the CPA Management Center's own theme and language): `GET /v0/resource/plugins/cpa-quota-warmup/panel`.

Configs that still set the pre-v0.4.0 top-level `time`/`model`/`accounts` (v0.3.0) or `default`/`providers`/`auths` (v0.1.0/v0.2.0) keys keep working exactly as before -- see "旧版配置格式（仍然支持）" below -- no `quota-warmup.yaml` is generated for those.

**Limitation, in one sentence**: the plugin cannot pin a request to a specific account (CPA gives plugins no such hook), so it can only fan requests out round-robin and reconcile coverage after the fact via usage records -- see "覆盖策略的局限" below for the full explanation (in Chinese; the rest of this document is Chinese-first).

---

CLIProxyAPI (CPA) 原生插件（Go c-shared 库）。每天在配置的时间点，给启用的每个账号发一条极短消息（默认 `"hi"`，`max_tokens: 16`），模型默认自动选该账号所属 provider 当前最便宜的一个，预热该账号的 5 小时额度窗口，避免第一次真实请求撞上"冷启动"配额检查。

插件本身**不需要，也没有**任何私有凭据或密钥：它就是本机 CPA 的一个普通客户端，用 `api-keys[0]`（或显式配置的 `api-key`）向自己的 `/v1/chat/completions` 发请求。

时区默认自动跟随宿主进程本身的本地时区（`time.Local`，systemd 部署下就是系统时区），不需要额外配置；也可以在 `advanced.timezone` 里手动指定覆盖。

## 快速开始

两步：

1. `config.yaml` 里只需要这 1 行，加上后重启一次 CPA：

   ```yaml
   plugins:
     configs:
       cpa-quota-warmup:
         enabled: true
         # config-file: "quota-warmup.yaml"   # 可选，默认 <cwd>/quota-warmup.yaml（与 config.yaml 同目录）
   ```

2. 插件会在 `config.yaml` 同目录下**自动生成并维护** `quota-warmup.yaml`，每个认证文件一段、默认全部 `enabled: false`。打开这个文件，把要预热的账号改成 `enabled: true`，保存——**不用重启**，插件 30 秒内的下一次 tick 就会读到；也可以不手动改文件，直接在状态面板的账号表里勾选「启用」、按需改时间/模型，点「保存」，效果一样（面板顶部会显示这个文件的完整路径）；**也可在面板『编辑配置文件』里直接改**——一个内嵌的文本编辑器，整份 `quota-warmup.yaml` 原文直接在浏览器里编辑保存，不用碰服务器/SSH。

## `quota-warmup.yaml` 详解

首次生成的样子（下面这份注释、缩进、`# provider: xxx` 行尾注释都是插件自动写的）：

```yaml
# cpa-quota-warmup 预热配置：每个认证文件一段，改 enabled / time / model 即可，保存后自动生效（无需重启）
# time：写 "05:30"，多个写 "05:30, 10:30"，或 cron "30 5,10,15,20 * * *"
# model：auto = 自动选该 provider 最便宜的；也可写具体模型名（见 GET /v1/models）
defaults:
  time: "05:30"
  model: auto
accounts:
  antigravity-alice@example.com.json:      # provider: antigravity
    enabled: false
    time: "05:30"
    model: auto
  codex-xxxx-alice@example.com-team.json:  # provider: codex
    enabled: false
    time: "05:30"
    model: auto
```

- `defaults:` 是没有单独设置的账号段所继承的时间/模型；改这两行会影响所有仍留空/等于默认值的账号。
- 每个账号段的字段都可以省略：不写 `time`/`model` 就用 `defaults:` 的值；不写 `enabled` 视为 `false`（新账号默认不预热，要手动打开）。也支持按账号覆盖 `message`、`max_tokens`（可选，不写就用 `config.yaml` 里 `advanced:` 的值）。
- `time` 的写法与顶层一致：`"05:30"`、逗号分隔的 `"05:30, 10:30"`，或标准 5 段 cron `"30 5,10,15,20 * * *"`。
- `model` 默认 `auto`：按内置的"从便宜到贵"候选表，从这台 CPA 实例 `GET /v1/models` 实际列出的模型里挑第一个；`GET /v1/models` 请求不到时退回候选表第一个（见下表）；也可以直接写死一个具体模型名。`codex` 会自动带上 `reasoning_effort: "low"`。
- **这份文件插件会持续维护，但绝不删除你写的内容**：`host.auth.list` 里新出现的认证文件，插件会追加一段（默认禁用）；消失的认证文件，插件会在那一行行尾追加 `# 认证文件不存在` 提示（账号段本身原样保留，你的设置不会丢，账号回来后这条提示会自动去掉）；你自己写的注释、值、格式都不会被覆盖或重排——插件是在 YAML 的语法树（`yaml.v3` 的 Node）级别做增量编辑，不是整份重新生成。只有真的有变化（新增/标注/取消标注）才会重写文件（原子写：临时文件 + `rename`）。
- **热加载**：每次 tick（30 秒一次）都会检查这个文件的 mtime，变了就重新解析；**解析失败**（比如手滑改出语法错误）不会影响调度——继续沿用上一次解析成功的配置，同时打一行 `host.log`（四语言）并在 `status`/面板上显示这个错误，绝不会因为一个 YAML 语法错误就整个停摆。

### 自动选择模型（`model: auto` 时）候选表

| provider | 候选表（从便宜到贵） |
| --- | --- |
| `codex` | gpt-5.3-codex-spark, gpt-5.6-luna, gpt-5.5, gpt-5.6-terra, gpt-5.6-sol, gpt-6-astra |
| `antigravity` | gemini-3.1-flash-lite, gemini-3-flash, gemini-3.6-flash-high, gemini-3.7-flash-high, gemini-3.8-flash-high, claude-sonnet-4-6 |
| `kimi` | kimi-k2, kimi-k2.5, kimi-k2.6, kimi-k2.8, kimi-k2.7-code, kimi-k2.8-code, kimi-k3, kimi-k3-256k |
| `xai` | grok-3-mini, grok-3-mini-fast, grok-4.3, grok-4.5, grok-4.6, grok-build-0.1 |
| `claude` | claude-3-5-haiku-20241022, claude-haiku-4-5-20251001, claude-sonnet-4-6 |
| `gemini-cli` / `aistudio` / `vertex` | gemini-2.5-flash-lite, gemini-2.5-flash, gemini-3.1-flash-lite-preview, gemini-3-flash-preview, gemini-3.5-flash-lite, gemini-3.5-flash |

只要 `GET /v1/models` 能拿到列表、且这个账号最终解析出的模型（无论来自账号段还是 `defaults:`）不在这个列表里，都会按现有的"预检"逻辑跳过并 `warn`，不会对着一个不存在的模型硬发。

## 高级设置（一般不用改）

以下全部是可选项，缺省即可正常工作，写在 **`config.yaml`**（不是 `quota-warmup.yaml`）的 `advanced:` 一个块里，新旧两种配置模式下都从这里读：

```yaml
plugins:
  configs:
    cpa-quota-warmup:
      enabled: true
      advanced:
        timezone: ""              # 留空 = 自动跟随宿主进程本地时区；也可以填 IANA 时区名手动覆盖
        base-url: "http://127.0.0.1:8317"
        api-key: ""                # 留空则从 cwd 的 config.yaml 读 api-keys[0]
        message: "hi"
        max-tokens: 16
        max-rounds: 3
        catch-up-minutes: 60
        language: "auto"           # auto/zh-CN/zh-TW/en/ru
        log: true
```

`priority`（插件加载优先级）是 CPA 通用的顶层键，不属于这个插件自己的配置，仍然写在最外层（`plugins.configs.cpa-quota-warmup.priority`），不放进 `advanced:`。

## 旧版配置格式（仍然支持）

`config.yaml` 里只要还留着下面任意一类顶层键，插件就继续按"内联模式"工作——**不会**生成/接管 `quota-warmup.yaml`，`status`/面板里 `mode` 字段会显示 `inline`（新默认的独立文件模式是 `file`）：

- **v0.1.0/v0.2.0**：`default:`/`providers:`/`auths:`/`timezone:`/`base-url:`/`api-key:`/`message:`/`max-tokens:`/`max-rounds:`/`catch-up-minutes:` 这些顶层键，行为与之前完全一致。
- **v0.3.0**：顶层 `time:`/`model:`/`accounts:`，行为与 v0.3.0 完全一致，包括 `accounts:` 列表项可以写成 `{match, time, model}` 对象覆盖单个账号、面板上按 `panel > account > global > provider > auto` 优先级手动设置模型（仍然存在 `overrides.json` 里，不受 v0.4.0 影响）。

两类都不需要迁移，检测到时 `plugin.register` 会打一行 `host.log` 提示可以删掉这些顶层键、只保留 `enabled: true` 来迁移到独立文件模式（仅提示，不影响运行）。v0.1.0 及更早的旧格式，面板不支持保存设置（`/set` 返回 501）；v0.3.0 内联模式的面板保存行为不变。

`config.yaml` 与 v0.3.0-inline 的 `quota-warmup.yaml`（v0.4.0 独立文件）两种意图等价时解析结果一致，`config_new_format_test.go` 的 `TestDecodeConfigOldAndNewFormatsAreEquivalent` 覆盖了 v0.1/v0.2 与 v0.3 之间的等价性。

<details>
<summary>v0.3.0 内联写法详解（点击展开）</summary>

```yaml
plugins:
  configs:
    cpa-quota-warmup:
      enabled: true
      time: "05:30"                    # 每天几点预热；也支持 cron
      model: "auto"                    # auto/具体模型名/{provider: model} 映射
      accounts:
        - "codex-*-team.json"                 # 用顶层 time/model
        - match: "antigravity-alice.json"     # 单独覆盖
          time: "05:30, 10:35"
          model: "gemini-3.7-flash-high"
      advanced:
        models:                    # 按 provider 覆盖模型，等价于顶层 model 写成映射
          codex: "gpt-5.6-luna"
          kimi: "kimi-k2.8"
```

`accounts:` 支持纯字符串（glob，`"*"` 表示全部账号）或 `{match, time, model}` 对象；`match` 必填，`time`/`model` 不写就沿用顶层的值，同一账号被多条命中时列表里靠后的覆盖靠前的。**不写或写成空列表 = 不预热任何账号**，会在 `status`/面板和日志里明确提示。

</details>

<details>
<summary>v0.1.0/v0.2.0 字段详解（点击展开）</summary>

```yaml
plugins:
  configs:
    cpa-quota-warmup:
      enabled: true
      priority: 1
      log: true
      timezone: "Asia/Shanghai"
      base-url: "http://127.0.0.1:8317"
      api-key: ""              # 留空时从 cwd 的 config.yaml 读 api-keys[0]
      message: "hi"
      max-tokens: 16
      max-rounds: 3
      catch-up-minutes: 60
      default:
        enabled: true
        times: ["05:30"]
      providers:
        antigravity: { model: "gemini-3.7-flash-high" }
        codex:       { model: "gpt-5.6-luna", reasoning-effort: "low" }
        kimi:        { model: "kimi-k2.8" }
        xai:         { model: "grok-4.6" }
        claude:      { model: "claude-haiku-4-5-20251001" }
        gemini-cli:  { model: "gemini-2.5-flash-lite" }
        aistudio:    { model: "gemini-2.5-flash-lite" }
        vertex:      { model: "gemini-2.5-flash-lite" }
      auths:
        - match: "codex-*-prolite.json"
          enabled: false
        - match: "antigravity-alice@example.com.json"
          times: ["05:30", "10:35"]
          model: "gemini-3.7-flash-high"
```

- `default:` 是应用到每个账号的基线；`providers.<provider>.model/reasoning-effort` 按账号的 provider 给出默认模型；`auths:` 按账号文件名（`host.auth.list` 的 `name` 字段）用 glob（`*`、`?`、`[]`）匹配，**列表里靠后的条目覆盖靠前的**（同一账号被多条命中时）。
- 账号的 provider 不在 `providers:` 里、且该账号自己也没有 `auths[].model` 覆盖 → 跳过并打一条 `warn` 日志。
- **`providers:` 里的模型名必须是这台 CPA 实例 `GET /v1/models` 实际列出的那个**，不是任意合法模型名就行——线上 2026-09-13 实测 antigravity 只暴露 `gemini-3.7-flash-high`/`gemini-3.8-flash-high`（`gemini-3.1-flash-lite` 直接 404 `model_not_found`），kimi 只有 `kimi-k2.8`/`kimi-k2.8-code`/`kimi-k3-256k`，xai 只有 `grok-4.6`。插件自己也会在每次 tick/`run` 前做同样的预检（见"机制"一节），配错了不会 404，只会跳过并 warn。
- `catch-up-minutes`：服务重启或该时间点被跳过后，仍在这个窗口内可以补发；超过就当天跳过，等下一次配置的时间点。
- `api-key` 留空时，插件从**当前工作目录**（CPA 的 cwd，即 `/var/lib/cli-proxy-api`）下的 `config.yaml` 里只读 `api-keys` 这一个键的第一个值，不解析其余内容。

</details>

## 机制

1. 后台每 30 秒 tick 一次。每次 tick 都重新调用 `host.auth.list` 拿最新认证文件列表（增删账号即时生效）。**文件模式**（v0.4.0 默认）下：先确保 `quota-warmup.yaml` 存在并已经和当前认证文件列表对齐（新增/标注消失，见上），再看每个账号自己是否 `enabled: true`；**v0.3.0 内联模式**下：看是否被顶层 `accounts[]` 里某条 glob 命中；**v0.1.0/v0.2.0 内联模式**下：走 `default:` → `providers.<provider>:` → `auths[]` 顺序覆盖。三种模式的判定逻辑相互独立，一份配置只会命中其中一种（判定顺序：先查旧版顶层键，再查 v0.3.0 顶层键，都没有就是文件模式）。
2. 对每个账号的每个时间表达式（HH:MM 或 cron），计算"上一个应该触发的时刻"：若 `now` 已经过了这个时刻、且 `now - 该时刻 <= catch-up-minutes`、且状态文件里还没有这个 `(账号, 日期, HH:MM)` 的记录 → 判定为"到期"（cron 的匹配用标准 5 段字段 `分 时 日 月 周`，`*`/`*/n`/`a,b`/`a-b`/`a-b/n` 五种写法都支持，五个字段之间用简单 AND 逻辑，不做 vixie-cron 里"日期与星期都限定时取 OR"那个特例）。
3. **模型解析**：若这个账号最终解析出的是"自动"档位（文件模式：账号段和 `defaults:` 都没写具体模型，或写的是 `auto`），用同一 api-key 对本机 `GET /v1/models` 取一次 `data[].id` 集合，从该 provider 的内置候选表里选第一个出现在集合里的；`GET /v1/models` 本身请求失败就直接用候选表第一个。若是显式指定的模型，同样对着这次 `GET /v1/models` 结果核对，不在里面就跳过（不发请求）并记 `warn`；这次 `GET /v1/models` 本身失败（网络问题等）就跳过预检、照常发送，不能因为预检失败反而拦住本该发出去的请求。
4. 到期且通过预检的账号按 provider 分组。每组内，"顺序"（不是并发）发送 N 条请求（N = 该组待预热账号数），每条请求带一个全新随机的 `X-Session-ID` 头；在最多 3 秒的窗口内持续收集这一轮所有请求标记下的**全部** `usage.handle` 记录（不是只看第一条），直到组内每个目标账号的 `AuthID` 都出现过、或者窗口到期。**同一个 `X-Session-ID` 标记下可能对应不止一条 usage 记录**：宿主可能在同一个客户端请求内部先打到 A 账号 429，再重试打到 B 账号成功，两条记录共享同一个 `SessionID`——只看"这个标记下最新一条"会把 A 账号的 429 直接吞掉。失败（429/其他）的记录照样算"覆盖"，账号的额度检查窗口已经被真实触碰过。仍未覆盖的账号进入下一轮，最多 `max-rounds`（默认 3）轮；轮次用尽仍未覆盖的账号记一条 `warn` 日志，但**这一天这个时间点视为已处理**，不会在同一天的 catch-up 窗口内反复重试。
5. 结果（是否覆盖、用了几轮、状态码、警告信息）持久化到 `<CPA 工作目录>/quota-warmup/state.json`（原子写：临时文件 + `rename`，这是插件自己的文件，不是 `config.yaml`，可以放心用 rename），只保留最近 7 天。v0.3.0 内联模式下面板手动设置的模型覆盖存在同目录下的 `overrides.json`（原子写，`reconfigure`/重启后依然有效）；文件模式下面板的保存直接写回 `quota-warmup.yaml`，不再用 `overrides.json`（若从 v0.3.0 升级时该文件还在，首次启动会把其中的模型覆盖并入新文件后把它改名为 `overrides.json.migrated`，见"面板与模型选择"）。

## 为什么不用 `host.model.execute`

`host.model.execute` 回调执行时 `InternalSource=true`，宿主对 `InternalSource=true` 的请求**不生成 usage 记录**（`sdk/api/handlers/handlers_execution.go:205`，已对照 v7.2.158 源码核实）。没有 usage 记录就无法知道这次预热请求到底落在了哪个账号上——而"确认预热覆盖到了目标账号"正是本插件存在的意义。所以插件改为像任何普通客户端一样，用 `net/http` 向本机 CPA 发一个真实的 `POST /v1/chat/completions`，走正常的 usage 上报链路。

## 覆盖策略的局限

**插件无法从 API 层面指定"这次请求必须用哪个账号"**（`pinned_auth_id` 只能通过宿主内部 ctx 注入，插件够不到）。所以覆盖账号的思路是"打散 + 事后核对"：

- 每条请求带一个全新的 `X-Session-ID`，让宿主的会话粘滞（session-affinity）机制把它当成一个全新会话，从而回落到宿主的 round-robin 选择器（对 antigravity 上的 `gemini-*`，本机还装了 `cpa-affinity-router` 插件接管选号，其 tie-break 策略是选最久未被选中的账号，顺序发多条一样能轮到每个账号）。
- 但"回落到 round-robin"不等于"保证轮询到每一个账号"——如果某个账号连续被选中好几次，同一组里的其他账号就要等到下一轮才可能被覆盖。`max-rounds` 就是为这种情况兜底的重试预算，用尽仍未覆盖的账号只能放弃并记警告，**不保证 100% 覆盖**，只是尽力而为。
- 覆盖核对优先按 `UsageRecord.SessionID` 精确匹配我们发的会话标记（见下面"核实结果"里 `header:` 前缀的坑）；如果宿主某个版本不再保留这个值，会退化成"发起时间窗口 ± 2s + 模型名"的宽松匹配。

## 面板与模型选择

状态页面（`GET .../panel`）顶部会显示当前模式（`file`/`inline`）与 `quota-warmup.yaml` 的完整路径（文件模式下）。账号表每一行都能直接编辑：

- **文件模式**（v0.4.0 默认）：每行有「启用」勾选框、可编辑的「时间」输入框、模型格子（`<input list=...>` + 下拉数据源，来自 `status` JSON 的 `available_models`，按 `owned_by` 分组，也能直接手打）+「自动」按钮，还有一个「保存」按钮一次性提交这一行的启用/时间/模型三项。点「保存」调 `GET .../set?auth=<name>&enabled=<bool>&time=<...>&model=<id|auto>`（只传的字段会被写回 `quota-warmup.yaml` 对应账号段，Node 级编辑，注释不丢），点模型旁边的「自动」只会单独把 `model` 设成 `auto`、不动其他字段。
- **v0.3.0 内联模式**（顶层写了 `time`/`model`/`accounts` 时）：模型列同 v0.3.0——面板/账号/顶层/`advanced.models` 四层优先级，`model_source` 标出来源，点「保存」调 `GET .../set?scope=global|auth&auth=<name>&model=<id|auto>`，存在 `overrides.json` 里，`model=auto` 是**删除**这条覆盖，不是存字面量。
- **v0.1.0/v0.2.0 内联模式**：`/set` 直接返回 501，面板上没有可保存的编辑控件（这个格式本来就没有"面板覆盖"这一层）。
- 模型校验规则两种模式一致：`model` 不是 `auto` 时必须出现在这次 `GET /v1/models` 的结果里，否则 400 并返回四语言错误信息；预检本身请求失败（网络问题等）会放行保存，但返回一条 `warning`。
- **从 v0.3.0 升级**：如果你之前在 v0.3.0 内联模式下用过面板设置过模型（`overrides.json` 里有内容），现在把 `config.yaml` 简化成只剩 `enabled: true` 切到文件模式后，插件第一次启动会自动把 `overrides.json` 里的模型覆盖合并进新生成的 `quota-warmup.yaml`（按账号覆盖合并进对应账号段的 `model`；全局覆盖合并进 `defaults.model`），然后把 `overrides.json` 改名成 `overrides.json.migrated`，只做这一次。**这一步只迁移模型**，不会把之前"哪些账号被启用"的状态带过来——迁移后请手动确认 `quota-warmup.yaml` 里对应账号的 `enabled` 字段。
- **v0.5.0：面板『编辑配置文件』在线编辑整份 `quota-warmup.yaml`**（仅文件模式）。账号表下方新增一个 `<textarea>`（等宽字体、Tab 键插入两个空格），「重新载入」拉取最新原文，「保存」提交修改：
  - 读：`GET .../config-yaml` 返回 `{path, content, mtime, error}`（`content` 是文件原文，`error` 是当前的解析错误，不阻止显示）。
  - 写：`GET .../config-yaml/save?content=<base64url 编码，无 padding>&mtime=<读到的 mtime>`。`content` 用 base64url 而不是直接拼进 query string，是为了绕开 GET 请求里换行/引号/中文这些字符的转义问题——面板 JS 用 `btoa(unescape(encodeURIComponent(text)))` 转标准 base64 再做 `+`→`-`、`/`→`_`、去 `=` 三步替换，服务端用 `base64.RawURLEncoding` 精确对应解码；解码后统一把 `\r\n`/`\r` 归一化成 `\n` 再校验/落盘。
  - 校验顺序与保存顺序：**先校验**（`yaml.v3` 解析 + 反序列化进强类型结构，天然覆盖"顶层要是 `defaults`/`accounts` 结构""字段类型对不对"这些检查；额外单独校验每个 `time` 表达式必须能被解析，不能只是随便一个字符串），失败返回 `400 {error, line, column}` 且完全不碰磁盘；**再检查 `mtime`**，跟当前文件实际 mtime 不一致（比如另一个人或后台 tick 在这期间改过文件）返回 `409` 加提示"文件已被别处修改，请刷新"；都通过才原子写入（temp+rename）、立即重新解析生效（不用等下一次 tick），并把新 `mtime` 返回给前端。
  - 面板上：校验失败时在编辑器上方用红字显示「第 N 行第 M 列：错误信息」；保存成功刷新账号表。
  - 这几处新增的文案（区块标题、按钮、错误提示）**只做了中文**，没有走四语言（i18n 目录里这几个键四种语言填的是同一句中文，只是为了不破坏 key 集合一致性测试，不是真的翻译）。
  - `mtime` 不是裸的文件系统时间戳字符串：实测发现两次紧挨着的原子写（temp+rename）在这台开发机的文件系统上会落在**完全相同**的纳秒级 `ModTime()` 上（见"已知限制"），所以实际的 token 是 `ModTime + 进程内写入序号` 拼出来的，保证同一个 `warmupFileManager` 实例做的任意两次写永远产生不同的 token。
- **v0.6.2：账号行的模型下拉只显示该账号所属 provider 的模型**，不再是这台 CPA 实例暴露的全部模型。分两层：
  1. **兜底：按 provider 推断**。`GET /v1/models` 的 `owned_by` 是上游请求格式族，不是 CPA 的 provider id（实测：antigravity→`antigravity`，codex→`openai`，kimi→`moonshot`，xai→`xai`，claude→`anthropic`/`claude`，gemini-cli/aistudio/vertex→`google`/`gemini`；某些 openai-compatibility 账号挂的 glm/minimax/qwen 甚至标成 `anthropic`）。`candidates.go` 的 `modelsForProvider` 用一张映射表把 `owned_by` 归到 provider，再并上这个 provider 内置候选表里出现在 `available_models` 中的模型，去重后候选表的"从便宜到贵"顺序在前、其余按字母序；`status` JSON 每个账号新增 `models: [...]`（这一层结果），顶层 `available_models` 不变（inline 模式的全局模型框还是用全量）。手输的模型即使不在这个列表里也照常能保存（`/set` 校验只看是否在全量 `available_models` 里），只是行状态会多一行灰字「不属于该 provider 的模型」——不阻止，纯提示，因为 `owned_by` 本来就只是启发式，不是权威归属。
  2. **优先：直接问控制台要账号的真实可用模型**。CPA 管理控制台账号卡片上的「模型」按钮背后是 `GET /v0/management/auth-files/models?name=<认证文件名>`（宿主 `internal/api/handlers/management/auth_files.go`，读 `registry.GetModelsForClient(authID)`，比 `owned_by` 启发式准），但需要管理密钥鉴权，插件后端只能拿到密钥的 bcrypt 哈希，摸不到明文。因为本面板是以 `/plugin-pages/cpa-quota-warmup/0` 同源 iframe 嵌进控制台的，面板 JS 改为在**浏览器端**原样照抄控制台自己解密 `localStorage['cli-proxy-auth']` 的算法（`"enc::v1::" + base64(XOR(utf8(JSON), key))`，`key = utf8("cli-proxy-api-webui::secure-storage|" + location.host + "|" + navigator.userAgent)`，同源同 UA 才能复现），拿到明文管理密钥后直接以 `Authorization: Bearer` 调控制台自己的这个接口——全程只在浏览器里发生，密钥不落插件后端、不写日志、不进 DOM。账号行下拉展开时按需（不是页面加载就抓全部账号）请求，结果按账号名内存缓存 5 分钟；请求中显示灰字「加载中…」；拿不到（没有管理密钥、401、网络失败）时静默退回上面第 1 层的 provider 推断，并在下拉底部加一行灰字「已按 provider 推断（未取到账号模型）」。**只有把面板当作控制台内嵌页打开时这一层才生效**——如果单独拿面板 URL 在控制台之外打开（没有 `cli-proxy-auth` 这个 localStorage 键，或者跨域访问不到它），会直接退回纯 provider 推断，此时面板顶部「配置」区块的 chips 里会看到「账号模型来源：provider 推断」（能拿到控制台接口时显示「控制台接口」）。

## 部署

```bash
cd ~/cpa-plugins/cpa-quota-warmup
scripts/build.sh                 # -> dist/cpa-quota-warmup-v0.6.3.so (+ .sha256)
sudo ops/merge-config.py         # 原地合并默认配置到 /var/lib/cli-proxy-api/config.yaml（保 inode）
                                  # sudo ops/merge-config.py --remove 可移除
sudo ops/deploy                  # 安装 .so 到插件目录并重启 cli-proxy-api.service
```

`ops/merge-config.py` 写入的默认配置就是"快速开始"第一步那 1 行（`enabled: true`，外加一行注释掉的 `config-file` 示例）。修改前会先按插件 id 做正则查找替换已有块，重复执行是幂等的。

**`ops/deploy` 与 `ops/merge-config.py` 是针对维护者本机 systemd 部署（`cli-proxy-api.service`、`/var/lib/cli-proxy-api/...`）写的辅助脚本**，不是通用安装程序；换一套部署方式（Docker、不同路径、不同服务名等）时请照着改脚本里的路径/服务名，或者干脆手动完成"放 `.so`、改 `config.yaml`、重启 CPA"这三步。

## 验证方法

```bash
# 1. 单元测试 + go vet
go vet ./... && go test ./...

# 2. 真 ABI 集成测试（不需要真实宿主/网络）
python3 integration_abi_test.py dist/cpa-quota-warmup-v0.6.3.so

# 3. 部署后看宿主日志（host.log 回调，前缀 [cpa-quota-warmup]）
journalctl -u cli-proxy-api | rg 'cpa-quota-warmup'

# 4. 状态页面（HTML，免鉴权，管理中心菜单里也能进），跟随管理中心的语言与主题
open http://127.0.0.1:8317/v0/resource/plugins/cpa-quota-warmup/panel

# 5. 状态 JSON（页面本身用的数据源；?lang= 可强制语言，不传则按 Accept-Language/LANG 协商）
curl -s 'http://127.0.0.1:8317/v0/resource/plugins/cpa-quota-warmup/status?lang=ru' | jq .

# 6. 立即手动触发一轮（GET，见下方"宿主事实核实"里为什么不是 POST）
curl -s 'http://127.0.0.1:8317/v0/resource/plugins/cpa-quota-warmup/run?auth=antigravity-*' | jq .

# 7. 文件模式：直接编辑 quota-warmup.yaml 打开某个账号，或者用面板/命令行等效调用（GET）
curl -s 'http://127.0.0.1:8317/v0/resource/plugins/cpa-quota-warmup/set?auth=antigravity-alice.json&enabled=true&model=gemini-3.7-flash-high' | jq .

# 8. cpa-usage-panel 里应该能看到这些请求（provider=对应 provider，model=配置的模型，
#    时间落在计划的 HH:MM 附近），确认预热请求确实打到了上游而不是本地短路

# 9. v0.5.0：面板在线编辑 quota-warmup.yaml（读 -> base64url 编码修改后的内容 -> 用读到的 mtime 保存）
MTIME=$(curl -s 'http://127.0.0.1:8317/v0/resource/plugins/cpa-quota-warmup/config-yaml' | jq -r .mtime)
CONTENT=$(printf 'defaults:\n  time: "05:30"\n  model: auto\naccounts: {}\n' | base64 -w0 | tr '+/' '-_' | tr -d '=')
curl -s "http://127.0.0.1:8317/v0/resource/plugins/cpa-quota-warmup/config-yaml/save?content=${CONTENT}&mtime=${MTIME}" | jq .
```

## 宿主事实核实结果（与任务原始假设的出入）

对照 CLIProxyAPI v7.2.158 源码（`$(go env GOMODCACHE)/github.com/router-for-me/!c!l!i!proxy!a!p!i/v7@v7.2.158`）核实，以下几点与最初给出的方案假设**不一致**，已按下述结论实现：

1. **`UsageRecord.SessionID` 不等于我们发的 `X-Session-ID` 原始值，而是带 `"header:"` 前缀。** `sdk/cliproxy/session/info.go` 的 `ExtractSessionInfo`（"5. OpenCode / Pi Slot / Task / Generic Headers"分支）执行 `info.SessionID = "header:" + sid`；这条路径经 `internal/pluginhost/adapters_usage_translation.go` 的 `usageAdapter.HandleUsage` 原样传给插件的 `UsageRecord.SessionID`。插件里所有按会话标记做覆盖核对的地方都已按 `"header:" + tag` 匹配（见 `usage.go` 的 `sessionHeaderRecordPrefix`），不是原方案假设的原始值直接相等。
2. **CPA 资源路由（`/v0/resource/plugins/<id>/...`）只接受 GET，POST 在宿主层就被拒绝。** `internal/pluginhost/management.go` 的 `ServeResourceHTTP` 开头即 `if !strings.EqualFold(r.Method, http.MethodGet) { return false }`——POST 请求根本不会构造 `ManagementRequest` 转发给插件，直接在 gin 路由层 404。要注册真正的 POST 路由，只能用 `Routes: []ManagementRoute{...}`（`/v0/management/` 前缀），但那一族路由**是要管理鉴权的**，与任务里"两个路由都免鉴权"的要求冲突。按最小合理假设，`run` 触发端点改成了 **GET**（`?auth=<glob>` 走 query string），继续挂在免鉴权的 resource 路由下；README 与代码注释（`management.go`）都记录了这个取舍。
3. **`host.model.execute` 确实不产生 usage 记录**，与任务给出的判断一致（`sdk/api/handlers/handlers_execution.go:205`，`InternalSource=true` 时跳过 usage 上报），已按此确认改用普通 HTTP 客户端。
4. **`POST /v1/chat/completions` 的路径、`max_tokens`、`reasoning_effort` 字段均按任务描述核实无误。** `internal/api/server_routes.go:66` 注册路由；`sdk/api/handlers/openai/openai_handlers.go:211-212` 显式读取并转发 `max_tokens`；`reasoning_effort` 由通用的 `internal/thinking/apply.go` 的 `extractOpenAIConfig` 解析（"OpenAI Chat Completions format" 的合法输入，`none/low/medium/high` 离散档位），不是本插件凭空加的字段，会被正常应用到匹配的模型/provider 上。实际是否被下游 codex 执行器采纳成 upstream 请求未做端到端验证（没有真实 codex 账号可测）。
5. **模型名的 provider 前缀语法（`provider/model` 或 `provider:model`）在 `/v1/chat/completions` 这条路径上不存在。** `ForcedProvider`（`sdk/api/handlers/model_execution.go`）只能通过插件内部的 `ProtocolExecutionRequest`/Gemini Interactions 的 `agent` 参数设置，普通客户端请求体的 `model` 字段没有任何解析出 provider 前缀的逻辑（`sdk/api/handlers/handlers_routing.go` 的 `providersForExecution` 只在 `execOptions.ForcedProvider` 已经非空时才用它，而这个值不会从请求体里派生）。因此本插件严格依赖"每个 provider 配置一个在该 provider 唯一存在的模型名"这一假设，与任务描述的兜底方案一致，未发现更好的替代方案。
6. **`pluginapi.ManagementRequest` 确实带 `Query`（`url.Values`）与 `Headers`（`http.Header`）字段**，且 `internal/pluginhost/management.go` 的 `ServeResourceHTTP` 在构造它时会把真实请求的 query string 和请求头原样克隆进去（`cloneHeader(r.Header)` / `cloneValues(r.URL.Query())`）。所以 `status`/`run`/`panel` 三个路由都能直接读到 `?lang=` 和 `Accept-Language`，不需要"只能退化到路径/查询串"那种更弱的方案。
7. **`cli-proxy-language` 的实际存储格式**：对照一份真实的 `/var/lib/cli-proxy-api/static/management.html` 构建反查得到的函数（对应变量名 `ll`/`ul`/`dl`/`cl`/`sl`/`ol`），确认它是 zustand `persist` 中间件写的，取值可能是 `{"state":{"language":"zh-CN"},...}` 这种信封、也可能是裸 JSON 字符串 `"zh-CN"`、极端情况下甚至是完全不带引号的裸字符串——三种都要兼容解析（本插件页面里的 `parseStored()` 照此实现）；`navigator.language` 兜底规则是 `zh-tw`/`zh-hk`/`zh-mo`/`zh-hant` 前缀 → `zh-TW`，`zh*` → `zh-CN`，`ru*` → `ru`，其余 → `en`，与任务描述完全一致，已按真实源码核实（不是假设）。

## 国际化（i18n）

插件跟随 CPA 管理中心（Management Center）的语言，覆盖 host.log 全部文本、`status`/`run` JSON 里的人类可读字段（`Skipped` 原因、`Warning`）、以及状态页面本身，支持 `zh-CN`（简体）、`zh-TW`（繁体）、`en`、`ru` 四种，与管理中心 i18next 支持的语言完全一致（`Vc="cli-proxy-language"`、`Hc=["zh-CN","zh-TW","en","ru"]`，对照真实 `management.html` 构建核实）。

- **消息目录**：`i18n.go` 里 `messagesEN/messagesZhCN/messagesZhTW/messagesRU` 四张表，key 用 `msgKey` 常量化；`tr(lang, key, args...)` 查表格式化，缺失语言/缺失 key 都回退英文，实在没有则回退裸 key 字符串（不会 panic）。四张表的 key 集合一致性有单测保证（`TestMessageCatalogsHaveTheSameKeys`）。ru 是认真写的技术俄语，不是机翻占位（含一处刻意简化：轮次计数的复数变格统一用 "раундов"，不做俄语 one/few/many 全套规则）。
- **服务端语言选择**：配置新增 `language: "auto" | "zh-CN" | "zh-TW" | "en" | "ru"`（默认 `auto`）。
  - `status`/`run`（有请求上下文）优先级：`?lang=` 查询参数 > `language` 配置（非 `auto` 时）> `Accept-Language` 头（取权重最高的一个 tag）> 环境变量 `LC_ALL`/`LANG`（`zh_CN.UTF-8` → `zh-CN`，`C`/`POSIX`/裸 `C.UTF-8` 视为未设置）> 兜底 `zh-CN`。**`?lang=` 优先级高于 `language` 配置是本插件自己的取舍**：任务原文只给出了 "auto 规则" 的链条，没说清楚 pin 死的 `language` 配置与单次请求的 `?lang=` 谁优先；这里选择让 `?lang=` 总是赢，因为状态页面自己每次都会带着访客浏览器实际检测到的语言发 `?lang=`，如果配置更优先，管理员一旦全局 pin 了语言，页面就没法再跟着访客自己的语言走了。
  - `host.log`（没有请求上下文）：`language` 配置（非 `auto` 时）> `LC_ALL`/`LANG` > 兜底 `zh-CN`。
  - `status`/`run` 的 JSON 响应都带 `"lang"` 字段说明实际用的是哪种。
- **状态页面**：`GET /v0/resource/plugins/cpa-quota-warmup/panel`（管理中心菜单会显示"配额预热 / Quota Warmup"），自包含 HTML（内联 CSS/JS，无外链），登记簿/表格风格（配置摘要用两列表格，账号列表与最近记录都是纯表格，没有卡片套卡片、没有渐变、没有 emoji），字体用系统字体栈，深浅色主题都跟随管理中心的 `cli-proxy-theme`（做法照抄 `cpa-usage-panel/panel.html`）。页面 JS 读 `localStorage['cli-proxy-language']`（同样支持 zustand-persist 的 JSON 信封或裸字符串两种存法），缺失时按 `navigator.language`（`zh-tw`/`zh-hk`/`zh-mo`/`zh-hant` 前缀 → `zh-TW`，`zh*` → `zh-CN`，`ru*` → `ru`，其余 → `en`）——这套判定逻辑对照真实 `management.html` 构建里的 `ol/sl/cl/ll/ul/dl` 几个函数核实过，不是拍脑袋写的。页面自己的文案（标题、表头、按钮等）复用 Go 端同一份消息目录，整份序列化成 JSON 注入页面（`window` 里一个 `<script type="application/json">`），不维护第二份翻译。
- `?auth=<glob>` 的"立即预热"按钮直接调 `/run`，结果就地渲染在按钮下方，不跳转页面。

## 已知限制

- 覆盖不保证 100%：round-robin 打散是"尽力而为"，`max-rounds` 用尽后放弃并只记警告，不会无限重试。
- `run` 只能是 GET（见上）。
- 手动触发（`run`）不写入按时间点的 `state.json`（不影响当天定时槽位的判定），只受每账号 60 秒节流保护，避免连点。
- 模型预检只确认 `GET /v1/models` 列出了这个名字，不代表这个模型这个账号一定能用（例如账号自身权限/额度问题仍可能在实际发送时报错）；预检本身失败（网络问题等）会被当作"跳过预检，照常发送"处理，不会阻塞正常预热。
- 状态页面首次渲染用服务端猜的语言（`?lang=`/`Accept-Language`/`LANG` 那条链，没有访问 localStorage 的能力），JS 加载后立即按 `cli-proxy-language`/`navigator.language` 校正；两者通常一致（浏览器语言与 `Accept-Language` 头本来就同源），但理论上 JS 执行前有极短暂的窗口可能显示了另一种语言的静态文案。
- `language` 配置项写了非法值（既不是 `auto` 也不是四种语言之一）会静默回退成 `auto`，不会让 `plugin.register`/`reconfigure` 失败；没有额外的 `warn` 日志（这一点与其他配置校验不完全一致，属于本次改动里对"不要因为一个拼写错误就整个不生效"的取舍）。
- **cron 的日期/星期字段用简单 AND 逻辑**，不是 vixie-cron 那种"日期与星期都限定时取 OR"的特例；这个插件的场景就是"每天/每隔几小时的固定时间点"，不需要那种特例，属于本次改动的刻意简化，已在 `cron_test.go` 里覆盖 `*`、`*/n`、`a,b`、`a-b`、星期几这几种写法。
- **顶层 `model`/`advanced.models` 的可用性校验只看这个模型是否出现在 `GET /v1/models` 里，不再进一步核实它是否"属于"某个账号的 provider**（`/v1/models` 的 `owned_by` 字段目前只用于面板下拉框的分组展示，插件没有其他可靠的"模型 ↔ provider 归属"数据源）；配错了不会立刻报错，只会在实际发送时可能因为账号/provider 不匹配而失败，这是本次改动里对一个不够明确的校验要求做的最小合理简化。
- **面板"模型来源"里 `panel`（面板覆盖）这一档，`scope=global` 和 `scope=auth` 两种面板覆盖用的是同一个优先级**（都在"账号对象 `model`"之上），这是协调方给出的优先级列表（`panel > account > global > provider > auto`）里没有进一步区分两种 panel 覆盖相对顺序时，本插件自己采用的最直接读法。
- `overrides.json`（v0.3.0 内联模式面板模型覆盖）和 `state.json`/`quota-warmup.yaml`（默认路径时）一样按进程当前工作目录解析路径，不随 `config.yaml` 走；这几份文件都不会被 CPA 自身的热重载 watcher 监视。
- **`quota-warmup.yaml` 的生成/增量维护只在每次 tick（30 秒一次）或 `status`/`set` 请求时触发**，不是文件系统事件驱动（没有 fsnotify）；新增账号最坏要等到下一次 tick 才会出现在文件里，但打开面板/请求 `status` 会立即触发一次，所以实际感知通常是"秒级"而不是"最多 30 秒"。
- **Node 级 YAML 编辑对"空 mapping 会被序列化成 flow style（`accounts: {}`）"这个 `yaml.v3` 行为做了专门规避**（否则再往里追加带行尾注释的账号段会生成语法错误的 YAML）；这是实现过程中用真实单元测试挖出的一个真 bug，已通过强制恢复 block style 修复，回归测试见 `warmupfile_test.go` 的 `TestWarmupFileManagerSetAccountCreatesMissingSection`。
- overrides.json 迁移到 `quota-warmup.yaml` 只发生一次（迁移后原文件被重命名），且只搬运模型覆盖，不搬运"账号是否启用"的状态，见"面板与模型选择"一节。
- 三种配置模式（文件 / v0.3.0 内联 / v0.1-v0.2 内联）互斥且按固定优先级探测（先查旧版顶层键，再查 v0.3.0 顶层键，都没有才是文件模式）；同一份 `config.yaml` 不支持混着写（比如顶层既有 `time:` 又想用文件模式）。
- **`GET .../config-yaml`/`GET .../config-yaml/save` 和其他 resource 路由一样不做鉴权**（这是这类路由在这台宿主上的既有约束，见"机制"/"宿主事实核实结果"）——也就是说任何能访问到 CPA 这个端口的人都能读到/改写 `quota-warmup.yaml` 全文。**如果 CPA 暴露在公网或不受信任网络，请在反向代理层限制 `/v0/resource` 的访问**（比如只放行管理网段、加一层 Basic Auth/IP allowlist），插件自身不提供这层保护。
- **`GET .../config-yaml/save` 的 `mtime` 冲突检测不是纯文件系统时间戳**：实测发现在这台开发机的文件系统上，两次紧挨着（无人为延迟）的原子写（temp+rename）会落在完全相同的纳秒级 `ModTime()` 上——如果直接拿 `ModTime()` 当 token，这种情况会被误判成"文件没变"，从而漏掉一次真实的并发冲突。已修复为 `ModTime + 本进程内的写入序号` 拼接成的 token（`mtimeToken`，见 `warmupfile.go`），保证同一个 `warmupFileManager` 实例做的任意两次写永远返回不同 token；但这只覆盖"这个进程自己做的写"之间的冲突检测，不同进程/外部编辑器在恰好同一纳秒各自写一次这种极端情况仍无法用纯 mtime 方案分辨（概率极低，且本来就不是这个机制设计要覆盖的场景）。回归测试见 `warmupfile_test.go` 的 `TestMtimeTokenDistinguishesWritesWithIdenticalModTime`/`TestOverwriteRawAlwaysProducesADistinctToken`。
- `GET .../config-yaml/save` 的内容长度上限 256 KiB（按解码后的字节数算，不是 base64 编码后的 query string 长度），超过直接 400，不写盘。

## v0.1.1（线上手动触发实测后的修复）

1. **同一会话标签的多条 `usage.handle` 记录不再被遮蔽。** 线上实测：一条 codex 预热请求先在其中一个 team 账号上得到 429 `usage_limit_reached`，宿主随即在同一请求内重试到另一个 team 账号成功——两条 `usage.handle` 记录共享同一个 `SessionID`。旧实现 `bySessionTag` 只返回最新一条、`waitForSessionCoverage` 找到一条就把该标记标记为"已处理"，于是 429 那条被吞掉，对应账号被误报"not covered"。现在 `usageRing.allBySessionTag` 返回该标记下的**全部**记录，`waitForSessionCoverage` 在整个窗口内持续收集、直到目标账号集合全部命中或超时才返回；失败记录一样算覆盖。见 `usage_test.go`/`runner_test.go` 里复现该场景的单测。
2. **默认模型名改成本机 `GET /v1/models` 实际暴露的那些**（antigravity `gemini-3.7-flash-high`、kimi `kimi-k2.8`、xai `grok-4.6`；codex/claude/gemini-cli/aistudio/vertex 不变），避免像 `gemini-3.1-flash-lite` 那样直接 404。
3. **加了模型预检**（见"机制"第 3 步）：配置漂移（模型改名/下线）时只会跳过并 warn，不会对着一个不存在的模型反复发请求。
4. `run` 手动触发现在也会像后台 tick 一样，给每个账号打一行 `host.log`（含预检跳过的情况），方便对着 journal 排查。

## v0.2.0（i18n：跟随 CPA 语言）

1. 新增消息目录 `i18n.go`（`zh-CN`/`zh-TW`/`en`/`ru`），覆盖所有 `hostLog` 文本、`roundOutcome.Warning`、`manualRunResult.Skipped` 的原因、`status`/`run` JSON 里的人类可读字段；`status`/`run` 响应新增 `"lang"` 字段。
2. 新增配置项 `language`（默认 `auto`），语言协商规则见上面"国际化（i18n）"一节。
3. 新增状态页面 `GET /v0/resource/plugins/cpa-quota-warmup/panel`（自包含 HTML，登记簿/表格风格，跟随管理中心主题与语言），管理中心菜单条目挪到这个页面上（`status`/`run` 不再带菜单标签，纯数据/动作端点）。
4. `ConfigFields` 的 `Description` 从纯英文改成"中文 / English"双语一句话（注册时是静态字符串，无法跟随访客浏览器语言）。
5. **实现过程中用真实单元测试挖出一个真 bug并修复**：本机 `LANG=C.UTF-8`，`languageFromEnv` 最初把编码后缀（`.UTF-8`）剥离的顺序放在了"是不是 `C`/`POSIX`"判断之后，导致 `C.UTF-8` 被误判成英语而不是"未设置"。已把剥离顺序调整到判断之前，并补了 `TestLanguageFromEnv` 里 `C.UTF-8`/`POSIX.UTF-8` 两个用例锁定这个修复。
6. 单元测试新增：语言协商全链路（query/Accept-Language/LANG/兜底，含优先级）、`tr()` 缺失语言/缺失 key 回退、四语言消息表 key 集合一致性、`status`/`run` 响应的 `lang` 字段与 `?lang=` 覆盖、`language` 配置项的规范化、面板 HTML 渲染（无残留占位符、各语言标题正确、未知语言回退英文）。ABI 集成测试新增：`management.register` 现在有 3 个资源路由（`panel`/`status`/`run`，只有 `panel` 带菜单）、`GET .../panel` 返回 `text/html` 且包含 `cli-proxy-language`/`cli-proxy-theme`、`status`/`run` 的 `?lang=` 生效。

## v0.2.1（修复：状态页静态文案不跟随 cli-proxy-language）

**线上实测发现的真 bug**：用 agent-browser 打开 `/panel`、在 `localStorage` 设好 `cli-proxy-language=zh-CN` 并刷新后，`document.documentElement.lang`、配置表行标签、账号状态列都已经是中文（这些走的是页面 JS 里动态渲染路径，本来就调用 `t()`），但标题、副标题、"Configuration"/"Refresh"/"Accounts" 等区块标题、表头（NAME/PROVIDER/MODEL/...）、输入框占位符、页脚这些**静态**文案仍然是英文。

**根因**：这些 `{{ui_*}}` 只在服务端首屏渲染时按 `Accept-Language`（无头浏览器发的是它自己的 OS/CLI 默认值，不是 `cli-proxy-language` 里存的那个）替换过一次；页面自己的 JS 算出真实语言后，只把 `t()` 用在了"以后动态渲染"的节点（配置表、账号表、最近记录、手动执行结果），从没回头重刷这批已经写死在 HTML 里的静态文案。

**修法**：模板里每一处渲染 `{{ui_*}}` 的元素都补上 `data-i18n="<key>"`（`<input>` 的 `placeholder` 用 `data-i18n-placeholder="<key>"`，`<title>` 同样处理）；页面 JS 在算出真实 `lang`/`dict` 之后，紧接着遍历 `[data-i18n]`/`[data-i18n-placeholder]` 用 `t(key)` 覆盖 `textContent`/`placeholder`，覆盖掉服务端首屏猜错的语言。服务端首屏替换本身保留（避免真的猜对时出现一次可见的重排/闪烁）。

新增单测 `TestPanelTemplateStaticTextHasDataI18nAttributes`：对模板里每一个 `{{ui_*}}` 占位符出现的位置（不是去重后的 key 集合——`ui_page_title`/`ui_col_provider`/`ui_col_model`/`ui_loading` 等 key 在模板里各出现不止一次，分别在 `<title>`/`<h1>`、两张表各自的表头等不同元素上），定位其所在的最近外层标签，断言标签内必须有匹配的 `data-i18n`/`data-i18n-placeholder` 属性；用"故意去掉 `<h1>` 上的属性、保留 `<title>` 上的"的方式手动验证过这条测试确实会 fail（而不是一个形同虚设的集合子集检查）。ABI 集成测试补了两条：面板 HTML 里 `data-i18n="ui_page_title"` 与 `data-i18n-placeholder=` 都存在。

## v0.3.0（配置大幅简化：3 行起步、自动选模型、cron、面板可编辑模型）

用户反馈配置太复杂、小白看不懂。这一版把最小配置压到 3～4 行，其余全部自动，高级项收进 `advanced:`：

1. **新配置：`time`/`model`/`accounts`（+ 可选 `advanced:`）**。`time` 接受单个 `"HH:MM"`、逗号分隔的字符串、列表，或者标准 5 段 cron 表达式（自己写的 5 段解析器，见 `cron.go`，只用简单 AND 逻辑组合五个字段，覆盖 `*`/`*/n`/`a,b`/`a-b`/星期几）；`accounts` 是账号文件名的 glob 列表（`"*"` = 全部，缺省/空 = 不预热任何账号，且会在 status/日志里明确提示），列表项也可以写成 `{match, time, model}` 对象单独覆盖某个账号；`model` 默认 `"auto"`，按内置的"从便宜到贵"候选表（详见"快速开始"）从这台实例实际暴露的模型里自动选，`GET /v1/models` 拉不到就退回候选表第一个，`codex` 自动带 `reasoning_effort: "low"`；也可以直接写模型名，或写成 `{provider: model}` 映射（等价于 `advanced.models`）。
2. **时区默认自动**：不配置时用宿主进程自身的 `time.Local`（systemd 部署下就是系统时区），可用 `advanced.timezone` 覆盖。
3. **模型优先级**：面板手动设置 > 账号对象 `model` > 顶层 `model`（非 `auto`） > `advanced.models[provider]` > 自动选择；任何一层给出的显式模型都要过 `GET /v1/models` 预检，不在里面就跳过并 warn（预检本身失败时放行照发）。
4. **面板可编辑模型**：`status` JSON 新增 `available_models`（`GET /v1/models` 的 `data[].id` + `owned_by`，拉不到就是空数组）；面板账号表新增可编辑的模型列（`<input list=...>` + 数据源下拉 + 保存/自动按钮），配置摘要区也有一个全局模型的同款输入框；新增 `GET .../set?scope=global|auth&auth=<name>&model=<id|auto>` 路由，写到 `<CPA 工作目录>/quota-warmup/overrides.json`（原子写，`reconfigure`/重启后仍生效），`model=auto` 是**清除**这条覆盖而不是存字面量 `"auto"`；`status` 每个账号带 `model_source` 标出模型来自哪一层。
5. **向后兼容**：旧版 `default:`/`providers:`/`auths:`/`timezone:`/`base-url:`/`api-key:`/`message:`/`max-tokens:`/`max-rounds:`/`catch-up-minutes:` 这些顶层键原样保留、解析结果与之前完全一致；检测到旧格式时 `plugin.register` 打一行提示日志，但不影响运行。`config_new_format_test.go` 的 `TestDecodeConfigOldAndNewFormatsAreEquivalent` 证明两种写法描述同一意图时解析结果等价。
6. **`ConfigFields` 从约 22 条精简为 4 条**（`enabled`/`time`/`model`/`accounts`/`advanced`，每条一句中英双语说明，`advanced` 的说明里列出全部子键）；`priority` 保留在顶层，不算这个插件自己的配置项。
7. 新增文件：`candidates.go`（内置候选模型表 + 自动选择）、`cron.go`（5 段 cron 解析/匹配/下一次触发计算）、`overrides.go`（面板模型覆盖的原子读写）；`sender.go` 新增 `ListModelsDetailed`（带 `owned_by`，供面板下拉框分组）。
8. 新增/更新测试：`cron_test.go`（cron 各写法 + 到期/补发边界）、`overrides_test.go`（读写 + 跨重载持久化）、`config_new_format_test.go`（新格式解析、账号对象覆盖、模型优先级链、新旧格式等价性）、`sender_test.go`（`httptest` 起真 HTTP server 测 `AvailableModels`/`ListModelsDetailed`）、`management_new_format_test.go`（`/set` 路由的校验/保存/清除、新格式 `status` 的 `time`/`accounts`/`model_source`/`available_models`）、`ops_merge_config_test.go`（直接从 `ops/merge-config.py` 里抽取 `BLOCK` 常量喂给 `decodeConfig`，防止脚本与解析器的契约漂移）。ABI 集成测试新增 `/set` 路由与新配置格式相关断言。
9. i18n 目录新增约 20 个 key（旧格式检测提示、未配置 accounts 提示、时间表达式解析失败提示、候选模型均不可用提示、`/set` 路由的四种响应文案、面板新增的标签/按钮/列头），四语言齐全，`TestMessageCatalogsHaveTheSameKeys` 继续保证四张表 key 集合一致。
10. `pluginVersion` 升到 `0.3.0`。

## v0.4.0（多账号配置改为独立文件，每个认证文件一段）

用户反馈"多账号还是难配"（在一份 `config.yaml` 里维护一堆 glob/覆盖对象容易出错）。这一版把账号配置从 `config.yaml` 挪到插件自动生成维护的 `quota-warmup.yaml`，每个认证文件一段，改一个字段就行：

1. **新默认「文件模式」**：`config.yaml` 只需要 `enabled: true`（可选 `config-file:` 指定路径，默认 `<cwd>/quota-warmup.yaml`）。插件启动/每次 tick 都会检查并维护这份文件：不存在就用 `host.auth.list` 全量生成（每个非 runtime-only 认证文件一段，`enabled: false`，行尾 `# provider: xxx` 注释）；已存在则在 **yaml.v3 Node 级别**增量编辑——新账号追加、消失的账号在行尾追加 `# 认证文件不存在`（账号回来后自动去掉），用户自己的键值、注释、格式全部保留，不做整份重新生成；只有真的有变化才原子写回（temp+rename）。见 `warmupfile.go`。
2. **`quota-warmup.yaml` 结构**：`defaults: {time, model}` + `accounts: {<认证文件名>: {enabled, time, model, message?, max_tokens?}}`；账号段可以省略字段（缺省用 `defaults`，`enabled` 缺省 `false`）。`time`/`model` 语法与 v0.3.0 一致（HH:MM/逗号列表/cron；`auto` 或具体模型名）。
3. **热加载与容错**：每次 tick 按 mtime 检测变化重新解析；**解析失败绝不影响调度**——继续用上一次解析成功的配置，打一行 `host.log`（四语言）并在 `status`/面板上显示错误信息，等用户改好后自动恢复。`status`/`set` 请求也会顺带触发一次生成/校对，所以刚注册完插件、tick 还没跑到时打开面板也能看到已生成的文件内容。
4. **面板可编辑 启用/时间/模型**：账号表每行新增「启用」勾选框和可编辑的「时间」输入框（模型编辑沿用 v0.3.0 的 `<input list=...>` + 下拉数据源）；新 `GET .../set?auth=<name>&enabled=<bool>&time=<...>&model=<id|auto>`（只传的字段会被写入，其余不动）直接 Node 级写回 `quota-warmup.yaml` 对应账号段，不再需要 `overrides.json` 这一层。面板顶部显示当前 `mode`（`file`/`inline`）与文件路径。
5. **`overrides.json` 一次性迁移**：升级后若发现 v0.3.0 遗留的 `overrides.json`，第一次启动会把其中的模型覆盖（按账号 + 全局）并入新生成的 `quota-warmup.yaml`，再把原文件改名为 `overrides.json.migrated`；只搬模型，不搬"是否启用"。
6. **完全向后兼容**：`config.yaml` 里仍保留 v0.3.0 顶层 `time`/`model`/`accounts`，或 v0.1.0/v0.2.0 的 `default`/`providers`/`auths`/... 这些旧键 → 继续按原逻辑跑，统一归类为「内联模式」，**不生成/不接管** `quota-warmup.yaml`；`status` 里 `config.mode` 显示 `inline`（新文件模式是 `file`），`plugin.register` 检测到内联配置时打一行提示可以精简迁移的 `host.log`。`advanced:`（`base-url`/`api-key`/`language`/`log`/`max-rounds`/`catch-up-minutes`/`message`/`max-tokens`）两种模式下都从 `config.yaml` 读，行为不变。v0.1.0/v0.2.0 内联模式下 `/set` 直接返回 501（这个格式本来就没有面板保存能力）；v0.3.0 内联模式的面板行为完全不变。
7. **`ConfigFields` 从 5 条进一步精简为 3 条**：`enabled`、`config-file`、`advanced`。
8. 新增文件：`warmupfile.go`（`quota-warmup.yaml` 的 Node 级读写/生成/增量维护/热加载与解析失败回退，`resolveFileAuth` 是文件模式的调度解析）；`overrides.go` 新增 `migrateOverridesToWarmupFile`。`runner.go` 的 `groupDueTargetsNew`/`groupDueTargetsFile` 共享同一个 `groupDueTargetsFromResolver`，`tickNew`/`tickFile` 共享同一个 `runTickForGroups`，`manualTriggerNew`/`manualTriggerFile` 共享同一个 `runManualTrigger`——三种模式除了"怎么解析出一个账号该在什么时间用什么模型预热"这一步，调度/发送/覆盖核对/持久化逻辑完全一致。
9. **实现过程中用真实单元测试挖出一个真 bug**：`yaml.v3` 会把空 mapping（`accounts: {}`）序列化成 flow style，一旦后续往里追加带行尾注释的账号段，再次编码会生成语法错误的 YAML（`accounts: {a.json:, # 注释\n{model: x}}` 这种断行断在 flow 集合中间）。已在每处可能"追加子节点"的地方强制恢复 block style 修复，回归测试见 `TestWarmupFileManagerSetAccountCreatesMissingSection`。
10. 新增测试：`warmupfile_test.go`（生成、增量追加保留注释、消失账号标注与恢复、解析失败回退与自愈、`setAccount` 保留注释写回、创建缺失账号段、只在有变化时才写盘）、`warmupfile_migration_test.go`（`overrides.json` 迁移三种情况：正常迁移、文件不存在、文件损坏）、`management_file_mode_test.go`（`status` 的 `mode`/`config_file`、`/set` 的启用/时间/模型写回与校验、内联两种模式的 `mode`/`/set` 门禁）、`runner_file_mode_test.go`（`manualTrigger`/`tick` 全链路：启用账号→发预热请求→usage 覆盖核对→`state.json` 落盘，全部走真实引擎代码、只在网络层用 fake）。`ops_merge_config_test.go` 同步更新为校验新的最小 `BLOCK`。ABI 集成测试同步更新为新的 `BASE_CONFIG`（`enabled: true` + `config-file` 指向临时文件）与 `/set` 的新参数形状。
11. `pluginVersion` 升到 `0.4.0`。

## v0.4.1

- 面板账号表版式整理：列顺序改为 名称/provider/启用/模型/时间点/下次触发/状态/操作，未启用账号显示灰色「未启用」，保存/自动按钮并入操作列。

## v0.5.0（面板在线编辑 quota-warmup.yaml）

线上验证 v0.4.0/v0.4.1 通过。这一版让面板直接内嵌一个文本编辑器，整份 `quota-warmup.yaml` 不用离开浏览器就能改：

1. **新增两个路由**（仅文件模式，都是 GET）：`GET .../config-yaml` 返回 `{path, content, mtime, error}`；`GET .../config-yaml/save?content=<base64url 编码，无 padding>&mtime=<上次读到的 mtime>` 校验通过后原子写入并立即热加载、返回新 `mtime`。校验顺序：先 `yaml.v3` 解析 + 反序列化进强类型结构（天然覆盖 `defaults`/`accounts` 顶层结构、各字段类型是否正确），再额外校验每个 `time` 表达式必须真的能被解析（不能只是随便一个字符串）；失败一律 `400 {error, line, column}` 且不写盘（`line`/`column` 来自 `yaml.v3` 自己的错误文本或者已解析节点的 `Node.Line/Column`，1-based）；`mtime` 跟当前文件不一致返回 `409`（"文件已被别处修改，请刷新"）。
2. **面板新增「编辑配置文件」区块**（账号表下方）：等宽字体 `<textarea>`、`spellcheck=false`、Tab 键插两个空格；「重新载入」「保存」两个按钮；只在文件模式下显示（`status` 的 `config.mode !== "file"` 时整块隐藏），首次进入文件模式时自动拉一次内容，之后只在手动点「重新载入」或保存成功后才刷新编辑器内容——**30 秒的后台自动刷新绝不碰这个编辑框**，避免打断正在输入的内容。校验失败在编辑器上方用红字显示「第 N 行第 M 列：错误信息」；保存成功刷新账号表。
3. **转义与换行**：编辑器初始内容单独走 `GET .../config-yaml`（不复用 `status` 接口），用 `fetch` 拿到后 `textarea.value = content` 赋值，不把 YAML 内容拼进 HTML 模板（避免转义/XSS 问题）；提交时 `btoa(unescape(encodeURIComponent(text)))` 转标准 base64 再做 `+`→`-`、`/`→`_`、去 `=` 三步替换成 base64url，服务端 `base64.RawURLEncoding` 精确解码；解码后统一把 `\r\n`/`\r` 归一化成 `\n`。补测试覆盖含中文注释、引号、反斜杠、`#`、多行的 YAML 整个往返（编码→解码→写盘→再读回）字节级一致。
4. **说明文字只保留中文**：这个功能新增的所有文案（编辑器区块标题/按钮、以及新增的服务端错误提示如"文件已被别处修改，请刷新"）都只写了中文，`i18n.go` 里对应的键四种语言表填的是同一句中文（不是真的四语言翻译，只是为了不破坏 `TestMessageCatalogsHaveTheSameKeys` 的 key 集合一致性检查）；`ConfigFields` 的三条说明也从"中文 / English"双语改成纯中文。已生成的 `quota-warmup.yaml` 头部注释和 `# provider: xxx` 行尾注释本来就是纯中文，不用改。
5. **安全**：`GET .../config-yaml`/`GET .../config-yaml/save` 和其他 resource 路由一样不做鉴权；README 新增提示——面板现在能读写配置文件全文了，CPA 若暴露在公网/不受信任网络，请在反代层限制 `/v0/resource` 的访问。
6. **实现过程中用真实测试挖出一个真 bug**：两次紧挨着（无人为延迟）的原子写（`os.CreateTemp` + `os.Rename`）在这台开发机的文件系统上会落在完全相同的纳秒级 `ModTime()` 上——如果直接拿 `ModTime()` 当 `mtime` token，会把一次真实的"文件在两次读写之间被改过"的冲突误判成"没变"，从而允许一次本该被拒绝的覆盖写入。用 ABI 集成测试（连续三次 `/config-yaml/save` 调用）实测复现后，改成 `ModTime + 本进程内写入序号` 拼接出的 token（见 `warmupfile.go` 的 `mtimeToken`/`writeSeq`），保证同一个 `warmupFileManager` 实例做的任意两次写永远返回不同 token；回归测试 `TestMtimeTokenDistinguishesWritesWithIdenticalModTime`/`TestOverwriteRawAlwaysProducesADistinctToken`。
7. 新增/更新测试：`management_config_yaml_test.go`（读取内容/路径/mtime、含中文注释引号反斜杠井号多行的保存往返、CRLF 归一化、无效时间表达式的 400+line+column、畸形 YAML 语法的 400、过期 mtime 的 409 冲突、缺参数的 400、非文件模式下两个路由都是 501）、`warmupfile_test.go` 新增两条 mtime token 回归测试、`panel_test.go` 未新增（本次面板改动只是新增一个区块，未改动既有的 i18n 覆盖测试所覆盖的元素结构）。ABI 集成测试新增 `/config-yaml` 读、`/config-yaml/save` 保存成功、校验失败三条断言（外加一条 mtime 冲突的 409 断言），并把 `Resources` 路径的期望列表更新为 6 条（末尾追加 `/config-yaml`、`/config-yaml/save`）。
8. `pluginVersion` 升到 `0.5.0`。

## v0.6.0

- 面板排版重做：沿用 CPA 管理控制台的主题变量（暖灰亮/暗两套），配置区改为摘要 chips + 折叠高级设置，账号表改为勾选/输入即自动保存（去掉保存按钮），时间格式化为 MM-DD HH:MM，最近记录用 ✓/✗，编辑器样式对齐控制台。

## v0.6.1

- 模型选择改为自定义下拉：点开即列出全部可用模型（按 provider 分组、输入即过滤、键盘可选），替换只在前缀匹配时才弹出的原生 datalist。

## v0.6.2

- 账号行的模型下拉从"全量模型"收窄到"该账号所属 provider 的模型"：新增 `candidates.go` 的 `modelsForProvider`（`owned_by` 映射表 + 候选表并集，见"面板与模型选择"）驱动兜底推断，`status` 每个账号新增 `models`/`model_hint` 字段；面板下拉不再分组，列表空时提示「该 provider 暂无可用模型」，手输不在列表里的模型仍可正常保存，只多一行不阻塞的灰字提示。
- 追加：面板作为控制台同源 iframe 时，优先照抄控制台自己对 `localStorage['cli-proxy-auth']` 的客户端解密算法，直接调控制台的 `GET /v0/management/auth-files/models?name=...` 拿账号的真实可用模型（比 `owned_by` 启发式准），全程只在浏览器发生、密钥不经插件后端；拿不到时静默退回 provider 推断。详见"面板与模型选择"。

## v0.6.3

- 修复：异步取到账号模型（或回退到 provider 推断）后，误把输入框现值当过滤词导致列表只剩当前模型。

## 图标

菜单图标按控制台内置图标的规格（24×24 圆角方块、`--primary-color` 底色、`strokeWidth 2` 白色线条）自行绘制，不含第三方素材。
