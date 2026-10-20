# WorkBuddy2API-Agent — 国际版、国内版多账号反代网关

<p align="center">
  <a href="https://github.com/cookie-kangd/workbuddy2api-agent/releases"><img src="https://img.shields.io/badge/Release-v0.1.22-2496ED?style=flat-square" alt="Version 0.1.22"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
</p>

把腾讯 **[www.workbuddy.ai](https://www.workbuddy.ai)**（国际版）与 **[codebuddy.cn](https://www.codebuddy.cn)**（国内版）的原生服务封装成标准 OpenAI 兼容接口（Chat Completions / Responses）与原生 Anthropic Messages 接口，并补齐多账号调度与运维能力。

本仓库是**自有发行线**，从 `v0.1` 重新起算版本号，主打三件事：**内存占用低**、**只发 Windows 便携包**、**Codex 桌面程序一键接入**。相对沿用的上游基线做了大幅精简，改动清单见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

## 核心卖点

- **低内存占用**：纯 Python 标准库、零 pip 依赖；大模块（智能体配置、定价、账号池）按需懒加载；面板统计走增量缓存 + 后台预热，首屏不做全量重折；常驻后台线程收敛到必要的那几条。闲置时进程常驻内存只在小几百 MB 量级（随账号数与历史用量浮动）。
- **只发 Windows 便携包**：Release 流水线只构建 Windows 绿色版 `workbuddy2api-agent-<版本>.zip` + `SHA256SUMS`，不再构建 Docker / OpenWrt 产物，也不再跑多余的工作流。
- **Codex 桌面程序一键接入**：看板「智能体配置」页可直接给本机 Codex App 写入模型目录与凭据，Bearer 令牌直连网关，**无需 CC Switch**。
- **升级交给脚本**：面板不再有「更新检查」板块与后台检查线程；升级用随包提供的 `update-wb-proxy.bat` / `update-wb-proxy.ps1`。
- **双区域独立路由**：国际版 / 国内版各自配置与调度，看板一键切换，状态落盘。
- **三协议**：Chat Completions、Responses API（Codex）、原生 Anthropic Messages（Claude Code / Anthropic SDK）。
- **多 Key 与出口绑定**：每个 Key 可固定走国际版或国内版、可限定模型，用量按 Key 归属。
- **Web 看板**：指标卡片、模型性能与用量大表、请求流水、账号与任务管理一屏可查。

 ⚡ **Vibe Coding 产物**：本项目为 100% Vibe Coding 协同产物——人类开发者提出架构与业务意图，AI 助手端到端完成逆向分析、链路调度、WAF 指纹脱敏与界面编写。

 📌 **提交代码贡献前**：请先查看并**严格遵循**贡献手册 [`CONTRIBUTING.md`](CONTRIBUTING.md)——提交格式、测试门禁与端到端冒烟要求都在那里（AI / 智能体请特别注意其中的硬性规则节）。

---

## 一、快速启动

### 1. Windows 便携版（推荐）

从 [Releases](https://github.com/cookie-kangd/workbuddy2api-agent/releases) 下载 `workbuddy2api-agent-<版本>.zip`，解压后双击 **`start-wb-proxy.bat`**，保持窗口运行。

启动后：**API 地址** `http://127.0.0.1:8788/v1`，**Web 看板** `http://127.0.0.1:8788/`。首次启动还没有账号，打开看板点「+ 添加账号 (OAuth)」完成授权即自动入库。

绿色包自带精简 Python 运行时（Windows 专属）。macOS / Linux 想本地跑，请直接用系统 Python（**3.9+**）执行 `./start-wb-proxy.sh [端口]`。

### 2. 面板访问密码

看板需要**面板访问密码**（默认 `admin`），它与 API Key 相互独立：密码只用于打开看板，可在「设置」页修改（或启动时 `--panel-password` 指定），以 PBKDF2-SHA256 摘要存于 `accounts/settings.json`（不存明文），登录态只留在浏览器会话里。

本机 / 受信局域网自用可带 `?pwd=面板密码` 免手输直接进面板（如 `http://127.0.0.1:8788/?pwd=admin`）；公网暴露时不要用——密码会留在浏览器历史与反向代理日志里。

### 3. 局域网共享

双击 **`start-wb-proxy-lan.bat`**（或 `./start-wb-proxy-lan.sh`）以 `--lan` 启动，监听所有网卡并强制要求 API Key（首次启动自动生成、写入 `accounts/settings.json` 并打印在日志里）。手机 / 其他电脑把 Base URL 指向 `http://<本机局域网IP>:8788/v1` 即可。

### 4. 关闭服务

静默启动后服务是 `pythonw` 进程，**没有任何窗口可以关**——关掉日志查看器窗口不影响服务。
要停掉它，除了去面板顶栏点那个红色电源按钮，也可以直接用包里的关闭脚本：

```bat
close-wb-proxy.bat
```

它做的和面板按钮是同一件事：面板登录 → `POST /shutdown` → 轮询 `/health`
确认端口真的释放了。不是按 PID 杀进程，所以服务有机会把最后一条用量记录写盘。

- **双击即关**：无需按 y，也不需要打开浏览器；执行完显示结果并倒计时 3 秒，窗口自动关闭（`--countdown 0` 可立即退出）
- 非默认端口：`close-wb-proxy.bat 9000`
- 面板密码不是默认 `admin` 时，先 `set WB_PANEL_PASSWORD=<密码>`，
  或直接用 `python close_wb_proxy.py --password <密码>`
- 服务没在运行时它会直接说 nothing to stop，不会误伤同端口上的其他程序
- 被容器或服务管理器自动拉起时，它会如实提示「已发送但仍在运行」，不谎报成功

### 5. 升级到新版本

面板**没有**更新板块，升级用随包脚本（脚本会保留 `accounts\`、`usage\` 与自身）：

```powershell
# 双击 update-wb-proxy.bat，或命令行：
powershell -ExecutionPolicy Bypass -File .\update-wb-proxy.ps1
```

常用参数：`-DryRun` 只看版本不改动、`-Diagnose` 只做网络体检、`-ZipPath <zip>` 用本地包离线更新、`-Backup` 更新前打包备份账号与用量。脚本会从发布页读取**真实附件名**，最新版暂时没有便携 zip 时自动回退到上一个有 zip 的版本。

### 6. 测试

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只跑名字里含 realm 的
```

Docker 一键部署支持三种模式（环境变量放在管道后的 `bash` 前面）：

```bash
curl -fsSL .../quick-deploy.sh | WB_DEPLOY_MODE=public bash
```

默认 `lan`（`8788:8788`）；`local` / `public` 只把 8788 绑到 `127.0.0.1`，
公网服务器请再套 Nginx / Caddy / Tunnel 并加 TLS。

- 191 个套件：143 个 Python + 48 个 JS；JS 需要 PATH 上有 `node`，缺失时会跳过并提示。
- `tests/_mobile_check.py` 是独立的 Playwright 手机/桌面布局检查器（需自行安装 Playwright），按需手动运行，不在上面的套件集里。
- CI（`.github/workflows/tests.yml`）跑同一条命令：Windows + Python 3.12；推送 `v*` tag 时额外断言 **tag == 源码版本**。

---

## 二、Codex 桌面程序一键接入

看板「智能体配置」页可检测本机已装的 AI 客户端，一键把它们的配置指向本网关，改前自动备份、随时字节级还原：

| 客户端 | 写入位置 | 协议 |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | 原生 Anthropic Messages（自动剥 `/v1` 后缀） |
| Claude Desktop | `%LOCALAPPDATA%\Claude-3p\configLibrary\`（微软商店版与 exe 版同一路径） | 原生 Anthropic Messages（3P 网关 profile，模型名只能是 `claude-<角色>-<序号>`） |
| Codex CLI / App | `~/.codex/config.toml`、`~/.codex/auth.json`、`~/.codex/wb-proxy-model-catalog.json` | Responses API（含桌面端模型目录，`model_catalog_json` 指向该文件） |
| OpenCode | `~/.config/opencode/opencode.json` 等 | OpenAI 兼容 |

**Codex 接入要点（与 CC Switch 的差异）**：

- 写 `model_provider` + `model` + `model_catalog_json` + `disable_response_storage`；
- provider 表里放 `experimental_bearer_token` 直连令牌，并把 `requires_openai_auth = false`，避免 Codex 回落到 OpenAI 官方登录；
- **不写 `env_key`**：写了 Codex 会把 `env_key` 当作鉴权来源而丢掉内联令牌，导致请求不带 Bearer、在「优化对话 / 重连」之间死循环；
- 模型目录的 `context_window` 采用网关已解析的真实上限（可达 1M），与 `/v1/models` 的 `context_length` 一致，避免 Codex 过早自动压缩上下文。

**配置完成后自动重启**：Codex 桌面版与 Claude Desktop 都只在启动时读一次配置，改完文件不重启就等于没改。看板点「一键配置」后，网关会**只重启正在运行的那一个**客户端（按可执行文件路径精确匹配，不会误杀同名的其他程序），没在运行的则提示"下次打开生效"。切换「国际版 / 国内版」出口时同理：会自动重写各客户端里嵌的模型清单，并重启正在运行的客户端。

 需要完全禁止这个行为（例如自动化环境）可设 `WB_AGENTS_NO_RESTART=1`：重启会被拒绝并记录原因，配置照常写入。

 **Claude Desktop 商店版 / exe 版都能用**：微软商店（MSIX）安装的 Claude 通常没有开启
 文件系统虚拟化，和 exe 版读同一个 `%LOCALAPPDATA%\Claude-3p`，两种安装方式共用同一
 套路径；只有在系统确实把它沙盒化时才改用包容器内的目录。配置完成后 Claude 需要重启一
 次（或看板「一键配置」的自动重启）才会在模型选择器里出现 `claude-sonnet-5` 等 4 个
 角色入口，它们都映射到你在看板选的那个模型。

也可以完全不装看板会话，直接用脚本接入（Key 在「设置」页生成）：

```bash
python wb_agents.py apply codex --base-url http://127.0.0.1:8788/v1 --api-key <你的Key>
```

---

## 三、核心特性

### 1. 模型列表对齐官方桌面端

对官方本地配置清单（50+ 底层模型）做清洗：剔除行内补全专用模型（`codewise-*`、`completion-gf`、`hunyuan-3b/7b` 等）与多云专线变体（`*-volc`、`*-lkeap` 等），严格对齐官方 Windows 桌面端，并宣告每个模型的上下文窗口、单次最大输出、视觉支持、工具调用与推理档位。上游新上架的模型无需发版即出现在 `/v1/models`。

 同名模型（如 `deepseek-v4.1-flash`）**暂未实现跨国内 / 国际账号的混合轮询**：两个区域作为独立出口分别配置与调度，请求只走当前所选网关。这是出站指纹对齐与账号防风控的取舍。

### 2. 设备指纹隔离（`derive_id`）

国际版与国内版共用同一套算法内核：以账号 UID 结合固定业务盐值单向哈希派生机器码与会话标识——同一账号每次出站都来自同一台虚拟设备（不随机漂移），不同账号之间彼此独立（阻断跨账号关联风控）。

### 3. 国内版自动化

- **每日签到**：一键完成国内版打卡领积分；
- **自动连续打卡与连登管家**：每日定时为国内版账号上报轻量会话事件，点亮官方成长中心连登天数与热力墙；自动闭环检查昨日漏签（有补签卡则自动补签保连登）、领取新手礼包/补偿、兑换 7d/14d/28d 连登档位奖励，并自动抽完抽奖次数；
- **成长任务与积分任务**：自动批量接取未接任务，构造规范行为事件上报点亮，并自动领奖入账；
- **猫猫日常**：自动检查旅行状态，在家自动派出、归来自动领奖。

### 4. 后台常驻调度器

- **每日 09:00 & 21:00**：国内版账号自动签到、对话活跃上报（点亮连登）与猫猫旅行闭环；国际版账号执行每日活跃打卡；
- **每日 22:00**：集中扫描全库账号，Token 剩余寿命不足 2 小时自动调用 Refresh Token 保活；
- **每日 01:00**：深夜时段执行夜猫子任务；
- 看板顶部另有「立即巡检保活」与「每日活跃打卡 (国际版)」可随时手动触发。

### 5. 限额护栏（设置 → 账号限额）

四条账号级护栏放在同一张表里，每行一条、每列一个作用域：保留积分、每日 Token 限额、每日积分限额、按模型每日 Token 限额。每条填 `0` 表示关闭（默认值），按**本地时间 0 点**解封，只在请求路径生效。

### 6. OpenRouter 价估算（等价 token 花费）

把每条请求的 token 消耗按 **OpenRouter 公布的模型价**折算成等价金额，回答「这些 token 放在 OpenRouter 上值多少钱」——与账号实际扣除的积分是两个口径，看板里并列显示：

- **总开关**（「设置 → 模型价格估算」，默认开启）：关掉后价格列、「API 等价花费」卡片、取价控件与未定价清单一起隐藏，后端也不再取价与折算；重新打开会立刻抓一次价并**补算关闭期间的请求**（历史日志一个字不改，金额始终是读时算的）。
- **计价口径**：输入按缓存命中 / 未命中两档单价拆分，输出单独单价，乘 token 数再按汇率折算；输出的 token 数取上游 `completion_tokens`（已包含推理 token，不另计）。按条件定价的模型（按输入长度或按 UTC 时段）按每条请求取最紧的那一档。
- **定价来源**：OpenRouter 模型目录的**模型级公布价**。OpenRouter 把它定义为该模型 top provider 的价——对多 provider 模型通常就是最低档（实测 `deepseek-v4.1-flash` 的模型级价只比 34 家 provider 的最低价高 5%）。这组数字只用于「这些 token 放在 OpenRouter 上值多少钱」的参考估算，**不等于 WorkBuddy 官方价，也不能用于结算**。每份价格按内容存成一条「定价策略」，每条请求只记引用了哪条策略，所以之后调价不会改写历史数字；`wb_pricing.py` 另内嵌一份快照作出厂价。
- **刷新**：「设置 → 定价刷新」默认每 5 分钟一次（填 0 关闭）；只有价格真的变了才写策略与时间轴，价格不动时每轮只是一次 HTTP GET。面板可看当前生效策略、上次 / 下次取价时间与失败原因，也能点「立即取价」。
- **新增模型自动取价**：候选清单 = 内置目录 ∪ 网关实时目录，上游新上架的名字 5 分钟内进入取价；两次取价之间就被调用也会按需补价（请求路径不发网络请求）。匹配顺序：面板手填覆盖 → 人工覆盖表 → 同名唯一命中 → 变体后缀继承（剥 `-lkeap` / `-taiji` / `-volc` / `-sg` 等，仍要求唯一命中），继承可在设置里整体关掉。
- **按模型校准**：在 `usage/pricing-markup.json` 写 `{"deepseek-v4.1-flash": 5.36}` 之类的系数，估算金额会乘以它；文件缺失或值非法时按 1.0。它只影响读时估算，不改写历史策略。
- **未定价可见**：`/pricing` 列出未定价模型及分类（别名 / OpenRouter 无对应 / 剥后缀后无唯一基准）与相似候选（仅建议，不自动采用），可在「设置 → 定价刷新」为某条手填 OpenRouter id 并登记（写 `pricing-overrides.json`，不改源码，登记后立即取价）。
- **展示位置**：数据看板「API 等价花费」卡片、各账号用量透视与模型性能表**最后一列**、网关页「估算价格」卡片、最近请求**最后一列**（悬停可看完整定价链：三档原始单价、汇率、匹配证据、策略 id 与补算标记 `*`）。
- **人民币 / 美元**一键切换（记在浏览器本地）；快照覆盖不到的模型显示 `—`，不猜数字。
### 7. 本地网络工具（可选，默认关闭）

Codex App 这类客户端会在 Responses 请求里声明 `web_search` / `web_fetch`，而上游没有对应的执行器。打开「设置 → 本地网络工具」后，网关把那份声明换成自己的同名 function，拦下模型的调用并在本地执行（搜索在 DuckDuckGo / Bing / Wikipedia / Stack Overflow / Google News 之间自动轮换，抓页面抓模型给出的 URL），再把结果喂回模型。关闭时声明原样透传。

### 8. 剩余用量估算（数据看板 → 剩余用量估算）

每个账号 × 模型在当前 24h 窗口的已用 / 预算 / 剩余（预算取撞线账号的平均用量），只读内存快照、不发上游请求，用来判断「哪个号先耗尽、要不要换号」。

---

## 四、账号添加与管理
窗口探针管的是「已经撞到 429 之后」；想提前知道某条配额线有没有被打满，可在「设置 → 模型额度定时主动查询」勾选模型：网关每 6 小时为每个「账号 + 模型」发一个最小请求做体检，查到 429 就立刻记该模型的冷却并把重置时刻交给窗口探针，请求通了就说明还没到限额。一旦某个「账号 + 模型」已经有了登记的 429 记录，就停止定时查询（改由窗口探针负责），记录消失后自动恢复轮询；每次查询都会消耗该账号极少量额度，默认不勾选任何模型。

### 方式一：浏览器 OAuth 授权（推荐，免客户端）

看板「+ 添加账号 (OAuth)」→ 浏览器完成登录授权 → 回调自动入库。国际版与国内版分别选择对应入口。

### 方式二：从本机桌面客户端导入

1. 先在本机桌面客户端里登录（这一步不能省）；
2. 看板点「扫描桌面客户端账号」，弹窗列出本机已登录的国际版 / 国内版账号；
3. 若提示凭据已加密，先把解码密钥交给网关，再点账号行的「导入」：
   - **Windows**：点弹窗里的「回收密钥」（只读，实测 1 秒内完成）；
   - **macOS / Linux**：进程内存回收只在 Windows 上实现，弹窗里会直接给出输入框，粘贴密钥即可。

拿到的密钥只留在网关进程内存、不落盘不写日志，且都要先过 keyblob 自检才生效。提示权限不足时，以管理员身份启动网关再试一次。

---

## 五、客户端配置与接入

- **API 接口地址 (Base URL)**：`http://127.0.0.1:8788/v1`（局域网为 `http://<局域网IP>:8788/v1`）
- **API Key**：本机单机模式（未配置 Key 且未开 LAN）可留空或填任意字符；已在看板配置 Key 或 LAN 模式，请用看板「设置」页里绑好出口的 Key；
- **模型名称**：填 `/v1/models` 里列出的任意官方对齐模型 ID（如 `deepseek-v4.1-flash`、`gpt-6-astra`、`glm-5.3`）。

**Codex CLI / Claude Code（Responses API）**

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你在看板设置中添加并绑定的API_Key"
```

**Claude Code / Anthropic SDK（原生 Anthropic Messages API）**

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_API_KEY="你在看板设置中添加并绑定的API_Key"
```

网关原生实现 `/v1/messages`（流式与非流式）：`system`、`text` / `image` / `document` / `tool_use` / `tool_result` 内容块双向转换，`tools` + `tool_choice`、`stop_sequences`、`metadata.user_id`、`thinking` / `output_config.effort` 全部映射到上游；流式输出是原生事件序列；鉴权接受 `x-api-key` 或 `Authorization: Bearer`，错误一律用 Anthropic 的错误信封。

边界：服务端工具（`web_search` 等）上游不支持，会被丢弃并在 system 里注明，不会伪造调用；`thinking` / `redacted_thinking` 块不回放；`top_k`、`cache_control`、`context_management` 与 `betas` 忽略；`/v1/messages/count_tokens` 返回的是网关的 CJK 感知估算值，**不是**官方分词器的精确值。

---

## 六、看板与接口一览

访问 `http://127.0.0.1:8788/` 使用集成看板：顶部页签默认是「网关与账号 / 数据看板 / 智能体配置 / 设置 / 运行日志」，每个主页面左侧有一条按页面区块现场生成的导航（点击直达、滚动高亮、地址栏带锚点，可收起成窄轨）。

常用接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（容器 / 监控探针） |
| GET | `/v1/models` | 模型清单（OpenAI 兼容） |
| GET | `/u/<ref>/v1/models` | 定址账号的模型清单；`<ref>` 支持完整 uid / uid 前 8 位 / 昵称，过滤该账号禁用的模型 |
| POST | `/v1/chat/completions` | Chat Completions（流式 / 非流式） |
| POST | `/u/<ref>/v1/chat/completions` | 定址调用：锁定该账号，不参与会话亲和 / 轮换 / 自动换号；账号不可用直接报错 |
| POST | `/v1/responses` | Responses API（Codex 走这条） |
| POST | `/u/<ref>/v1/responses` | 定址 Responses API，语义同上 |
| POST | `/v1/messages` | 原生 Anthropic Messages |
| GET | `/accounts` | 账号列表与状态 |
| GET | `/usage/remaining` | 剩余用量估算（需面板会话） |
| GET | `/scheduler` | 定时调度器运行状态与排程日志 |
---

## 七、与上游基线的差异

本仓库由上游基线派生而来，但已按自有需求做了大改动，与上游不再同步：

1. **版本号重置**：从 `v0.1` 重新起算，作为自有发行线的起点；
2. **面板去掉「更新」板块**：删除 `wb_updates.py`、更新检查路由与设置项、后台检查线程，升级交给 `update-wb-proxy.ps1`；
3. **只发 Windows 便携包**：Release 流水线只出便携 zip + `SHA256SUMS`，删除 `docker-publish`、`release-checksums`、`revert-guard` 等工作流；
4. **Codex 一键接入**：新增 `wb_agents.py` 的 Codex 配置写入与桌面端模型目录生成，`context_window` 与 `/v1/models` 对齐；
5. **升级脚本适配两段版本号**：`update-wb-proxy.ps1` 支持 `0.1` 这类版本，本地号更高时按发布版本更新；
6. **低内存优化**：懒加载大模块、面板统计增量缓存、收敛后台线程；
7. **去除上游信息**：仓库内的镜像名、脚本地址、致谢链接、打包说明均改为指向本仓库或直接移除。

完整改动记录见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

---

## 八、免责声明 (Disclaimer)

1. 本项目为非官方自托管网关，仅供技术研究、逆向协议学习与个人合法授权账号在私有环境测试使用。
2. 本项目不提供任何账号及额度。请严格遵守官方服务条款，禁止用于任何商业转售、恶意并发或违规滥用。
3. 使用者需自行承担因使用本项目产生的一切风险与后果。
