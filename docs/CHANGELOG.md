# 版本更新记录

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
