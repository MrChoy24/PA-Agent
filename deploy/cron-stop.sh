#!/bin/sh
# 1Panel 计划任务 —— 每天定时「停止」pa-agent 容器（例如次日 08:00）。
#
# 面板填法同 cron-start.sh：
#   任务类型：Shell 脚本
#   容器：不勾选「在容器中执行」
#   解释器：/bin/sh
#
# ⚠️ 为什么必须自己发 SIGTERM 并等待，而不是直接 `docker stop`：
#   compose 文件里写了 stop_grace_period: 30s，但那条只对 `docker compose stop`
#   生效；计划任务里直接 `docker stop` 用的是默认 10 秒。
#   而守护进程收到 SIGTERM 后，只有在【轮询间隙】才会退出——如果此刻正在跑一轮
#   AI 分析（实测单轮可能 5~15 分钟），它不会中途中断，10 秒后就被 SIGKILL 强杀。
#   所以这里：发 SIGTERM → 每 5 秒看一次状态 → 等满 TIMEOUT（默认 300 秒）才强杀。

set -u

CONTAINER="${PA_AGENT_CONTAINER:-pa-agent}"
TIMEOUT="${PA_AGENT_STOP_TIMEOUT:-300}"
LOG_PREFIX="[pa-agent 定时停止]"

log() {
    printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_PREFIX" "$*"
}

if ! command -v docker >/dev/null 2>&1; then
    log "失败：找不到 docker 命令"
    exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    log "容器 $CONTAINER 不存在，视为已停止"
    exit 0
fi

STATE="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)"
if [ "$STATE" != "running" ]; then
    log "容器 $CONTAINER 当前状态 $STATE，无需停止"
    exit 0
fi

# 先看有没有正在跑的分析，把它记进日志，方便解释这一轮花了多久才停下。
INFLIGHT="$(docker logs --tail 40 "$CONTAINER" 2>&1 | grep -c '开始分析' || true)"
log "发送 SIGTERM（最近 40 行里有 $INFLIGHT 条「开始分析」）"
docker kill -s SIGTERM "$CONTAINER" >/dev/null 2>&1 || true

waited=0
while [ "$waited" -lt "$TIMEOUT" ]; do
    sleep 5
    waited=$((waited + 5))
    STATE="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo gone)"
    if [ "$STATE" = "exited" ] || [ "$STATE" = "gone" ] || [ "$STATE" = "created" ]; then
        log "已正常停止（等待 ${waited}s，最终状态 $STATE）"
        exit 0
    fi
done

# 到达上限仍在跑：说明那一轮分析没结束，只能强杀。磁盘上会留一条 partial 记录
# （records/pending/*.json），下次启动的增量分析会忽略它，不影响后续运行。
log "等待 ${TIMEOUT}s 后仍在运行，强制停止：docker stop -t 0 $CONTAINER"
docker stop -t 0 "$CONTAINER" >/dev/null 2>&1 || true
log "已强制停止"
exit 0
