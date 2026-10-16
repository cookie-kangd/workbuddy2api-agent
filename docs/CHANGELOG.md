# 版本更新记录

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
