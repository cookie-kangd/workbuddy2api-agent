<#
  update-wb-proxy.ps1  —  WorkBuddy2API-Agent 免安装版（绿色包）更新脚本

  更新策略（先清空，再安装）：
    1. 读当前目录 wb_proxy.py 里的版本号；
    2. 查最新版本（国内镜像优先，GitHub 直连兜底）；
    3. 去发布页读取**真实附件名**（不写死文件名，上游改名也不会失效）；
       最新版还没有便携 zip 时，自动回退到上一个有 zip 的版本；
    4. 已是最新 -> 提示后停住；有新版本 -> 显示新旧版本号，等输入 Y；
    5. 输入 Y 后：下载 -> 校验 SHA256 -> 解压 ->
       **清空目录里除 accounts\、usage\ 和本更新脚本以外的所有内容** ->
       再把新包文件复制进去。上游增删文件不会留下残留。

  无论怎么清空，下面这些永远不动：
    accounts\              账号、API Key、面板密码、设置、智能体配置备份
    usage\                 用量日志、聚合缓存、定价覆盖
    update-wb-proxy.ps1    本脚本（否则更新后自己也没了）
    update-wb-proxy.bat    配套的双击启动器

  用法（推荐直接双击 update-wb-proxy.bat）：
    powershell -ExecutionPolicy Bypass -File .\update-wb-proxy.ps1

  可选参数：
    -Target <目录>           要更新的目录（默认：脚本所在目录）
    -ZipPath <zip>           用本地 zip 更新，完全不联网（离线用）
    -Source auto|github|mirror
                             auto（默认）：国内镜像优先，直连兜底
                             github      ：只走 GitHub 直连
                             mirror      ：只用国内镜像
    -Backup                  更新前把 accounts\ 和 usage\ 打包备份
    -Diagnose                只做网络/环境体检，不改任何东西
    -DryRun                  只检测版本，不做任何改动
    -Yes                     跳过 Y 确认（自动继续）
    -NoPause                 结束后不等待按键（给批处理/自动化用）

  镜像说明：国内镜像（ghfast.top / ghproxy.net / gh-proxy.com 等）是第三方
  公益转发服务。脚本会校验官方 checksums.txt / SHA256SUMS 的 SHA256，能挡住
  传输损坏；镜像本身若被篡改则无法防范，介意请用 -Source github。
#>

[CmdletBinding()]
param(
    [string]$Target = "",
    [string]$ZipPath = "",
    [ValidateSet("auto", "github", "mirror")]
    [string]$Source = "auto",
    [switch]$Backup,
    [switch]$Diagnose,
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # 关掉进度条，PS 5.1 下载能快很多

# 老版 Windows PowerShell 默认可能不带 TLS 1.2，GitHub / 镜像会直接握手失败
try {
    $tls = [System.Net.SecurityProtocolType]::Tls12
    if ([enum]::GetNames([System.Net.SecurityProtocolType]) -contains "Tls13") {
        $tls = $tls -bor [System.Net.SecurityProtocolType]::Tls13
    }
    [System.Net.ServicePointManager]::SecurityProtocol = $tls
} catch { }

# ---------------------------------------------------------------- 配置
$Repo      = "cookie-kangd/workbuddy2api-agent"
$KeepDirs  = @("accounts", "usage")            # 数据目录：清空与复制都跳过
$KeepFiles = @("update-wb-proxy.ps1", "update-wb-proxy.bat")   # 本更新脚本：永不删除
$UA        = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) wb-proxy-updater"

$DirectApi  = "https://api.github.com/repos/$Repo/releases/latest"
$DirectRel  = "https://github.com/$Repo/releases/latest"
$DirectBase = "https://github.com/$Repo/releases/download"
$RawSource  = "https://raw.githubusercontent.com/$Repo/main/wb_proxy.py"

# 国内加速节点，按实测速度排序；直连永远排在最后兜底
$Mirrors = @(
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://gh.llkk.cc/",
    "https://mirror.ghproxy.com/"
)

