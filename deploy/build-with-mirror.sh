#!/usr/bin/env bash
# 在没有 Docker Hub 直连的服务器上构建并启动 PA Agent。
#
# 用法（在 /opt/pa-agent 下）：
#   bash deploy/build-with-mirror.sh                # 自动探测能用的基础镜像源
#   bash deploy/build-with-mirror.sh --proxy        # 额外带上 apt 清华源
#
# 它会依次尝试几个常用的基础镜像源，第一个能从 Docker Hub 拉下来的就用它，
# 然后 docker compose up -d --build。

set -u

cd "$(dirname "$0")/.." || exit 1
echo "[build] 工作目录：$(pwd)"

USE_APT_MIRROR=0
for arg in "$@"; do
    case "$arg" in
        --proxy) USE_APT_MIRROR=1 ;;
        *) ;;
    esac
done

# 候选基础镜像（前两个是镜像站，最后一个是官方）
CANDIDATES=(
    "docker.m.daocloud.io/library/python:3.12-slim"
    "dockerproxy.net/library/python:3.12-slim"
    "hub-mirror.c.163.com/library/python:3.12-slim"
    "python:3.12-slim"
)

CHOSEN=""
for img in "${CANDIDATES[@]}"; do
    echo "[build] 尝试拉取基础镜像：$img"
    if timeout 90 docker pull "$img" >/dev/null 2>&1; then
        CHOSEN="$img"
        echo "[build] 成功：$img"
        break
    fi
    echo "[build] 失败，换下一个"
done

if [ -z "$CHOSEN" ]; then
    echo "[build] 所有候选基础镜像都拉不下来。" >&2
    echo "[build] 请先在 1Panel「容器 → 配置 → 镜像加速」里配好加速地址，" >&2
    echo "[build] 或者把 CANDIDATES 里加一个你能访问的镜像站再重试。" >&2
    exit 1
fi

APT_ARG=""
if [ "$USE_APT_MIRROR" = "1" ]; then
    APT_ARG="https://mirrors.tuna.tsinghua.edu.cn"
fi

echo "[build] 开始构建（基础镜像 $CHOSEN）…"
docker compose -f docker-compose.1panel.yml build \
    --build-arg "PYTHON_IMAGE=$CHOSEN" \
    --build-arg "APT_MIRROR=$APT_ARG" || exit 1

echo "[build] 启动容器…"
docker compose -f docker-compose.1panel.yml up -d || exit 1

echo
echo "[build] 完成。看日志："
echo "        docker logs -f pa-agent"
echo "[build] 健康检查："
echo "        docker exec pa-agent python -m pa_agent.daemon --health-check"
