# 把 Python 源码同步进安卓工程（Chaquopy 需要源码在 app/src/main/python 下）。
#
# 用法（在 android/ 目录下）：
#     powershell -ExecutionPolicy Bypass -File sync-python.ps1
#
# 为什么用同步而不是让 Gradle 直接引用 ../app：
#   * Chaquopy 只认自己 sourceSets 里的 python 目录，跨目录引用会牵扯一堆路径问题；
#   * 同步后安卓侧的文件结构一目了然，排查"打进包里的到底是哪份代码"很容易。
# 单一事实来源仍是仓库根的 app/ —— 这里只是构建期复制，
# 目标目录已加入 .gitignore，不会被误提交。
#
# build-apk.ps1 会自动调用本脚本，通常不需要手动执行。

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $Here          # 仓库根
$Target = Join-Path $Here "app\src\main\python"

Write-Host "=== 同步 Python 源码到安卓工程 ===" -ForegroundColor Cyan
Write-Host "  源  : $ProjectRoot"
Write-Host "  目标: $Target"

# 要同步的顶层内容：整个 app 包 + 两个入口脚本
$items = @("app", "run_app.py", "run_headless.py")

if (Test-Path $Target) {
    Get-ChildItem $Target -Force | ForEach-Object {
        # 保留 Android 侧自己写的 bootstrap.py，其余清掉后重建
        if ($_.Name -ne "bootstrap.py") { Remove-Item $_.FullName -Recurse -Force }
    }
} else {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
}

# 复制时排除缓存与模拟器产物，避免把无关文件塞进 APK
$excludeDirs = @("__pycache__", ".sim", ".pytest_cache", ".ruff_cache")

function Copy-Tree($source, $destination) {
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Get-ChildItem $source -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            if ($excludeDirs -contains $_.Name) { return }
            Copy-Tree $_.FullName (Join-Path $destination $_.Name)
        } else {
            if ($_.Extension -in @(".pyc", ".pyo")) { return }
            Copy-Item $_.FullName (Join-Path $destination $_.Name) -Force
        }
    }
}

foreach ($item in $items) {
    $source = Join-Path $ProjectRoot $item
    if (-not (Test-Path $source)) {
        throw "找不到 $source —— 请在项目根目录下运行（本脚本期望 android/ 位于仓库根之下）"
    }
    if ((Get-Item $source).PSIsContainer) {
        Copy-Tree $source (Join-Path $Target $item)
    } else {
        Copy-Item $source (Join-Path $Target $item) -Force
    }
}

# 统计结果，便于确认同步真的发生了
$pyCount = (Get-ChildItem (Join-Path $Target "app") -Recurse -Filter *.py |
            Where-Object { $_.FullName -notmatch "__pycache__" }).Count
$dataCount = (Get-ChildItem (Join-Path $Target "app") -Recurse -File |
              Where-Object { $_.Extension -in @(".gz", ".pem") }).Count
Write-Host "  已同步 $pyCount 个 .py 文件，$dataCount 个数据文件（证书/文案表）" -ForegroundColor Green

# 抽查关键文件是否到位：缺了会在运行时才炸
$must = @(
    "app\bambu\certs\ca_cert.pem",
    "app\bambu\data\hms_zh_cn.json.gz",
    "app\headless.py",
    "app\web\server.py",
    "bootstrap.py"
)
foreach ($m in $must) {
    $path = Join-Path $Target $m
    if (-not (Test-Path $path)) { throw "同步后缺少关键文件：$m" }
}
Write-Host "  关键文件校验通过（内置 CA / HMS 文案表 / 入口 / 引导模块）" -ForegroundColor Green
