# 把所有成品归集到一个交付目录，并生成校验清单。
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
        Label = "Windows 单文件"
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
        Path = "dist-android\BambuMonitor-1.0.0-arm64.apk"
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
$readme = Join-Path $OutDir "README.md"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$rows = ($copied | ForEach-Object {
    "| $($_.Label) | ``$($_.File)`` | $($_.SizeMB) MB |"
}) -join "`r`n"

$readmeText = @"
# 拓竹打印机监控台 · 交付包

构建时间：$stamp

## 包含的产物

| 版本 | 文件 | 大小 |
|---|---|---|
$rows

## 各版本怎么用

### Windows（``windows/BambuMonitor.exe``）
双击即可运行，已自带 Python 运行时，**不需要**装 Python。
首次运行会生成配置目录（``%APPDATA%\BambuMonitor``）。
杀毒软件可能对未签名的一次性打包 exe 报警，属正常现象。

### Linux（``linux/BambuMonitor-headless``，无界面服务版）
```
chmod +x BambuMonitor-headless
./BambuMonitor-headless --host 0.0.0.0 --port 8080
```
然后浏览器打开 ``http://<这台机器的IP>:8080``。
带界面的版本是 ``linux/BambuMonitor-linux-gui``（需要图形环境与 Qt 依赖库）。

### Docker（``docker/bambu-monitor-image.tar.gz``）
```
docker load -i bambu-monitor-image.tar.gz
docker run -d --name bambu-monitor --network host \
  -v bambu-config:/config bambu-monitor:latest
```
``--network host`` 是必须的：打印机的发现（SSDP 广播）与视频流都要求容器
能直接看到局域网。

### 安卓（``android/BambuMonitor-arm64.apk``）
把 APK 传到平板/手机上安装（需要允许"未知来源"）。
应用**内置了完整的 Python 服务**，在设备本机跑 HTTP 服务，界面用 WebView 打开，
所以平板可以完全脱离电脑独立使用；因为服务绑在 ``0.0.0.0``，
同一 Wi-Fi 下的其它手机/电脑也能访问这台平板的页面。

* 架构：arm64-v8a（当前绝大多数平板与手机）
* 系统：Android 7.0+（API 24）
* 包内已含 OpenCV，支持 X1 / X2D / H2 / P2S 的 RTSPS 高清通道
* 这是 **debug 签名**的包，用于自用安装；上架应用商店需要换正式签名

## 全部版本共用的功能

四个版本共用同一套 Python 内核（同一个 ``app/`` 目录），因此功能一致：
设备管理（自动发现 / 手动添加 / 改名 / 删除）、暂停 / 继续 / 停止、
腔体灯开关、打印速度、摄像头画面与帧率、HMS 错误码中文解释、
多台打印机并排监控。**网页端是各平台统一的操作界面。**

## 校验

``SHA256SUMS.txt``（无 BOM 的 UTF-8，可直接给 Linux 用）：
```
# Linux / macOS
sha256sum -c SHA256SUMS.txt
# Windows PowerShell
Get-FileHash .\android\BambuMonitor-arm64.apk -Algorithm SHA256
```
"@
[System.IO.File]::WriteAllText($readme, $readmeText, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "已生成说明文件：$readme" -ForegroundColor Cyan

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
