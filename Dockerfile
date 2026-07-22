# ============================================================
# selenium_traffic_system Docker 镜像
# 基于 Python 3.11 + Chromium（适用于东京 VPS 部署）
# ============================================================
FROM python:3.11-slim-bookworm

LABEL maintainer="selenium_traffic_system"
LABEL description="Selenium Traffic System with AdSense Risk Control"

# 避免交互式安装提示
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# 安装系统依赖 + Chromium 浏览器
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium 浏览器及依赖
    chromium \
    chromium-driver \
    # 中文字体（避免页面渲染方块）
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    # 基础工具
    curl \
    wget \
    unzip \
    # 进程管理
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# 设置 Chromium 路径环境变量
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# 创建工作目录
WORKDIR /app

# 先复制依赖文件（利用 Docker 缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 创建必要目录
RUN mkdir -p /app/qa_sessions /app/report /app/logs /app/test_reports

# 创建默认配置文件（如不存在）
RUN if [ ! -f config.json ]; then \
    echo '{}' > config.json; \
    fi

# 暴露 Flask 端口
EXPOSE 5001

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${RUN_PORT:-5001}/api/status || exit 1

# 启动命令
CMD ["python", "app.py"]
