#!/usr/bin/env bash
# ==============================================================================
# WorkBuddy2API-Agent 一键安装与快速更新脚本（本地源码构建）
# 支持系统: Linux, macOS, NAS (fnOS / 群晖 / 威联通 / Unraid 等)
# 用法:
#   首次部署: curl -fsSL https://raw.githubusercontent.com/cookie-kangd/workbuddy2api-agent/main/quick-deploy.sh | bash
#   快速更新: 再次运行相同的命令即可完成平滑无感升级（账号配置绝不丢失）
# 说明: 本仓库不推送预编译 Docker 镜像，脚本会克隆源码并本地构建镜像。
#   部署模式: WB_DEPLOY_MODE=local|lan|public（默认 lan）
#             local/public 只把 8788 绑到 127.0.0.1，公网机请再套 Nginx/Caddy/Tunnel
#             示例: curl -fsSL .../quick-deploy.sh | WB_DEPLOY_MODE=public bash
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

# 0. 部署模式：本机 / 公网机器默认不要直接暴露 8788；容器内仍监听 0.0.0.0，
#    由 Docker 的 host 端绑定决定谁能连进来。
DEPLOY_MODE="${WB_DEPLOY_MODE:-lan}"
case "${DEPLOY_MODE}" in
    local|lan|public) ;;
    *)
        echo -e "${RED}[错误] WB_DEPLOY_MODE 只能是 local / lan / public，当前为: ${DEPLOY_MODE}${NC}"
        exit 1
        ;;
esac
if [ "${DEPLOY_MODE}" = "lan" ]; then
    PORT_BINDING="8788:8788"
else
    PORT_BINDING="127.0.0.1:8788:8788"
fi
echo -e "${BLUE}[信息] 部署模式: ${DEPLOY_MODE}（端口映射 ${PORT_BINDING}）${NC}"

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
    if [ -n "${WB_DEPLOY_MODE:-}" ]; then
        echo -e "${YELLOW}[提示] 已有部署不会自动改写端口映射；如需限制监听地址，请手动编辑 docker-compose.yml 的 ports。${NC}"
    fi
    echo -e "${YELLOW}[更新模式] 拉取最新源码...${NC}"
    git pull --ff-only || echo -e "${YELLOW}[提示] 源码拉取失败，改用本地副本继续。${NC}"
fi
if [ "${DEPLOY_MODE}" != "lan" ] && [ -f "docker-compose.yml" ]; then
    # 源码自带 compose 默认把 8788 开到全部网卡；local/public 改成只绑回环，
    # 公网机器就必须再套一层反代。只改 ports 一行，不动其余配置。
    if grep -q "^[[:space:]]*-[[:space:]]*\"8788:8788\"" docker-compose.yml 2>/dev/null; then
        sed -i.bak 's|- "8788:8788"|- "${PORT_BINDING}"|' docker-compose.yml
        rm -f docker-compose.yml.bak
        echo -e "${BLUE}[信息] 已按 ${DEPLOY_MODE} 模式把端口绑定改为 ${PORT_BINDING}${NC}"
    fi
fi
echo -e "${BLUE}[信息] 正在构建镜像并启动容器...${NC}"
$COMPOSE_CMD up -d --build
echo -e "\n${GREEN}[成功] WorkBuddy2API-Agent 已就绪！${NC}"

# 4. 获取本地 IP 并输出引导提示
LOCAL_IP="127.0.0.1"
if command -v hostname >/dev/null 2>&1; then
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
fi

if [ "${DEPLOY_MODE}" = "lan" ]; then
    SHOW_HOST="${LOCAL_IP:-127.0.0.1}"
else
    SHOW_HOST="127.0.0.1"
fi
echo -e "\n${BLUE}------------------------------------------------------${NC}"
echo -e "${GREEN}Web 控制看板:${NC} http://${SHOW_HOST}:8788"
echo -e "${GREEN}OpenAI 接口:${NC}  http://${SHOW_HOST}:8788/v1"
if [ "${DEPLOY_MODE}" = "public" ]; then
    echo -e "${YELLOW}公网部署:${NC} 8788 只绑在本机回环地址，请用 Nginx / Caddy / Cloudflare Tunnel 反代并加 TLS，不要直接把 8788 开到公网。"
fi
echo -e "${BLUE}------------------------------------------------------${NC}"
echo -e "${YELLOW}查看初始 API Key:${NC}  $COMPOSE_CMD logs wb-proxy | grep -i 'api key'"
echo -e "${YELLOW}查看实时运行日志:${NC}  $COMPOSE_CMD logs -f wb-proxy"
echo -e "${YELLOW}后续如何更新:${NC}      再次运行本脚本，或执行: git pull && $COMPOSE_CMD up -d --build"
echo -e "${BLUE}======================================================${NC}\n"
