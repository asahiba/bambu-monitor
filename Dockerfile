# 拓竹打印机监控台 · 无界面服务版（Linux 容器）
#
# 构建：  docker build -t bambu-monitor .
# 运行：  docker run -d --name bambu-monitor --network host \
#             -v /path/to/data:/data bambu-monitor
# 或直接：docker compose up -d
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BAMBU_MONITOR_CONFIG_DIR=/data \
    BAMBU_WEB_PORT=8080 \
    BAMBU_WEB_FPS=4 \
    BAMBU_MAX_FPS=10 \
    TZ=Asia/Shanghai

WORKDIR /app

# 只装服务端依赖（paho-mqtt + opencv-headless），不装 Qt
COPY requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY app ./app

# 配置与日志放在挂载卷里
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('BAMBU_WEB_PORT','8080')+'/health', timeout=3).status==200 else 1)"

# 无界面服务模式；额外参数可在 docker run 时追加
# 例：docker run ... bambu-monitor python -m app.headless --discover
CMD ["python", "-m", "app.headless", "--status-interval", "0"]