# ---------------------------------------------------------------- 输出
function Say-Step($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Say-Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Say-Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Say-Err($m)  { Write-Host "[x] $m" -ForegroundColor Red }

function Pause-End {
    if ($NoPause) { return }
    Write-Host ""
    Write-Host "按回车键关闭窗口..." -ForegroundColor DarkGray
    [void](Read-Host)
}

function Stop-Now([int]$code) { Pause-End; exit $code }

# ---------------------------------------------------------------- 版本
function Get-LocalVersion([string]$dir) {
    $py = Join-Path $dir "wb_proxy.py"
    if (-not (Test-Path -LiteralPath $py)) { return $null }
    $text = Get-Content -LiteralPath $py -Raw -Encoding UTF8
    $m = [regex]::Match($text, 'server_version\s*=\s*"wb-proxy/(\d+\.\d+\.\d+)"')
    if (-not $m.Success) { $m = [regex]::Match($text, '"version"\s*:\s*"(\d+\.\d+\.\d+)"') }
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

function Get-RunningGateway {
    try {
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and ($_.CommandLine -like "*wb_proxy.py*") }
    } catch { @() }
}

# ---------------------------------------------------------------- 来源
# 镜像在前、直连在后：国内环境下直连常常慢到超时，先试它只会白白等待
function Get-Sources([string]$mode) {
    $list = @()
    foreach ($m in $Mirrors) { $list += @{ base = $m; label = "$m 镜像"; small = 20; big = 900 } }
    if ($mode -ne "mirror") { $list += @{ base = ""; label = "GitHub 直连"; small = 15; big = 120 } }
    if ($mode -eq "github") { $list = @($list | Where-Object { $_.base -eq "" }) }
    return $list
}

function Get-TagFromHtml([string]$url, [int]$timeout) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $url -Headers @{ "User-Agent" = $UA } -TimeoutSec $timeout
        $c = [string]$r.Content
        # 优先认页面"自身"的版本：canonical / og:url / title，比扫页面链接准
        foreach ($pat in @('<link rel="canonical" href="[^"]*/releases/tag/(v?\d+\.\d+\.\d+)"',
                           '<meta property="og:url" content="[^"]*/releases/tag/(v?\d+\.\d+\.\d+)"',
                           '<title>[^<]*?Release\s+(v?\d+\.\d+\.\d+)')) {
            $m = [regex]::Match($c, $pat)
            if ($m.Success) { return $m.Groups[1].Value }
        }
        $all = [regex]::Matches($c, '/releases/tag/(v?\d+\.\d+\.\d+)') | ForEach-Object { $_.Groups[1].Value }
        if ($all.Count -gt 0) { return $all[0] }
    } catch { }
    return $null
}

function Get-TagFromRaw([string]$url, [int]$timeout) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $url -Headers @{ "User-Agent" = $UA } -TimeoutSec $timeout
        $m = [regex]::Match([string]$r.Content, 'server_version\s*=\s*"wb-proxy/(\d+\.\d+\.\d+)"')
        if ($m.Success) { return "v" + $m.Groups[1].Value }
    } catch { }
    return $null
}

function Get-TagFromApi([int]$timeout) {
    try {
        $r = Invoke-RestMethod -Uri $DirectApi -TimeoutSec $timeout `
             -Headers @{ "User-Agent" = $UA; "Accept" = "application/vnd.github+json" }
        if ($r.tag_name) { return [string]$r.tag_name }
    } catch { }
    return $null
}

function Find-LatestTag([string]$mode) {
    $sources = Get-Sources $mode
    # 每个来源先试页面、再试源码，这样一个来源就能定下来，不用全扫一遍
    foreach ($s in $sources) {
        Say-Step "检查版本：$($s.label)"
        $t = Get-TagFromHtml ($s.base + $DirectRel) $s.small
        if ($t) { return @{ tag = $t; source = $s.label } }
        $t = Get-TagFromRaw ($s.base + $RawSource) $s.small
        if ($t) { return @{ tag = $t; source = "$($s.label) 源码" } }
    }
    # 兜底：GitHub API（国内常被限流 403，放最后最省时间）
    if ($mode -ne "mirror") {
        Say-Step "检查版本：GitHub API 直连"
        $t = Get-TagFromApi 8
        if ($t) { return @{ tag = $t; source = "GitHub API 直连" } }
    }
    return $null
}

# ---------------------------------------------------------------- 附件
$AssetCache = @{}

function Get-ReleaseAssets([string]$base, [string]$tag, [int]$timeout) {
    $key = "$base|$tag"
    if ($AssetCache.ContainsKey($key)) { return $AssetCache[$key] }
    $names = @()
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri ($base + "https://github.com/$Repo/releases/expanded_assets/$tag") `
             -Headers @{ "User-Agent" = $UA } -TimeoutSec $timeout
        $pat = "/releases/download/" + [regex]::Escape($tag) + "/([^`"?]+)"
        $names = @([regex]::Matches([string]$r.Content, $pat) | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique)
    } catch { }
    $AssetCache[$key] = $names
    return $names
}

