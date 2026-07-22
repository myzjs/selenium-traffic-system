#!/bin/bash
# ============================================================
# selenium_traffic_system 东京服务器 Docker 部署脚本
# 使用方法：
#   1. 将整个项目上传到东京服务器
#   2. chmod +x deploy.sh
#   3. ./deploy.sh
# ============================================================
set -e

echo "🚀 =========================================="
echo "🚀  Selenium Traffic System - 东京部署"
echo "🚀 =========================================="

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 检查 Docker 是否安装
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}📦 Docker 未安装，正在安装...${NC}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo -e "${GREEN}✅ Docker 安装完成${NC}"
fi

# 检查 Docker Compose
if ! command -v docker compose &> /dev/null; then
    echo -e "${YELLOW}📦 安装 Docker Compose 插件...${NC}"
    apt-get update && apt-get install -y docker-compose-plugin
fi

echo ""
echo "📋 检查配置文件..."

# 检查 .env 文件
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️  .env 文件不存在，从模板创建...${NC}"
    cp .env.example .env
    echo -e "${RED}❗ 请编辑 .env 文件填入真实凭据后重新运行此脚本${NC}"
    echo -e "${YELLOW}   nano .env${NC}"
    exit 1
fi

# 检查 config.json
if [ ! -f config.json ]; then
    echo -e "${YELLOW}⚠️  config.json 不存在，创建空配置...${NC}"
    echo '{}' > config.json
fi

# 创建必要目录
mkdir -p qa_sessions report logs

echo ""
echo "🔨 构建 Docker 镜像..."
docker compose build --no-cache

echo ""
echo "🚀 启动服务..."
docker compose up -d

echo ""
echo "⏳ 等待服务启动..."
sleep 5

# 检查服务状态
if docker compose ps | grep -q "running"; then
    echo ""
    echo -e "${GREEN}✅ ==========================================${NC}"
    echo -e "${GREEN}✅  部署成功！${NC}"
    echo -e "${GREEN}✅ ==========================================${NC}"
    echo ""
    echo "📌 访问地址:"
    
    # 获取服务器 IP
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")
    source .env 2>/dev/null || true
    PORT=${RUN_PORT:-5001}
    
    echo "   http://${SERVER_IP}:${PORT}"
    echo ""
    echo "📌 常用命令:"
    echo "   查看日志:    docker compose logs -f"
    echo "   重启服务:    docker compose restart"
    echo "   停止服务:    docker compose down"
    echo "   更新部署:    git pull && docker compose up -d --build"
    echo ""
    echo "📌 安全提醒:"
    echo "   - 建议配置防火墙仅允许特定 IP 访问 ${PORT} 端口"
    echo "   - 建议配置 Nginx 反向代理 + HTTPS"
    echo ""
else
    echo -e "${RED}❌ 服务启动失败，请检查日志:${NC}"
    echo "   docker compose logs"
    exit 1
fi
