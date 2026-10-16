# 版本更新记录

## v0.1.3 — Claude Desktop 配置留存修复

v0.1.2 里 Claude Desktop 能一键配置成功、但模型不进选择器的问题出在我们整文件覆写
profile：Claude 自己维护的 `preferences` 等键被冲掉，应用运行时会用内存态把文件写回
来，我们的 profile 就在这个来回里被丢掉。这一版改成合并写入，并补齐微软商店版的路径。

### 修复

- **网关 profile 改成「读-合并-写」**：只替换 CC Switch 定义的那几个 floor 键
  （`inferenceProvider` / `inferenceGatewayBaseUrl` / `inferenceGatewayApiKey` /
  `inferenceGatewayAuthScheme` / `inferenceModels` / `modelDiscoveryEnabled`）；
  `disableDeploymentModeChooser` / `coworkEgressAllowedHosts` 仅缺失时写入，用户手改过
  的不会被覆盖。
- **`claude_desktop_config.json` 只在已存在时补 `deploymentMode`**：不再新建空的 1P
  目录——那正是商店版会忽略的形态。
- **模型路由不再 404**：`claude-<角色>-<序号>` 是 Claude Desktop 内部的名字，映射到
  你在看板选的模型。选的模型不存在（切了出口、模型下线、或从没选过）时此前每个回合都
  404；现在先按网关当前真实清单校验，取第一个可用模型兜底（清单为空时保守保留配置，
  不瞎猜）。
- **兜底模型改为确定性选取**：原实现用 set 迭代，兜底模型每次可能不同。

### 兼容

- **Claude Desktop 同时支持微软商店版与 exe 版**：商店版（MSIX）多数情况下没有开启
  文件系统虚拟化，与 exe 版读同一个 `%LOCALAPPDATA%\Claude-3p`，天然共用一套路径；
  只有确实被虚拟化（本地目录完全不存在）时才回退到
  `Packages\AnthropicPBC.Claude_*\LocalCache` 沙盒内的对应目录。检测（probe）同样
  覆盖两种形态。

---


## v0.1.2 — 接入稳定性与客户端重启

### 新增

- **一键配置后自动重启客户端**：Codex 桌面版与 Claude Desktop 只在启动时读一次
  配置，改完不重启等于没改。看板点「一键配置」后，网关按**可执行文件路径**精确
  匹配、只重启正在运行的那一个客户端（不会误杀同名的其他程序）；没有运行则提示
  「下次打开生效」。重启结果会写进返回体，看板 toast 如实说明。
- **切换出口后自动重写并重启**：`POST /realm` 切换国际版 / 国内版时，会把此网关
  写入过、且没有被外部改动过的客户端配置按新出口的模型清单重写一遍，并重启正在
  运行的客户端。此前切换出口对已接入的客户端是「静默过期」——客户端里仍留着上个
  出口的模型清单。
- **Claude Desktop 一键接入**：新增 `claude-desktop` 客户端，写 CC Switch 同款的
  3P 网关 profile（`deploymentMode=3p` + `Claude-3p/configLibrary` 下的 profile 与
  `_meta.json`）。Claude Desktop 只接受 `claude-<角色>-<序号>` 这种模型名（写别的
  会让它整组拒收），所以 profile 里固定写 4 个角色路由，网关再把这些路由映射回
  你在看板里选的真实模型。
- **集成记录保存 key_id**（不保存密钥）：切换出口重写配置时能按原 key 重新解析，
  密钥仍然只写在客户端自己的配置文件里。

### 变更

- **单请求体上限 16MB → 32MB**：2026-10-16 上午直连 Codex 的真实回合实测
  16.90–16.92MB，只比 16MB 高一点点却整回合 413（客户端还用同一份 body 反复重试，
  一次对话刷出 7 个失败回合）。32MB 仍然拦住「几个 50MB 并发打穿小内存机器」的
  最坏情况。确需调整仍可用 `WB_MAX_PAYLOAD_BYTES`。

### 修复

- **413 提示不再指错方向**：网关自己的上限与上游的「请求体过大」共用同一条提示，
  上游拒绝时会误导运维去调 `WB_MAX_PAYLOAD_BYTES`。现在按网关自己的报文格式区分，
  上游的 413 改提示「精简对话/去掉大附件」。
- **一键配置 Claude 类客户端不再产出「必定 400」的配置**：Anthropic Messages 通道
  默认关闭（#367），而 Claude Code / Claude Desktop 只能走这条通道，此前一键配置
  写完就算成功，实际第一次请求就被网关 400 拒掉。现在给这类客户端配置时会自动打开
  该通道，并在返回里写明「可在设置页关闭」。
- **补齐 Claude Code 的模型槽位**：除 `ANTHROPIC_MODEL` 外，还写
  `ANTHROPIC_SMALL_FAST_MODEL`、`ANTHROPIC_DEFAULT_*`、`CLAUDE_CODE_SUBAGENT_MODEL`。
  不写这些时，摘要 / 压缩 / 子代理那几类回合会落到客户端内置默认模型名上，而这个
  网关并不提供那个名字。CC Switch 同样写全部槽位。
- **`model_catalog_json` 改绝对路径**：写相对路径时 Codex CLI 会按自己的当前目录
  解析，从非 `~/.codex` 目录启动就看不到第三方模型。CC Switch 也是写成绝对路径。
- **重启加了总开关**：`WB_AGENTS_NO_RESTART=1`（或 `disable_restarts()`）可彻底
 禁止杀进程。测试套件默认开启——此前一个没打桩的调用会真的杀掉并重启用户正在
 使用的 Codex / Claude Desktop。
