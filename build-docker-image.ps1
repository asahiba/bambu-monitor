# 构建 Docker 单文件分发产物（一个 .tar 文件 = 完整镜像）。
#
# 用法（Windows PowerShell）：
#     powershell -ExecutionPolicy Bypass -File build-docker-image.ps1
#
# 为什么叫"单文件"：`docker save` 把整个镜像（含所有层与依赖）打成一个 tar，
# 拷到目标机后 `docker load` 即可运行，不需要目标机联网拉依赖、
# 也不需要源码。这与 exe 的"单文件"是同一个交付形态。
#
# 本脚本会：
#   1. 构建运行镜像（Dockerfile：只装服务端依赖，不含 Qt）
#   2. 起容器跑健康检查，确认真的能跑
#   3. docker save 成 tar，再用 gzip 压小

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Image = if ($env:BAMBU_IMAGE) { $env:BAMBU_IMAGE } else { "bambu-monitor" }
$Tag = if ($env:BAMBU_TAG) { $env:BAMBU_TAG } else { "latest" }
$OutDir = Join-Path $Root "dist-docker"
$TarGz = Join-Path $OutDir "bambu-monitor-$Tag-image.tar.gz"

Write-Host "=== 1/4 构建镜像 $Image`:$Tag ===" -ForegroundColor Cyan
docker build -t "$Image`:$Tag" .
if ($LASTEXITCODE -ne 0) { throw "镜像构建失败" }

Write-Host "=== 2/4 冒烟测试（起容器 → 健康检查 → 停止）===" -ForegroundColor Cyan
$container = "bambu-monitor-smoke-$PID"
# ⚠️ 清理不存在的容器时 `docker rm` 会往 stderr 写 "No such container"，
# 在 $ErrorActionPreference=Stop 下会被当成终止性错误而中断整个脚本。
# 因此调 docker 的清理/启动动作时临时放宽，再恢复。
$ErrorActionPreference = "Continue"
docker rm -f $container 2>&1 | Out-Null
docker run -d --name $container -p 18080:8080 "$Image`:$Tag" 2>&1 | Out-Null
$ErrorActionPreference = "Stop"
try {
    $healthy = $false
    for ($i = 1; $i -le 20; $i++) {
        Start-Sleep -Seconds 3
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:18080/health" -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
    }
    if ($healthy) {
        Write-Host "  健康检查通过（/health = 200）" -ForegroundColor Green
        $ErrorActionPreference = "Continue"
        $status = docker inspect --format '{{.State.Health.Status}}' $container 2>&1
        $ErrorActionPreference = "Stop"
        Write-Host "  容器健康状态: $status"
    } else {
        Write-Host "  健康检查未通过，容器日志：" -ForegroundColor Yellow
        $ErrorActionPreference = "Continue"
        docker logs --tail 20 $container
        $ErrorActionPreference = "Stop"
    }
} finally {
    $ErrorActionPreference = "Continue"
    docker rm -f $container 2>&1 | Out-Null
    $ErrorActionPreference = "Stop"
}

Write-Host "=== 3/4 导出为单个 tar ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Tar = Join-Path $OutDir "bambu-monitor-$Tag-image.tar"
docker save -o $Tar "$Image`:$Tag"
if ($LASTEXITCODE -ne 0) { throw "docker save 失败" }
$tarMb = [math]::Round((Get-Item $Tar).Length / 1MB, 1)

Write-Host "=== 4/4 压缩 ===" -ForegroundColor Cyan
if (Test-Path $TarGz) { Remove-Item $TarGz -Force }
# 用 .NET 的 GZipStream 压缩（PowerShell 5.1 没有 Compress-Archive 处理大文件的好选项）
$in = [System.IO.File]::OpenRead($Tar)
$out = [System.IO.File]::Create($TarGz)
$gz = New-Object System.IO.Compression.GZipStream($out, [System.IO.Compression.CompressionLevel]::Optimal)
try { $in.CopyTo($gz) } finally { $gz.Dispose(); $out.Dispose(); $in.Dispose() }
Remove-Item $Tar -Force
$gzMb = [math]::Round((Get-Item $TarGz).Length / 1MB, 1)

Write-Host ""
Write-Host "构建完成：" -ForegroundColor Green
Write-Host "  未压缩 tar : $tarMb MB（已删除，只保留下面的压缩包）"
Write-Host "  分发文件   : $TarGz ($gzMb MB)"
Write-Host ""
Write-Host "目标机上的用法（无需源码、无需联网）："
Write-Host "  docker load -i bambu-monitor-$Tag-image.tar.gz"
Write-Host "  docker run -d --name bambu-monitor --network host -v `$PWD/data:/data $Image`:$Tag"
Write-Host "  # 无真机时可先演示："
Write-Host "  docker run --rm -p 8080:8080 $Image`:$Tag python -m app.headless --sim 4 --status-interval 0"
