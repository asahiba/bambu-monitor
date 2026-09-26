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
#   * **Python 3.10**（不是项目主 .venv 的 3.13 —— 原因见下面 Get-BuildPython 的注释）
# 缺什么会明确告知，而不是抛一堆难懂的 Gradle 报错。

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $Here
Set-Location $Here

# Chaquopy 需要一个 **3.10** 的 Python 来解释/编译 pip 依赖。
#
# 为什么不能直接用项目主 .venv（3.13）：Chaquopy 的 install 只接受与它自己
# 平台标签匹配的 wheel，而本工程**刻意不装任何预编译原生包**
# （opencv / numpy / cryptography 在 Chaquopy 仓库里是 4096 字节对齐的，
# 在 16 KB 内存页设备上会拒绝加载并闪退 —— 见
# tools/strip_16kb_incompatible_deps.py 的说明）。
# 于是这里只剩纯 Python 的 paho-mqtt，任何 Python 版本都能装；
# 仍固定 3.10 是因为它是 Chaquopy 17 的默认版本、覆盖最全，
# 且与"曾经用过 cp310 的 opencv"保持一致，避免以后再踩平台标签的坑。
# Chaquopy 17 起要求 buildPython 的主次版本与 chaquopy.version 完全一致。
function Get-BuildPython {
    # 复用的两段小逻辑：读某解释器的主次版本 / 确保 .venv310 存在后返回它
    function Get-PyMinor($exe) {
        $savedEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $ver = (& $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
        $ErrorActionPreference = $savedEap
        return "$ver".Trim()
    }
    function Use-Py310($exe) {
        $venv = Join-Path $ProjectRoot ".venv310\Scripts\python.exe"
        if (-not (Test-Path $venv)) {
            Write-Host "  正在创建 Python 3.10 构建用虚拟环境 .venv310 ..."
            & $exe -m venv (Join-Path $ProjectRoot ".venv310")
        }
        if (Test-Path $venv) { return $venv }
        return $exe  # 建不出来就直接用原解释器，Chaquopy 也能干活
    }

    # 1) 显式指定优先。CI（GitHub Actions 的 setup-python）会把解释器装在
    #    hostedtoolcache 下，下面的注册表/固定路径都找不到它，必须留这个口子。
    if ($env:BAMBU_BUILD_PYTHON -and (Test-Path $env:BAMBU_BUILD_PYTHON)) {
        $exe = $env:BAMBU_BUILD_PYTHON
        $minor = Get-PyMinor $exe
        if ($minor -eq "3.10") { return Use-Py310 $exe }
        Write-Host "  ⚠ BAMBU_BUILD_PYTHON 指向的不是 3.10（实际 $minor），已忽略" -ForegroundColor Yellow
    }

    # 2) 当前正在跑的解释器（CI 里 setup-python 之后它就是 3.10）
    $self = (Get-Process -Id $PID).Path
    if ($self -and (Split-Path $self -Leaf) -match '^python') {
        if ((Get-PyMinor $self) -eq "3.10") { return Use-Py310 $self }
    }

    $candidates = @(
        (Join-Path $ProjectRoot ".venv310\Scripts\python.exe")
    )

    # 找所有已安装的 Python，挑出 3.10
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($v in @("-3.10")) {
            $savedEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $found = (& py $v -c "import sys;print(sys.executable)" 2>$null)
            $ErrorActionPreference = $savedEap
            if ($LASTEXITCODE -eq 0 -and $found) { $candidates += ($found | Select-Object -First 1).Trim() }
        }
    }
    foreach ($p in @(
        "D:\python310\python.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        "C:\Python310\python.exe"
    )) { $candidates += $p }

    foreach ($exe in $candidates) {
        if (-not (Test-Path $exe)) { continue }
        if ((Get-PyMinor $exe) -ne "3.10") { continue }
        return Use-Py310 $exe
    }
    return $null
}

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
    $pyForBuild = Get-BuildPython
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

# --- Python 3.10（Chaquopy 打包 pip 依赖要用）---
$py = Get-BuildPython
if ($py) {
    Write-Host "  Python: $py (3.10)"
} else {
    $problems += @'
未找到 Python 3.10。安卓版必须用 3.10 构建（原因见 android/app/build.gradle 里
chaquopy 段的注释：Chaquopy 仓库在 android_24_arm64_v8a 上 opencv 只有 cp310）。
装一个即可：
    下载 https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe
    安装时勾选 "Add Python to PATH"，或装到 D:\python310
装好后本脚本会自动创建 .venv310。
'@
}

if ($problems.Count -gt 0) {
    Write-Host ""
    Write-Host "缺少以下前置条件，无法构建：" -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "  - $p" -ForegroundColor Yellow }
    exit 2
}

Write-Host ""
Write-Host "=== 2/5 同步 Python 源码进安卓工程 ===" -ForegroundColor Cyan
# ⚠️ 必须检查子脚本的退出码。
#
# 踩过的坑：sync-python.ps1 在 CI 上失败（它报"同步后缺少关键文件"并 throw），
# 但这里没检查退出码，于是脚本**继续往下跑 Gradle** —— 产出的 APK 是上一次的
# 旧产物，最后表现为"包里的 Python 模块不对"这种极难定位的现象。
# 同步是后面一切的前提，失败必须立刻停。
& powershell -ExecutionPolicy Bypass -File (Join-Path $Here "sync-python.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "同步 Python 源码失败（退出码 $LASTEXITCODE）—— 见上面的报错，先修好再构建"
}

# --- 固定签名材料 ---
# 把 .android-signing/（不入库）里的 keystore 与口令复制到 android/，
# Gradle 从 android/keystore.properties 读取（见 app/build.gradle 的签名段）。
# 缺这些文件也能构建，只是退回 debug 签名 —— 但那样装过的用户无法覆盖更新。
Write-Host ""
Write-Host "=== 准备签名材料 ===" -ForegroundColor Cyan
$signSrc = Join-Path $ProjectRoot ".android-signing"
$signProps = Join-Path $Here "keystore.properties"
if (Test-Path (Join-Path $signSrc "keystore.properties")) {
    Copy-Item (Join-Path $signSrc "keystore.properties") $signProps -Force
    Copy-Item (Join-Path $signSrc "*.jks") $Here -Force -ErrorAction SilentlyContinue
    Write-Host "  已使用固定发布签名（.android-signing/）" -ForegroundColor Green
} elseif (Test-Path $signProps) {
    Write-Host "  已使用 android/keystore.properties" -ForegroundColor Green
} else {
    Write-Host "  [!] 没有签名材料 -> 本次用 debug 签名，无法覆盖安装其它签名版本" -ForegroundColor Yellow
    Write-Host "      需要正式签名见 docs/PACKAGING.md 的「安卓签名」一节" -ForegroundColor Yellow
}


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
$target = Join-Path $outDir "BambuMonitor-1.2.0-arm64.apk"
Copy-Item $apk $target -Force
$mb = [math]::Round((Get-Item $target).Length / 1MB, 1)

Write-Host ""
Write-Host "=== 5/5 完成 ===" -ForegroundColor Green
Write-Host "  APK : $target ($mb MB)"
Write-Host "  架构：arm64-v8a（当前绝大多数平板/手机）"
Write-Host "  系统：Android 7.0+（API 24）"
Write-Host "  运行：内置 Python 服务跑在设备本机，无需电脑在旁边"