function Pick-ZipAsset($names) {
    $zips = @($names | Where-Object { $_ -like "*.zip" })
    if ($zips.Count -eq 0) { return $null }
    # 新版叫 workbuddy2api-agent-vX.zip，旧版叫 wb-proxy-vX.zip，都要认
    $p = @($zips | Where-Object { $_ -match 'agent.*\.zip$' })       | Select-Object -First 1
    if (-not $p) { $p = @($zips | Where-Object { $_ -match 'wb-proxy.*\.zip$' }) | Select-Object -First 1 }
    if (-not $p) { $p = $zips | Select-Object -First 1 }
    return $p
}

function Get-CandidateTags([string]$latest, [int]$count) {
    $out = @($latest)
    $m = [regex]::Match($latest, '^v?(\d+)\.(\d+)\.(\d+)$')
    if (-not $m.Success) { return $out }
    $maj = [int]$m.Groups[1].Value; $min = [int]$m.Groups[2].Value; $patch = [int]$m.Groups[3].Value
    for ($i = 1; $i -lt $count; $i++) {
        $n = $patch - $i
        if ($n -lt 0) { break }
        $out += "v$maj.$min.$n"
    }
    return $out
}

function Test-AssetExists([string]$base, [string]$tag, [string]$name, [int]$timeout) {
    try {
        $req = [System.Net.HttpWebRequest]::Create("$base$DirectBase/$tag/$name")
        $req.UserAgent = $UA
        $req.AddRange(0, 0)
        $req.Timeout = $timeout * 1000
        $req.ReadWriteTimeout = $timeout * 1000
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return ($code -ge 200 -and $code -lt 300)
    } catch { return $false }
}

function Resolve-Release([string]$latestTag, [string]$mode) {
    $sources = Get-Sources $mode
    foreach ($tag in (Get-CandidateTags $latestTag 8)) {
        foreach ($s in $sources) {
            $zip = Pick-ZipAsset (Get-ReleaseAssets $s.base $tag $s.small)
            if ($zip) { return @{ tag = $tag; asset = $zip; base = $s.base; label = $s.label; sources = $sources } }
        }
        # 发布页取不到附件列表时，退一步猜常见文件名
        foreach ($s in $sources) {
            foreach ($g in @("workbuddy2api-agent-$tag.zip", "wb-proxy-$tag.zip")) {
                if (Test-AssetExists $s.base $tag $g $s.small) {
                    return @{ tag = $tag; asset = $g; base = $s.base; label = $s.label; sources = $sources }
                }
            }
        }
    }
    return $null
}

function Download-File([string]$url, [string]$outPath, [int]$timeout) {
    try {
        if (Test-Path -LiteralPath $outPath) { Remove-Item -LiteralPath $outPath -Force }
        Invoke-WebRequest -UseBasicParsing -Uri $url -Headers @{ "User-Agent" = $UA } -OutFile $outPath -TimeoutSec $timeout
        return (Test-Path -LiteralPath $outPath) -and ((Get-Item -LiteralPath $outPath).Length -gt 0)
    } catch {
        if (Test-Path -LiteralPath $outPath) { Remove-Item -LiteralPath $outPath -Force -ErrorAction SilentlyContinue }
        return $false
    }
}

function Get-ExpectedHash([string]$tag, [string]$assetName, [string]$firstBase, $sources, [string]$tmpDir) {
    # 先用下载成功的那个来源取，失败再试其它来源
    $order = @($firstBase) + @($sources | ForEach-Object { $_.base } | Where-Object { $_ -ne $firstBase } | Select-Object -Unique)
    foreach ($b in $order) {
        foreach ($csName in @("checksums.txt", "SHA256SUMS")) {
            $tmp = Join-Path $tmpDir $csName
            if (-not (Download-File ("$b$DirectBase/$tag/$csName") $tmp 60)) { continue }
            $text = Get-Content -LiteralPath $tmp -Raw -ErrorAction SilentlyContinue
            if (-not $text) { continue }
            $hit = $null
            foreach ($line in ($text -split "`r?`n")) {
                $parts = $line -split '\s+', 2
                if ($parts.Count -ne 2) { continue }
                $name = $parts[1].Trim()
                if ($name -eq $assetName) { $hit = $parts[0].Trim().ToLower(); break }
                # 有些 checksums.txt 用星号前缀（二进制模式）
                if (-not $hit -and $name -like "*.zip" -and ($name.Trim('*') -eq $assetName)) { $hit = $parts[0].Trim().ToLower() }
            }
            if ($hit) { return $hit }
        }
    }
    return $null
}

