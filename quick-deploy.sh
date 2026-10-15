#!/usr/bin/env bash
# ==============================================================================
# WorkBuddy2API-Agent 一键安装与快速更新脚本（本地源码构建）
# 支持系统: Linux, macOS, NAS (fnOS / 群晖 / 威联通 / Unraid 等)
# 用法:
#   首次部署: curl -fsSL https://raw.githubusercontent.com/cookie-kangd/workbuddy2api-agent/main/quick-deploy.sh | bash
#   快速更新: 再次运行相同的命令即可完成平滑无感升级（账号配置绝不丢失）
# 说明: 本仓库不推送预编译 Docker 镜像，脚本会克隆源码并本地构建镜像。
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "\n${BLUE}======================================================${NC}"
echo -e "${GREEN}   WorkBuddy2API-Agent Docker 一键部署与更新脚本${NC}"
echo -e "${BLUE}======================================================${NC}\n"

REPO_URL="https://github.com/cookie-kangd/workbuddy2api-agent.git"

# 1. 检查 Docker 环境
if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未检测到 Docker，请先安装 Docker 后再运行此脚本。${NC}"
    exit 1
fi

# 检查 Docker Compose 支持
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    echo -e "${RED}[错误] 未检测到 Docker Compose 支持 (docker compose / docker-compose)。${NC}"
    exit 1
fi

# 2. 确定工作目录（优先复用当前目录里的仓库，否则克隆一份）
APP_DIR="workbuddy2api-agent"
if [ -f "docker-compose.yml" ] && grep -q "wb-proxy" docker-compose.yml 2>/dev/null; then
    TARGET_DIR="$(pwd)"
    echo -e "${BLUE}[信息] 在当前目录检测到项目配置: ${TARGET_DIR}${NC}"
else
    TARGET_DIR="$(pwd)/${APP_DIR}"
    if [ ! -d "${TARGET_DIR}/.git" ]; then
        echo -e "${BLUE}[信息] 克隆仓库到: ${TARGET_DIR}${NC}"
        git clone --depth 1 "${REPO_URL}" "${TARGET_DIR}"
    fi
    cd "${TARGET_DIR}"
fi

# 3. 首次安装 / 更新：都是拉最新源码后重建镜像，数据目录不动
mkdir -p accounts usage
if [ -d .git ]; then
    echo -e "${YELLOW}[更新模式] 拉取最新源码...${NC}"
    git pull --ff-only || echo -e "${YELLOW}[提示] 源码拉取失败，改用本地副本继续。${NC}"
fi
echo -e "${BLUE}[信息] 正在构建镜像并启动容器...${NC}"
$COMPOSE_CMD up -d --build
echo -e "\n${GREEN}[成功] WorkBuddy2API-Agent 已就绪！${NC}"

# 4. 获取本地 IP 并输出引导提示
LOCAL_IP="127.0.0.1"
if command -v hostname >/dev/null 2>&1; then
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
fi

echo -e "\n${BLUE}------------------------------------------------------${NC}"
echo -e "${GREEN}Web 控制看板:${NC} http://${LOCAL_IP:-127.0.0.1}:8788"
echo -e "${GREEN}OpenAI 接口:${NC}  http://${LOCAL_IP:-127.0.0.1}:8788/v1"
echo -e "${BLUE}------------------------------------------------------${NC}"
echo -e "${YELLOW}查看初始 API Key:${NC}  $COMPOSE_CMD logs wb-proxy | grep -i 'api key'"
echo -e "${YELLOW}查看实时运行日志:${NC}  $COMPOSE_CMD logs -f wb-proxy"
echo -e "${YELLOW}后续如何更新:${NC}      再次运行本脚本，或执行: git pull && $COMPOSE_CMD up -d --build"
echo -e "${BLUE}======================================================${NC}\n"
