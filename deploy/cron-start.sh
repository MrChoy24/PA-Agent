#!/bin/sh
# 1Panel 计划任务 —— 每天定时「启动」pa-agent 容器（例如 18:00）。
#
# 面板填法：
#   任务类型：Shell 脚本
#   执行周期：每天 18:00（用「每小时」+ 开始时间 18 点，或直接写 cron 表达式）
#   容器：不勾选「在容器中执行」——本脚本要在【宿主机】上跑 docker 命令
#   解释器：/bin/sh
#
# 本脚本只做三件事：确认容器存在 → 启动 → 打印第一屏日志自证启动成功。
# 用 docker start 而不是 docker compose up，是因为后者会检查/重建容器，
# 计划任务里没必要冒这个风险（挂载与 env_file 都已在容器创建时固化）。

set -u

CONTAINER="${PA_AGENT_CONTAINER:-pa-agent}"
COMPOSE_FILE="${PA_AGENT_COMPOSE:-/opt/pa-agent/docker-compose.1panel.yml}"
LOG_PREFIX="[pa-agent 定时启动]"

log() {
    printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_PREFIX" "$*"
}

if ! command -v docker >/dev/null 2>&1; then
    log "失败：找不到 docker 命令"
    exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    log "容器 $CONTAINER 不存在。首次部署请先执行："
    log "  docker compose -f $COMPOSE_FILE up -d"
    exit 1
fi

# 已经在跑就直接返回成功：计划任务的重复执行不应该报错。
STATE="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)"
if [ "$STATE" = "running" ]; then
    log "容器 $CONTAINER 已在运行，无需启动"
    exit 0
fi

if ! docker start "$CONTAINER" >/dev/null; then
    log "失败：docker start $CONTAINER 返回非零"
    exit 1
fi

log "已启动容器 $CONTAINER（启动前状态：$STATE）"

# 等 15 秒让守护进程打出启动行，把这几个关键值写进计划任务日志，
# 以后翻任务记录就能看出当时用的是哪条链路、哪个周期。
sleep 15
log "最近日志："
docker logs --tail 12 "$CONTAINER" 2>&1 | sed 's/^/    /'

exit 0