# ---------------------------------------------------------------- 清空 / 安装
function Test-Protected([string]$name) {
    if ($KeepDirs  -contains $name) { return $true }
    if ($KeepFiles -contains $name) { return $true }
    if ($name -like "update-wb-proxy.*") { return $true }   # 兼容改名后的启动器
    return $false
}

function Backup-DataDirs([string]$dir, [string]$label) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $dest  = Join-Path (Split-Path -Parent $dir) ("wb-proxy-数据备份-$stamp.zip")
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $tmp = Join-Path $env:TEMP ("wb-backup-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    try {
        foreach ($d in $KeepDirs) {
            $src = Join-Path $dir $d
            if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $tmp $d) -Recurse -Force }
        }
        if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Force }
        [System.IO.Compression.ZipFile]::CreateFromDirectory($tmp, $dest)
        Say-Ok "数据已备份：$dest"
    } catch {
        Say-Warn "备份失败（不影响更新）：$($_.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Clear-ProgramFiles([string]$dir) {
    # 删掉顶层里除保护名单之外的一切；目录递归删，文件直接删
    $removed = 0
    $failed  = @()
    foreach ($item in (Get-ChildItem -LiteralPath $dir -Force)) {
        if (Test-Protected $item.Name) { continue }
        try {
            Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
            $removed++
        } catch {
            # 只读或被占用时，先清掉只读属性再试一次
            try {
                Get-ChildItem -LiteralPath $item.FullName -Recurse -Force -ErrorAction SilentlyContinue |
                    ForEach-Object { try { $_.Attributes = "Normal" } catch { } }
                Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
                $removed++
            } catch {
                $failed += $item.Name
            }
        }
    }
    return @{ removed = $removed; failed = $failed }
}

function Install-Payload([string]$payload, [string]$dest) {
    # 复制新包内容；accounts\ 与 usage\ 一律不碰（保留用户数据）
    $copied = 0
    foreach ($item in (Get-ChildItem -LiteralPath $payload -Force)) {
        if ($KeepDirs -contains $item.Name) { continue }
        Copy-Item -LiteralPath $item.FullName -Destination $dest -Recurse -Force
        $copied++
    }
    return $copied
}

# ---------------------------------------------------------------- 体检
function Test-Http([string]$label, [string]$url, [int]$timeout) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $url -Headers @{ "User-Agent" = $UA } -TimeoutSec $timeout
        Say-Ok ("{0,-38} OK   HTTP {1}  {2} bytes" -f $label, $r.StatusCode, $r.RawContentLength)
        return $true
    } catch {
        $code = ""
        if ($_.Exception.Response) { $code = "HTTP $([int]$_.Exception.Response.StatusCode) " }
        Say-Err ("{0,-38} FAIL {1}{2}" -f $label, $code, $_.Exception.Message.Split("`n")[0])
        return $false
    }
}

function Test-Range([string]$label, [string]$url, [int]$timeout) {
    try {
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.UserAgent = $UA
        $req.AddRange(0, 65535)
        $req.Timeout = $timeout * 1000
        $req.ReadWriteTimeout = $timeout * 1000
        $resp = $req.GetResponse()
        $ms = New-Object System.IO.MemoryStream
        $resp.GetResponseStream().CopyTo($ms)
        $resp.Close()
        Say-Ok ("{0,-38} OK   {1} bytes" -f $label, $ms.Length)
        return $true
    } catch {
        Say-Err ("{0,-38} FAIL {1}" -f $label, $_.Exception.Message.Split("`n")[0])
        return $false
    }
}

