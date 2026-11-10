#!/bin/sh
# PA Agent 容器入口。
#
# 为什么需要它：compose 把宿主机目录（config / logs / trade_records / records）
# 绑进容器。这些目录在宿主机上通常由 root 创建、属主是 root，而镜像里进程以
# 非特权用户（uid 10001）运行 —— 结果就是启动时报 Permission denied，日志写
# 不出来、配置写不回去。1Panel 的「容器 → 编排」正是这种用法，所以必须处理。
#
# 做法：镜像以 root 启动本脚本 → 修正挂载目录属主 → 用 setpriv 降权到
# PA_AGENT_UID/PA_AGENT_GID（默认 10001）再 exec 真正的命令。
# 如果镜像被以非 root 启动（docker run --user），则跳过修正直接运行。
set -eu

APP_UID="${PA_AGENT_UID:-10001}"
APP_GID="${PA_AGENT_GID:-10001}"

# HOME 修正：setpriv 只换 uid/gid，不改环境变量，所以降权后 HOME 仍指向 /root。
# 程序里有很多「探测 ~/.qoder-cn / ~/.trae-cn 等本地登录态」的逻辑，HOME 停在
# /root 会让每次探测都撞上 Permission denied。这里指到一个可写目录，探测直接
# 得到「不存在」，不再报错。
# 注意：这些桌面客户端的凭据在服务器上本来就没有，探测失败是预期行为。
if [ -d "/home/paagent" ]; then
    export HOME="${HOME_OVERRIDE:-/home/paagent}"
else
    export HOME="${HOME_OVERRIDE:-/tmp}"
fi

if [ "$(id -u)" = "0" ]; then
    for dir in /app/config /app/logs /app/trade_records /app/records; do
        mkdir -p "$dir" 2>/dev/null || true
    done

    # 只改挂载点这一层及其内容；失败不致命（可能是只读挂载或宿主机 NFS），
    # 代码里已对不可写目录做了降级处理，这里尽力而为即可。
    chown -R "${APP_UID}:${APP_GID}" \
        /app/config /app/logs /app/trade_records /app/records 2>/dev/null || \
        echo "[entrypoint] 警告：无法修改挂载目录属主，将以只读方式继续（配置仍可用，日志走标准输出）" >&2

    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$@"
    fi

    # 极端情况下没有 setpriv：退回 su-exec / gosu，否则直接以当前用户运行
    if command -v su-exec >/dev/null 2>&1; then
        exec su-exec "${APP_UID}:${APP_GID}" "$@"
    fi

    echo "[entrypoint] 警告：找不到 setpriv/su-exec，将以 root 运行" >&2
fi

exec "$@"
