# syntax=docker/dockerfile:1
#
# PA Agent 无头守护进程镜像
# ------------------------------------------------
# 这个镜像里**没有** PyQt6：服务器不需要图形界面，装 Qt 只会让镜像
# 从 ~180MB 涨到 ~600MB，还得额外装一堆图形库。
#
# 构建：
#   默认（走清华 pip 源，国内可用）：
#     docker build -t pa-agent:latest .
#   基础镜像也拉不动时（Docker Hub 被墙 / 加速器失效），把基础镜像换成镜像站：
#     docker build -t pa-agent:latest \
#       --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
# 运行（见 docs/服务器部署指南.md）：
#   docker run -d --name pa-agent --env-file .env -v "$PWD/config:/app/config" \
#       -v "$PWD/trade_records:/app/trade_records" -v "$PWD/logs:/app/logs" \
#       pa-agent:latest

# 基础镜像可用 --build-arg 覆盖，方便在无法直连 Docker Hub 的机器上构建。
# 注意：docker compose 里请用 compose 的 `build.args` 传，不要加 --build-arg。
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# pip 源。默认清华镜像（国内外均可访问、完整同步 PyPI）。
# 想用官方源：--build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 换源时的备用源（主源 404 某个包时 pip 会依次尝试），留空表示不用。
ARG PIP_EXTRA_INDEX_URL=

# apt 源。留空 = 用 Debian 官方源（deb.debian.org）。
# 国内构建 apt 卡住时传：--build-arg APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
ARG APT_MIRROR=

WORKDIR /app

# ── 系统依赖 ──────────────────────────────────────────────────────────────────
# tzdata 让容器里的本地时间正确（K线时间、日志时间都依赖它）；
# ca-certificates 是访问 Binance / OKX / 钉钉 / DeepSeek 的 HTTPS 前提；
# util-linux 提供 setpriv，入口脚本靠它从 root 降权到普通用户。
# 传了 APT_MIRROR 就先改源（Debian 12 用 deb822 的 debian.sources，老镜像用 sources.list，两者都试）。
RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|http://deb.debian.org|${APT_MIRROR}|g; s|http://security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
        sed -i "s|http://deb.debian.org|${APT_MIRROR}|g; s|http://security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# ── Python 依赖 ──────────────────────────────────────────────────────────────
# 先只拷依赖清单，让 docker layer 缓存能复用（改代码不用重装依赖）。
# pip 会自动读取环境变量 PIP_INDEX_URL / PIP_EXTRA_INDEX_URL，
# 上面两个 ARG 在 RUN 阶段就是环境变量，所以这里不用再拼参数。
COPY requirements-daemon.txt ./requirements-daemon.txt
RUN pip install --no-cache-dir -r requirements-daemon.txt

# ── 应用代码 ─────────────────────────────────────────────────────────────────
COPY pa_agent ./pa_agent
COPY prompt_engineering ./prompt_engineering
COPY experience ./experience
COPY config/settings.example.json ./config/settings.example.json

# ── 入口脚本 ─────────────────────────────────────────────────────────────────
# 镜像以 root 启动 → 入口脚本修正挂载目录属主 → setpriv 降权到 10001 再跑。
# 这样宿主机上属主为 root 的挂载目录（1Panel 里就是这样）也能正常写入。
# 不改属主也行：代码对不可写目录做了降级（配置用内存默认值、日志走 stdout）。
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# 运行时目录（挂载点）。用卷覆盖，保证容器重建后记录不丢。
RUN mkdir -p /app/config /app/logs /app/trade_records /app/records/pending \
    && groupadd --gid 10001 paagent \
    && useradd --create-home --uid 10001 --gid 10001 paagent \
    && chown -R paagent:paagent /app

# ── 健康检查 ─────────────────────────────────────────────────────────────────
# --health-check 会真的拉一次行情，所以 interval 放宽到 5 分钟，避免打爆交易所限频。
# 用 setpriv 降权执行：否则健康检查以 root 跑，会把 logs/ 里的日志文件
# 变成 root 属主，反而让主进程（10001）写不进去。
HEALTHCHECK --interval=5m --timeout=60s --start-period=60s --retries=3 \
    CMD setpriv --reuid="${PA_AGENT_UID}" --regid="${PA_AGENT_GID}" --init-groups \
        python -m pa_agent.daemon --health-check || exit 1

# ── 默认参数 ─────────────────────────────────────────────────────────────────
# 覆盖示例：docker run ... pa-agent:latest --symbol ETHUSDT --timeframe 1h
ENV PA_DAEMON_LOG_LEVEL=INFO \
    PA_DAEMON_POLL_INTERVAL=20 \
    PA_DAEMON_ANALYSIS_COOLDOWN=120 \
    PA_DAEMON_WAIT_BAR_CLOSE=true \
    PA_AGENT_UID=10001 \
    PA_AGENT_GID=10001

# 注意：这里刻意不写 USER。入口脚本需要 root 才能 chown 挂载目录，
# 之后自己降权到 PA_AGENT_UID。若用 --user 覆盖启动，脚本会检测到
# 非 root 而跳过 chown，直接 exec 目标命令（此时需自行保证目录可写）。
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "python", "-m", "pa_agent.daemon"]
CMD []