function Invoke-Diagnostics([string]$mode) {
    Write-Host ""
    Say-Step "环境"
    Write-Host ("  PowerShell       : " + $PSVersionTable.PSVersion)
    Write-Host ("  .NET CLR         : " + $PSVersionTable.CLRVersion)
    Write-Host ("  OS               : " + [System.Environment]::OSVersion.VersionString)
    Write-Host ("  SecurityProtocol : " + [System.Net.ServicePointManager]::SecurityProtocol)
    $k = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    Write-Host ("  WinINET 代理     : enable=$($k.ProxyEnable) server=$($k.ProxyServer) pac=$($k.AutoConfigURL)")
    try {
        $sp = [System.Net.WebRequest]::GetSystemWebProxy()
        Write-Host ("  .NET 看到的代理  : " + $sp.GetProxy('https://github.com/'))
    } catch { }

    Write-Host ""
    Say-Step "连通性（每个请求最多 15 秒）"
    foreach ($s in (Get-Sources $mode)) { Test-Http $s.label ($s.base + $DirectRel) 15 | Out-Null }

    Write-Host ""
    Say-Step "解析最新版本与附件名"
    $found = Find-LatestTag $mode
    if (-not $found) { Say-Err "版本探测失败。"; return }
    Say-Ok "最新 tag：$($found.tag)（来源 $($found.source)）"
    $rel = Resolve-Release $found.tag $mode
    if (-not $rel) { Say-Err "所有来源都没找到可下载的 zip 附件。"; return }
    Say-Ok "将使用：$($rel.tag) / $($rel.asset)（来源 $($rel.label)）"

    Write-Host ""
    Say-Step "下载连通性（每个来源取前 64KB）"
    $url = "$DirectBase/$($rel.tag)/$($rel.asset)"
    foreach ($s in $rel.sources) { Test-Range "$($s.label) 下载" ($s.base + $url) 15 | Out-Null }
}

# ================================================================ 主流程
Write-Host ""
Write-Host "==============================================" -ForegroundColor Blue
Write-Host "  WorkBuddy2API-Agent 免安装版 更新脚本" -ForegroundColor Blue
Write-Host "==============================================" -ForegroundColor Blue
Write-Host ""

if ([string]::IsNullOrWhiteSpace($Target)) { $Target = $PSScriptRoot }
if ([string]::IsNullOrWhiteSpace($Target)) { $Target = (Get-Location).Path }
$Target = (Resolve-Path -LiteralPath $Target).Path

if (-not (Test-Path -LiteralPath (Join-Path $Target "wb_proxy.py"))) {
    Say-Err "在这个目录里没找到 wb_proxy.py：$Target"
    Say-Err "请把脚本放到解压出来的 wb-proxy 文件夹里，或用 -Target 指定目录。"
    Stop-Now 1
}

$local = Get-LocalVersion $Target
Say-Step "目录      ：$Target"
if ($local) { Say-Step "当前版本  ：$local" }
else        { Say-Warn "没能从 wb_proxy.py 里读出当前版本号。" }

$running = @(Get-RunningGateway)
if ($running.Count -gt 0) {
    Say-Warn "检测到网关正在运行（进程号：$($running.ProcessId -join ', ')）。"
    Say-Warn "更新前请先关掉那个运行窗口，否则文件可能被占用、更新失败。"
}

if ($Diagnose) { Invoke-Diagnostics $Source; Stop-Now 0 }

# --- 确定装哪个版本 -------------------------------------------------------
$rel      = $null
$zipFile  = $null
$localZip = $false