## v0.1.1 — 同步上游 v1.7.6 – v1.7.10

本仓库仍是从 v0.1 起算的自有发行线。这一版把上游 `workbuddy2api-hub` 从
v1.7.5 到 v1.7.10 的改动整体并入，同时**保留本仓库此前的精简与自有改动**：
看板「更新检查」板块不回归、Release 只发 Windows 便携包、Codex 桌面程序
一键接入继续走 Bearer 直连。上游这一段的要点如下。

### 新增

- **系统设置两个可视化开关（默认关闭）**：一是「启用 Anthropic Messages
  接口 (Claude)」，知晓协议转换在长上下文下的积分风险后可一键开启，无需改
  环境变量并重启；二是「免费额度耗尽回退积分版」（#387），国际版
  `deepseek-v4.1-flash` 全池 6004 撞线后可回退到 `deepseek-v4.1-flash-sg`。
- **每个 API Key 独立并发限制**（#359）：编辑 Key 时可设最大并发数（0 或留空
  = 不限），卡片显示「并发上限 N」徽章，请求层实时计数，超限返回 429。
- **账号池一键连通性测试**（#375）与任务福利页「未活跃账号一键打卡」（#379）。
- **后台模型重定向（免费替代）**（#363）：桌面客户端内置路由偷偷把记忆生成 /
  环境分析 / 自动化等后台任务发往付费模型时，网关在选账号前原子改写为免费模型
  （`WB_BACKGROUND_MODEL_TARGET`，默认 `deepseek-v4.1-flash`）。

### 变更

- **一键配置补齐能力元数据**：Codex 桌面程序目录与 DSH 一键配置都会带上视觉
  识图（`supports_vision` / `input_modalities`）与思考档位
  （`reasoning_efforts`），模型窗口继续与 `/v1/models` 对齐（沿用本仓库取
  已解析真实上限的做法）。
- **每日活跃打卡防冷却回退**（#341）：首选免费模型额度用满（6004）或限流
  （429 冷却）时，自动顺延到同区域其它可用免费模型，避免当天活跃奖励丢失。
- **下游聚合器缓存命中规范对齐**（#370）：流式与非流式响应统一补齐并透传
  `prompt_tokens_details.cached_tokens`，New API / One API 能正确显示缓存命中。
- **熔断引入时间窗与本机断网识别**（#375）；**国际版身份头对齐 5.8.4 客户端**
  （#380、#382）解决 11-128 会话标识拦截；**国际版每日活跃网页通道失败自动
  重试**（#377，默认开启，次数与等待可调）。
- **安全默认**：`/v1/messages`（Anthropic Messages 转换层）在缓存一致性验证
  前默认禁用，需在设置里显式开启或设 `WB_ENABLE_ANTHROPIC_MESSAGES=1`。
- **修复**：流式 `finish_reason: error` 记账不实（#389）、413 请求体超限缺少
  `gateway_hint`（#372）、GPT 家族 `max_tokens < 16` 自动夹取（#364）、总览
  「当前禁用」区分配额耗尽与临时限流并给出真实起始时刻（#376）。

### 保留（不同步上游）

- 面板更新检查模块（`wb_updates.py`）、其设置项/路由/后台线程与相关测试仍不回归；
  升级继续用随包的 `update-wb-proxy.ps1` / `update-wb-proxy.bat`。
- Release 仍只发 Windows 便携包；`docker-publish` / `release-checksums` /
  `revert-guard` 等工作流不回归，Docker / OpenWrt 产物不构建。
- 上游仓库地址、镜像名与致谢相关说明保持本仓库口径。

## v0.1 — 首个版本

本仓库从 v0.1 重新起算版本号，作为自有发行线的起点。相比沿用过来的上游基线，
这一版做了以下调整：

### 新增

- **Codex 桌面程序（App）一键接入**：`wb_agents.py` 一键生成 Codex 的
  `config.toml` 模型目录与凭据，Bearer 令牌直连网关，**无需 CC Switch**；
  看板「智能体一键配置」页可对已安装的 Codex 桌面程序一键应用。
- **只发布 Windows 便携包**：Release 流水线只构建并上传 Windows 便携版
  `workbuddy2api-agent-<版本>.zip` 与 `SHA256SUMS`，不再构建 Docker / OpenWrt
  产物；`docker-publish`、`release-checksums`、`revert-guard` 等工作流移除。

### 变更

- **去除看板「更新」板块**：设置页不再显示更新检查开关与「立即检查更新」，
  版本升级改由随包提供的 `update-wb-proxy.ps1` / `update-wb-proxy.bat` 完成，
  减少后台检查线程与网络请求。
- **Codex 模型目录与 `/v1/models` 对齐**：一键配置写出的 `context_window`
  改为采用网关已解析的真实上限（可达 1M），不再取桌面端保守默认值（300k），
  避免 Codex 过早自动压缩上下文。
- **版本号两段式**：`server_version`、看板概览与升级脚本均支持 `0.1` 这类
  两段版本号。
- **升级脚本适配**：`update-wb-proxy.ps1` 支持两段版本号、本地版本高于发布
  版本时按发布版本更新，文案改为「发布方 / 新包」口径。

### 移除

- 面板更新检查模块（`wb_updates.py`）及其设置项、路由、测试。
- 上游仓库相关的镜像名、脚本地址、致谢链接与打包说明。

### 文档

- 根目录 `README.md` 重写为面向本仓库的说明，突出低内存占用、Windows 便携包
  与 Codex 一键接入。
