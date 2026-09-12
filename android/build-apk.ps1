# 构建安卓 APK（平板/手机可独立运行的完整版）。
#
# 用法（在项目根目录或 android/ 下均可）：
#     powershell -ExecutionPolicy Bypass -File android\build-apk.ps1
#
# 依次做：前置检查 → 同步 Python 源码 → Gradle 构建 → 把 APK 拷到 dist-android/
#
# 前置条件（本机已具备）：
#   * JDK 17+
#   * Android SDK（ANDROID_HOME 或 D:\Android\Sdk），含 platforms;android-34 与 build-tools;34.0.0
#   * Gradle 8.9（或 D:\Android\gradle-8.9）
#   * 项目自己的 .venv（Chaquopy 需要本机 Python 来打包 pip 依赖）
# 缺什么会明确告知，而不是抛一堆难懂的 Gradle 报错。

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $Here
Set-Location $Here

Write-Host "=== 1/5 前置检查 ===" -ForegroundColor Cyan
$problems = @()

# --- JDK ---
$java = Get-Command java -ErrorAction SilentlyContinue
if ($java) {
    # ⚠️ `java -version` 把版本写到 stderr，而本脚本是 ErrorActionPreference=Stop，
    # 会被当成终止性错误。临时放宽再读。
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $javaVersion = (& java -version 2>&1 | Select-Object -First 1)
    $javaHome = Split-Path (Split-Path $java.Source -Parent) -Parent
    $ErrorActionPreference = $saved
    Write-Host "  JDK  : $javaVersion"
    Write-Host "  HOME : $javaHome"
    $env:JAVA_HOME = $javaHome
} else {
    $problems += "未找到 java。请安装 JDK 17+。"
}

# --- Android SDK ---
$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) {
    foreach ($candidate in @(
        "D:\Android\Sdk",
        (Join-Path $env:LOCALAPPDATA "Android\Sdk"),
        "C:\Android\Sdk"
    )) {
        if (Test-Path $candidate) { $sdk = $candidate; break }
    }
}
if ($sdk -and (Test-Path $sdk)) {
    Write-Host "  SDK  : $sdk"
    $env:ANDROID_HOME = $sdk
    $env:ANDROID_SDK_ROOT = $sdk
    # local.properties 是 Gradle 定位 SDK 与本地路径的常规方式（不入库）。
    # 同时写入 bambu.python —— Chaquopy 用它找"本机 Python"来打包 pip 依赖，
    # 不写的话它会在 PATH 里找 python3，而本机 PATH 上是应用商店占位符，必然失败。
    $pyForBuild = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    Set-Content -Path (Join-Path $Here "local.properties") -Encoding ASCII -Value @(
        ("sdk.dir=" + ($sdk -replace '\\', '\\')),
        ("bambu.python=" + ($pyForBuild -replace '\\', '\\'))
    )
    if (-not (Test-Path (Join-Path $sdk "platforms\android-34"))) {
        $problems += 'SDK 缺少 Android 34 平台：sdkmanager "platforms;android-34"'
    }
    if (-not (Test-Path (Join-Path $sdk "build-tools"))) {
        $problems += 'SDK 缺少 build-tools：sdkmanager "build-tools;34.0.0"'
    }
} else {
    $problems += @'
未找到 Android SDK。任选：
    1) 装 Android Studio（最省事）：https://developer.android.com/studio
    2) 只装命令行工具：
       下载 https://developer.android.com/studio#command-line-tools-only
       设 ANDROID_HOME 后执行：sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0"
'@
}

# --- Gradle ---
$gradleBin = $null
foreach ($candidate in @(
    (Join-Path $Here "gradlew.bat"),
    "D:\Android\gradle-8.9\bin\gradle.bat"
)) {
    if (Test-Path $candidate) { $gradleBin = $candidate; break }
}
if (-not $gradleBin) {
    $g = Get-Command gradle -ErrorAction SilentlyContinue
    if ($g) { $gradleBin = $g.Source }
}
if ($gradleBin) {
    Write-Host "  Gradle: $gradleBin"
} else {
    $problems += "未找到 gradle。请装 Gradle 8.9，或放到 D:\Android\gradle-8.9"
}

# --- 项目 venv（Chaquopy 打包 pip 依赖要用）---
$py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $py) {
    Write-Host "  Python: $py"
} else {
    $problems += "找不到 $py。请先运行 run.bat 建好虚拟环境。"
}

if ($problems.Count -gt 0) {
    Write-Host ""
    Write-Host "缺少以下前置条件，无法构建：" -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "  - $p" -ForegroundColor Yellow }
    exit 2
}

Write-Host ""
Write-Host "=== 2/5 同步 Python 源码进安卓工程 ===" -ForegroundColor Cyan
& powershell -ExecutionPolicy Bypass -File (Join-Path $Here "sync-python.ps1")

Write-Host ""
Write-Host "=== 3/5 Gradle 构建（首次要下载 Chaquopy/AGP 依赖，约 3-10 分钟）===" -ForegroundColor Cyan
# Chaquopy 的 buildPython 已在 app/build.gradle 里从 local.properties 读取
& $gradleBin assembleDebug --no-daemon --console=plain
if ($LASTEXITCODE -ne 0) { throw "Gradle 构建失败（退出码 $LASTEXITCODE）" }

Write-Host ""
Write-Host "=== 4/5 收集产物 ===" -ForegroundColor Cyan
$apk = Join-Path $Here "app\build\outputs\apk\debug\app-debug.apk"
if (-not (Test-Path $apk)) { throw "构建结束但找不到 APK：$apk" }
$outDir = Join-Path $ProjectRoot "dist-android"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$target = Join-Path $outDir "BambuMonitor-1.0.0-arm64.apk"
Copy-Item $apk $target -Force
$mb = [math]::Round((Get-Item $target).Length / 1MB, 1)

Write-Host ""
Write-Host "=== 5/5 完成 ===" -ForegroundColor Green
Write-Host "  APK : $target ($mb MB)"
Write-Host "  架构：arm64-v8a（当前绝大多数平板/手机）"
Write-Host "  系统：Android 7.0+（API 24）"
Write-Host "  运行：内置 Python 服务跑在设备本机，无需电脑在旁边"
