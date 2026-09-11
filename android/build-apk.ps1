# 构建安卓 APK（含前置检查，缺什么会明确告诉你）。
#
# 用法（Windows PowerShell）：
#     powershell -ExecutionPolicy Bypass -File android\build-apk.ps1
#
# 前置条件：JDK 17+ 与 Android SDK。SDK 未安装时本脚本会给出安装指引，
# 而不是抛一堆看不懂的错误 —— APK 构建链的报错对不熟悉安卓的人非常不友好。

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

Write-Host "=== 前置检查 ===" -ForegroundColor Cyan
$problems = @()

# --- JDK ---
# ⚠️ `java -version` 把版本号写到 **stderr**，而本脚本设了 $ErrorActionPreference=Stop，
# 于是它会被当成终止性错误。这里临时放宽，只取第一行文本。
$java = Get-Command java -ErrorAction SilentlyContinue
if ($java) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $javaVersion = (& java -version 2>&1 | Select-Object -First 1)
    $ErrorActionPreference = $saved
    Write-Host "  JDK : $javaVersion"
} else {
    $problems += "未找到 java。请安装 JDK 17+（例如 Microsoft OpenJDK 或 Temurin）。"
}

# --- Android SDK ---
$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) {
    foreach ($candidate in @(
        (Join-Path $env:LOCALAPPDATA "Android\Sdk"),
        "C:\Android\Sdk",
        "D:\Android\Sdk"
    )) {
        if (Test-Path $candidate) { $sdk = $candidate; break }
    }
}
if ($sdk -and (Test-Path $sdk)) {
    Write-Host "  SDK : $sdk"
    if (-not (Test-Path (Join-Path $sdk "platforms\android-34"))) {
        $problems += "SDK 里没有 Android 34 平台。请用 Android Studio 的 SDK Manager 安装，或运行：sdkmanager `"platforms;android-34`""
    }
    if (-not (Test-Path (Join-Path $sdk "build-tools"))) {
        $problems += "SDK 里没有 build-tools。请安装：sdkmanager `"build-tools;34.0.0`""
    }
} else {
    # 用单引号 here-string：里面要写 PowerShell 命令示例（含双引号），
    # 双引号版本会让解析器把 "$env:..." 当成变量展开而报错
    $problems += @'
未找到 Android SDK。三种安装方式任选：
    1) 装 Android Studio（最省事，自带 SDK 与 Gradle）：
       https://developer.android.com/studio
    2) 只装命令行工具（约 500MB，适合 CI）：
       下载 https://developer.android.com/studio#command-line-tools-only
       解压后设置 ANDROID_HOME，再执行：
         sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0"
    3) 不装 SDK，改用 Android Studio 图形界面打开本目录直接 Build APK
'@
}

# --- Gradle ---
$gradle = Get-Command gradle -ErrorAction SilentlyContinue
$wrapper = Join-Path $Here "gradlew.bat"
if ($gradle) {
    Write-Host "  Gradle : $($gradle.Source)"
} elseif (Test-Path $wrapper) {
    Write-Host "  Gradle : 使用项目内 gradlew"
} else {
    $problems += "未找到 gradle，也没有 gradlew。请安装 Gradle 8.9+，或先执行 `gradle wrapper` 生成包装器。"
}

if ($problems.Count -gt 0) {
    Write-Host ""
    Write-Host "缺少以下前置条件，无法构建：" -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "  - $p" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "提示：用 Android Studio 打开本目录（$Here）是最省事的路径，" -ForegroundColor Cyan
    Write-Host "      它会自动补齐 SDK 与 Gradle，然后 Build → Build APK(s)。" -ForegroundColor Cyan
    exit 2
}

if ($sdk) { $env:ANDROID_HOME = $sdk }

Write-Host ""
Write-Host "=== 构建 debug APK ===" -ForegroundColor Cyan
if (Test-Path $wrapper) {
    & $wrapper assembleDebug
} else {
    # 还没有 wrapper：先生成再构建
    gradle wrapper
    if ($LASTEXITCODE -ne 0) { throw "gradle wrapper 失败" }
    & $wrapper assembleDebug
}
if ($LASTEXITCODE -ne 0) { throw "APK 构建失败" }

$apk = Join-Path $Here "app\build\outputs\apk\debug\app-debug.apk"
if (Test-Path $apk) {
    $mb = [math]::Round((Get-Item $apk).Length / 1MB, 2)
    Write-Host ""
    Write-Host "构建完成：$apk ($mb MB)" -ForegroundColor Green
    Write-Host ""
    Write-Host "提醒：装到手机前请确认 MainActivity.DEFAULT_URL 已改成你服务端的局域网地址" -ForegroundColor Yellow
    Write-Host "（或装好后长按屏幕输入地址）。详见 android/README.md。" -ForegroundColor Yellow
} else {
    throw "构建结束但没找到 APK，请检查上面的输出"
}