if ($ZipPath) {
    if (-not (Test-Path -LiteralPath $ZipPath)) { Say-Err "找不到 zip 文件：$ZipPath"; Stop-Now 1 }
    $ZipPath  = (Resolve-Path -LiteralPath $ZipPath).Path
    $zipFile  = $ZipPath
    $localZip = $true
    Say-Step "使用本地包：$ZipPath"
} else {
    Write-Host ""
    $found = Find-LatestTag $Source
    if (-not $found) {
        Write-Host ""
        Say-Warn "所有来源都没查到最新版本（网络问题？）。"
        Say-Warn "可以先跑 -Diagnose 看具体哪一步失败。"
        if ($DryRun) { Stop-Now 1 }
        $manual = Read-Host "也可手动输入要更新的版本号（例如 1.6.20），留空则退出"
        if ([string]::IsNullOrWhiteSpace($manual)) { Say-Warn "已退出，没有做任何改动。"; Stop-Now 0 }
        $found = @{ tag = $manual.Trim(); source = "手动输入" }
    }
    if ($found.tag -notmatch '^v') { $found.tag = "v" + $found.tag }

    Write-Host ""
    Say-Step "最新发布  ：$($found.tag)"
    Say-Step "来源      ：$($found.source)"
    Say-Step "正在确认该版本的下载附件..."
    $rel = Resolve-Release $found.tag $Source
    if (-not $rel) {
        Write-Host ""
        Say-Err "找到了版本 $($found.tag)，但它没有可下载的 zip 附件（可能还在上传）。"
        Say-Warn "可以用 -ZipPath 指定手动下载的 zip，或稍后重试。"
        Stop-Now 1
    }
    if ($rel.tag -ne $found.tag) { Say-Warn "注意：$($found.tag) 还没有便携 zip，已自动回退到 $($rel.tag)。" }
    Say-Ok "将安装    ：$($rel.tag) / $($rel.asset)"

    Write-Host ""
    if ($local) {
        try {
            $lv = [version]$local
            $rv = [version]($rel.tag -replace '^v', '')
            if ($rv -le $lv) {
                Say-Ok "已经是最新版本，无需更新。"
                Say-Step "当前版本 $local  /  可更新到 $($rel.tag -replace '^v','')"
                Stop-Now 0
            } else {
                Say-Warn "发现新版本：$local  ->  $($rel.tag -replace '^v','')"
            }
        } catch {
            Say-Warn "版本号无法比较，按有新版本处理。"
        }
    } else {
        Say-Warn "读不到当前版本，按有新版本处理。"
    }
}

if ($DryRun) {
    Write-Host ""
    Say-Ok "仅检测模式：没有做任何改动。"
    Stop-Now 0
}

# --- 确认 -----------------------------------------------------------------
Write-Host ""
Write-Host "将要更新目录：" -ForegroundColor White
Write-Host "  $Target"
Write-Host "更新方式：先清空该目录下的程序文件，再放入新版文件" -ForegroundColor White
Write-Host ""
Write-Host "以下内容不会被删除：" -ForegroundColor Green
Write-Host "  accounts\              账号、API Key、面板密码、设置" -ForegroundColor Green
Write-Host "  usage\                 用量日志、定价覆盖" -ForegroundColor Green
Write-Host "  update-wb-proxy.ps1    更新脚本本体" -ForegroundColor Green
Write-Host "  update-wb-proxy.bat    更新脚本启动器" -ForegroundColor Green
Write-Host ""
Write-Host "该目录下其它所有文件都会先被删除，再换成新版。" -ForegroundColor Yellow
Write-Host ""

if (-not $Yes) {
    $ans = Read-Host "确认现在更新请输入 Y 再回车（其它任意键取消）"
    if ($ans.Trim() -notmatch '^(y|yes)$') {
        Write-Host ""
        Say-Warn "已取消，没有做任何改动。"
        Stop-Now 0
    }
}

