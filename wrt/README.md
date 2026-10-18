# wrt/ — OpenWrt 打包配方

这里放 WorkBuddy2API-Agent 的 OpenWrt 安装包配方，**只保留打包与运行需要的
东西**。默认发布渠道只出 Windows 便携包（见 `.github/workflows/release.yml`），
本目录供需要时手动构建。

## 内容

```
wrt/
├── README.md                      # 本文件
└── openwrt/
    ├── build-ipk.sh               # 打包 .ipk（opkg 系，OpenWrt <= 24.10），不需要 SDK
    ├── build-apk.sh               # 打包 .apk（apk-tools 3，OpenWrt 25.x），需要 OpenWrt SDK
    └── workbuddy2api/
        ├── Makefile               # 包定义（PKG_VERSION / PKG_RELEASE / 依赖 / postinst ...）
        └── files/                 # init.d、uci 配置、面板缓存预热脚本
```

两种包的内容一致：`wb_*.py` + `dashboard.html`（纯 Python 标准库）+
`/etc/init.d/workbuddy2api` + `/etc/config/workbuddy2api` + `/usr/bin/workbuddy2api-warm`。

| 产物 | 适用系统 | 安装方式 |
| --- | --- | --- |
| `workbuddy2api_<版本>-<发布号>_all.ipk` | opkg 系（OpenWrt 24.10 及更早） | `opkg install <文件>` |
| `workbuddy2api-<版本>-r<发布号>.apk` | apk-tools 3（OpenWrt 25.x） | `apk add --allow-untrusted <文件>`（未签名时） |

依赖（两套系统同名）：`python3-light`、`python3-urllib`、`python3-uuid`、
`python3-openssl`、`python3-sqlite3`、`python3-codecs`。OpenWrt 为了省空间把
ssl、sqlite3 和完整编码表拆成了独立包，缺任何一个都会在启动或首次解析域名时
报 `ModuleNotFoundError`。

## 本地构建

```sh
# .ipk：任意 POSIX sh + python3 即可，不需要 SDK（脚本内嵌打包器并自检）
sh wrt/openwrt/build-ipk.sh --app-src "$PWD"      # 产物在 wrt/ipk/

# .apk：需要 OpenWrt 25.x 的 x86_64 SDK（glibc 宿主机）
sh wrt/openwrt/build-apk.sh <SDK 目录> "$PWD"     # 产物在 wrt/apk/
```

脚本不从网络拉源码：`--app-src` 不给时默认用当前仓库检出。

## 升级版本

只改一处：`wrt/openwrt/workbuddy2api/Makefile` 的 `PKG_VERSION`（需要时
`PKG_RELEASE`）。两个脚本都会把源码 `wb_proxy.py` 的 `server_version` 与
`PKG_VERSION` 比对，不一致就拒绝出包——宁可不发，也不发一个版本号对不上的包。

## 数据目录与持久化

- 程序文件在 `/usr/lib/workbuddy2api`，升级时整体替换；
- 账号凭据与用量在 `/etc/workbuddy2api/`（`postinst` 会把它加进
  `/etc/sysupgrade.conf`，系统升级不丢）；
- 配置在 `/etc/config/workbuddy2api`（`conffiles`，权限 600）；
- 首次安装不自动启用服务：`uci set workbuddy2api.main.panel_password='...'`、
  `uci commit`，再 `enable` + `start`。

`postinst` 只装一条 cron：`workbuddy2api-warm` 每 2 分钟预热面板的统计缓存
（`uci main.warm=0` 可关），服务每次启动后也会在后台先预热一轮。init.d 把
`WB_STATS_TTL` 默认调到 900 秒，配合预热器让面板点开即出数；这两个默认值是
配套的，改一个要一起看。
