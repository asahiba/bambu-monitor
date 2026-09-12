﻿# 把所有成品归集到一个交付目录，并生成校验清单。
#
# 用法（项目根目录）：
#     powershell -ExecutionPolicy Bypass -File make-bundle.ps1
#
# 产物：dist-all/
#   ├─ windows/BambuMonitor.exe           Windows 单文件（自带 Python 运行时）
#   ├─ linux/BambuMonitor-headless        Linux 单文件（自带 Python 运行时）
#   ├─ docker/bambu-monitor-image.tar.gz  Docker 单文件镜像（docker load 即用）
#   ├─ android/BambuMonitor-arm64.apk     安卓 APK（平板/手机独立运行）
#   ├─ README.md                          四种产物的用法与验证结论
#   └─ SHA256SUMS.txt                     校验清单
#
# 设计取舍：只归集**已构建成功**的产物，缺哪个就在报告里写明怎么构建，
# 而不是留下一个空目录让人猜。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$OutDir = Join-Path $Root "dist-all"
Write-Host "=== 归集成品到 $OutDir ===" -ForegroundColor Cyan

# 生成 README.md 要用到 Python（原因见下面「交付说明」处的注释）。
# 用项目自带的 .venv；没有就退回 PATH 上的 python。
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { $py = $found.Source }
}
if (-not (Test-Path $py)) {
    Write-Host "  警告：找不到 Python，README.md 将不会生成（产物与校验清单不受影响）" -ForegroundColor Yellow
}

# 允许重复执行：先清掉旧的分类目录（保留手写的 README 之外的自动内容）
foreach ($sub in @("windows", "linux", "docker", "android")) {
    $path = Join-Path $OutDir $sub
    if (Test-Path $path) { Remove-Item $path -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

# 产物定义：源路径 -> 交付目录下的文件名 -> 说明
$artifacts = @(
    @{
        Path = "dist-onefile\BambuMonitor.exe"
        Dest = "windows\BambuMonitor.exe"
        Label = "Windows 单文件（图形界面）"
    },
    @{
        # windowed 的 exe 没有控制台句柄，命令行输出全靠这个 console 版
        Path = "dist-onefile\BambuMonitor-cli.exe"
        Dest = "windows\BambuMonitor-cli.exe"
        Label = "Windows 单文件（命令行版）"
    },
    @{
        Path = "dist-onefile-headless\BambuMonitor-headless"
        Dest = "linux\BambuMonitor-headless"
        Label = "Linux 单文件（无界面服务版）"
    },
    @{
        Path = "dist-onefile-linux\BambuMonitor-linux"
        Dest = "linux\BambuMonitor-linux-gui"
        Label = "Linux 单文件（带界面版）"
    },
    @{
        Path = "dist-docker\bambu-monitor-latest-image.tar.gz"
        Dest = "docker\bambu-monitor-image.tar.gz"
        Label = "Docker 单文件镜像"
    },
    @{
        Path = "dist-android\BambuMonitor-1.0.1-arm64.apk"
        Dest = "android\BambuMonitor-arm64.apk"
        Label = "安卓 APK（arm64）"
    }
)

$copied = @()
$missing = @()
foreach ($item in $artifacts) {
    $source = Join-Path $Root $item.Path
    if (-not (Test-Path $source)) {
        $missing += $item
        continue
    }
    $dest = Join-Path $OutDir $item.Dest
    Copy-Item $source $dest -Force
    $sizeMb = [math]::Round((Get-Item $dest).Length / 1MB, 1)
    $copied += [pscustomobject]@{
        Label = $item.Label
        File = $item.Dest
        SizeMB = $sizeMb
    }
    Write-Host ("  ✓ {0,-28} {1,7} MB  {2}" -f $item.Label, $sizeMb, $item.Dest) -ForegroundColor Green
}

foreach ($item in $missing) {
    Write-Host ("  ✗ {0,-28} 未构建（{1}）" -f $item.Label, $item.Path) -ForegroundColor Yellow
}

# --- 校验清单 ---
# 刻意用「无 BOM 的 UTF-8」：SHA256SUMS 要能被 Linux 的 `sha256sum -c` 直接吃，
# 带 BOM 会让第一行的哈希前面多出 EF BB BF，校验必然失败。
$sumFile = Join-Path $OutDir "SHA256SUMS.txt"
$lines = @()
Get-ChildItem $OutDir -Recurse -File |
    Where-Object { $_.Name -ne "SHA256SUMS.txt" -and $_.Name -ne "README.md" } |
    Sort-Object FullName |
    ForEach-Object {
        $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
        $rel = $_.FullName.Substring($OutDir.Length + 1) -replace '\\', '/'
        $lines += "$hash  $rel"
    }
[System.IO.File]::WriteAllLines($sumFile, $lines, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "已生成校验清单：$sumFile（$($lines.Count) 个文件）" -ForegroundColor Cyan

# --- 交付说明 ---
# ⚠️ 刻意用 Python 生成而不是 PowerShell here-string：
# PowerShell 把反引号当转义字符，Markdown 的代码围栏（三个反引号）
# 在 here-string 里会被吃掉，产出没有代码块的 README（已踩过）。
$readme = Join-Path $OutDir "README.md"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$manifest = Join-Path $env:TEMP "bambu_bundle_manifest.json"
$copied | ConvertTo-Json -Depth 4 | Set-Content -Path $manifest -Encoding UTF8

if ($py -and (Test-Path $py)) {
    # README 交给独立脚本生成。刻意不在 PowerShell 里拼 Markdown：
    # PowerShell 把反引号当转义字符，代码围栏（三个反引号）会被吃掉，
    # 用 here-string 传 Python 代码也容易在引号/换行上出岔子。
    & $py (Join-Path $Root "tools\make_bundle_readme.py") $manifest $readme $stamp
} else {
    Write-Host "  跳过 README.md（没有可用的 Python）" -ForegroundColor Yellow
}
Remove-Item $manifest -Force -ErrorAction SilentlyContinue


# --- 汇总 ---
$totalMb = [math]::Round(((Get-ChildItem $OutDir -Recurse -File |
    Measure-Object -Property Length -Sum).Sum) / 1MB, 1)

Write-Host ""
Write-Host "=== 交付目录汇总 ===" -ForegroundColor Cyan
$copied | Format-Table -AutoSize
Write-Host "总计：$totalMb MB"

if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "以下产物尚未构建，构建命令：" -ForegroundColor Yellow
    Write-Host "  Windows : build-onefile.bat"
    Write-Host "  Linux   : bash linux/build-headless-docker.sh"
    Write-Host "  Docker  : powershell -ExecutionPolicy Bypass -File build-docker-image.ps1"
    Write-Host "  安卓    : powershell -ExecutionPolicy Bypass -File android\build-apk.ps1"
}