# --- 下载 + 校验 + 解压（这一步全部完成，才会动目录） --------------------
$tmpRoot = Join-Path $env:TEMP ("wb-proxy-update-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null

try {
    if (-not $localZip) {
        $url     = "$DirectBase/$($rel.tag)/$($rel.asset)"
        $zipFile = Join-Path $tmpRoot "release.zip"

        Write-Host ""
        $ok = $false
        foreach ($s in $rel.sources) {
            Say-Step "下载中（$($s.label)）..."
            if (Download-File ($s.base + $url) $zipFile $s.big) {
                Say-Ok "下载完成（$([math]::Round((Get-Item -LiteralPath $zipFile).Length / 1MB, 2)) MB，来自 $($s.label)）。"
                $rel.base = $s.base; $rel.label = $s.label; $ok = $true; break
            }
            Say-Warn "$($s.label) 下载失败，换下一个来源。"
        }
        if (-not $ok) {
            Say-Err "所有下载来源都失败了。目录未做任何改动。"
            Say-Warn "可以手动下载 $($rel.asset)，然后用 -ZipPath 指定本地文件更新。"
            Say-Warn "也可跑 -Diagnose 查看每个来源的具体报错。"
            Stop-Now 1
        }

        $expected = Get-ExpectedHash $rel.tag $rel.asset $rel.base $rel.sources $tmpRoot
        if ($expected) {
            $actual = (Get-FileHash -LiteralPath $zipFile -Algorithm SHA256).Hash.ToLower()
            if ($actual -eq $expected) { Say-Ok "SHA256 校验通过。" }
            else { Say-Err "SHA256 不匹配，已中止（期望 $expected，实际 $actual）。目录未做任何改动。"; Stop-Now 1 }
        } else {
            Say-Warn "没取到该文件的官方校验值，跳过校验。"
        }
    }

    Write-Host ""
    Say-Step "正在解压..."
    $extract = Join-Path $tmpRoot "extract"
    Expand-Archive -LiteralPath $zipFile -DestinationPath $extract -Force

    # 发布包外层是 wb-proxy 文件夹；也兼容直接放在根目录或改过目录名的包
    $payload = Join-Path $extract "wb-proxy"
    if (-not (Test-Path -LiteralPath (Join-Path $payload "wb_proxy.py"))) {
        if (Test-Path -LiteralPath (Join-Path $extract "wb_proxy.py")) {
            $payload = $extract
        } else {
            $cand = Get-ChildItem -LiteralPath $extract -Directory |
                Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "wb_proxy.py") } |
                Select-Object -First 1
            if ($cand) { $payload = $cand.FullName }
            else { Say-Err "解压出来的包里找不到 wb_proxy.py。目录未做任何改动。"; Stop-Now 1 }
        }
    }
    $newVer = Get-LocalVersion $payload
    if (-not $newVer) {
        # 包本身不完整，绝不能在这种情况下清空目录
        Say-Err "新包里读不出版本号，可能不完整。已中止，目录未做任何改动。"
        Stop-Now 1
    }
    Say-Ok "待安装版本：$newVer"

    if ($Backup) { Write-Host ""; Backup-DataDirs $Target $newVer }

    # --- 先清空，再安装 ---------------------------------------------------
    Write-Host ""
    Say-Step "正在清空旧的程序文件..."
    $clear = Clear-ProgramFiles $Target
    Say-Ok "已清除 $($clear.removed) 个顶层项目。"
    if ($clear.failed.Count -gt 0) {
        Say-Warn "有 $($clear.failed.Count) 项删不掉（多半是被占用）：$($clear.failed -join ', ')"
        Say-Warn "若有旧网关窗口在跑，关掉后重新运行本脚本再更新一次。"
    }

    Say-Step "正在放入新版文件..."
    $copied = Install-Payload $payload $Target
    # 新包在 zip 根目录还带一份 release-manifest.json，一并放进安装目录
    $manifest = Join-Path $extract "release-manifest.json"
    if (Test-Path -LiteralPath $manifest) {
        Copy-Item -LiteralPath $manifest -Destination (Join-Path $Target "release-manifest.json") -Force
    }
    Say-Ok "已写入 $copied 个顶层项目。"

    # --- 收尾检查 ---------------------------------------------------------
    $sanity = @("wb_proxy.py", "dashboard.html", "start-wb-proxy.bat")
    $missed = @($sanity | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Target $_)) })
    foreach ($f in $missed) { Say-Warn "注意：$f 在新包里不存在（上游可能已改名或移除）。" }

    $afterVer = Get-LocalVersion $Target
    $keepOk = @($KeepFiles | Where-Object { Test-Path -LiteralPath (Join-Path $Target $_) })

    Write-Host ""
    if ($afterVer -and $local -and ([version]$afterVer -gt [version]$local)) {
        Say-Ok "更新完成：$local  ->  $afterVer"
    } else {
        Say-Ok "更新完成：$newVer"
    }
    Say-Step "accounts\、usage\ 与更新脚本本身均原样保留。"
    if ($keepOk.Count -gt 0) { Say-Step "更新脚本仍在：$($keepOk -join ', ')" }
    if ($clear.failed.Count -eq 0 -and $missed.Count -eq 0) {
        Say-Step "现在双击本目录里的 start-wb-proxy.bat 启动新版本即可。"
    }
    if ($running.Count -gt 0) {
        Say-Warn "如果刚才有旧窗口还在跑，记得先把它关掉再启动。"
    }
    Stop-Now 0
}
catch {
    Write-Host ""
    Say-Err "出错了：$($_.Exception.Message)"
    Say-Warn "如果网关正在运行，请先关掉它再重试。"
    Stop-Now 1
}
finally {
    Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
}
