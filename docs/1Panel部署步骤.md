# 在 1Panel 面板上部署（Docker 容器）

这份文档只讲 1Panel，通用细节见 [`服务器部署指南.md`](服务器部署指南.md)。

包内已包含你需要的全部内容：`Dockerfile`、`docker-compose.yml`、`deploy/docker-entrypoint.sh`、
`.env.example`、`pa_agent/`、`prompt_engineering/`、`config/settings.example.json`。
**不需要你另外装 Python，也不需要把 Key 写进配置文件**。

---

## 零、先确认一件事：服务器能不能访问交易所

PA Agent 只做出站访问，不监听任何端口，所以**不用在 1Panel 里放行端口**。但服务器必须能连上交易所：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://data-api.binance.vision/api/v3/time
curl -s -o /dev/null -w '%{http_code}\n' https://www.okx.com/api/v5/public/time
```

- 两个都返回 `200` → 直接用。
- 超时或报错 → 国内机房通常连不上，换香港 / 日本 / 新加坡的机器，或者按
  [代理](#七走代理可选) 一节配 `HTTPS_PROXY`。

---

## 一、上传并解压

1. 1Panel 左侧「主机」→「文件」，进入 `/opt`（没有就在根目录建一个 `/opt`）。
2. 点「上传」，传 `PA_Agent-server.zip`（或 `.tar.gz`）。
3. 右键压缩包 →「解压」→ 解压到 `/opt/pa-agent`。

解压后 `/opt/pa-agent/` 里应该直接能看到 `Dockerfile`、`docker-compose.yml`、`pa_agent/`。
如果多套了一层 `PA_Agent-server/`，把里面的内容全部剪切到 `/opt/pa-agent/` 即可。

---

## 二、建 .env（所有密钥都放这里）

1Panel「主机」→「文件」→ 进 `/opt/pa-agent` → 右键 `.env.example` →「复制」→ 命名为 `.env`。
然后编辑 `.env`，至少填这几行：

```ini
# ── AI 模型（必填）────────────────────────────
PA_AGENT_API_KEY=你的APIKey
PA_AGENT_API_BASE=https://ark.cn-beijing.volces.com/api/v3
PA_AGENT_MODEL=kimi-k3-1-pro-261024

# ── 行情（必填）──────────────────────────────
PA_DAEMON_DATA_SOURCE=binance
PA_DAEMON_SYMBOL=BTCUSDT
PA_DAEMON_TIMEFRAME=15m

# ── 钉钉通知（可选）──────────────────────────
DINGTALK_WEBHOOK_URL=
DINGTALK_SECRET=

# ── 备用 GLM 通道（可选，仅方舟/SenseNova 主通道时生效）──
# 智谱 glm-4-flash，免费额度；模型名与 Base URL 写死在代码里，只填 Key
# 也认 GLM_API_KEY / ZHIPU_API_KEY 两个旧名
# PA_AGENT_GLM_API_KEY=

# ── 交易所凭据（只做行情可以不填）──────────────
BINANCE_API_KEY=
BINANCE_API_SECRET=
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=
OKX_DEMO=false

# ── 自动交易（先保持试运行）──────────────────
PA_DAEMON_AUTO_TRADE=false
PA_DAEMON_DRY_RUN=true
PA_DAEMON_VENUE=okx
```

> **关键设计**：AI Key、钉钉、交易所凭据、自动交易三个开关都支持环境变量，
> 所以容器里**不需要挂载含密钥的 `settings.json`**。
> 但**数字风控**（最低置信度、单笔上限、每日单数、杠杆、保证金模式、价差保护）
> 刻意只能写在 `settings.json` 里——避免 `.env` 拼错一个字就放宽风控。

设完后把 `.env` 权限收紧：右键 `.env` →「权限」→ 改成 `600`。

---

## 三、用「编排」启动

> ⚠️ **这里有个坑**：1Panel 会把编排文件存到它自己的目录
> `/opt/1panel/docker/compose/pa-agent/`，然后**在那个目录里**执行
> `docker compose`。所以 `env_file: .env`、`build: context: .`、
> `volumes: ./config` 这些相对路径全部会指向 1Panel 的目录，而不是你的
> `/opt/pa-agent`，直接报：
>
> ```
> env file /opt/1panel/docker/compose/pa-agent/.env not found
> ```
>
> **所以给 1Panel 用的必须是包里那个 `docker-compose.1panel.yml`**
> （里面所有路径都写成了绝对路径 `/opt/pa-agent/...`）。

1. 1Panel 左侧「容器」→「编排」→「创建编排」。
2. 名称填 `pa-agent`。
3. 在「编排内容」里**原样粘贴 `docker-compose.1panel.yml` 的内容**（注意不是
   `docker-compose.yml`）。
4. 点「确认」，1Panel 会自动 `docker compose up -d --build`。
5. 首次构建约 2–4 分钟（装 Python 依赖）。

> **如果你把包解压到了别的位置**（比如 `/www/pa-agent`），必须把
> `docker-compose.1panel.yml` 里所有 `/opt/pa-agent` 替换成你的实际路径，否则
> 依然会找不到 `.env`。

> 如果更习惯命令行，在 1Panel「主机」→「终端」里执行：
> ```bash
> cd /opt/pa-agent && docker compose -f docker-compose.1panel.yml up -d --build
> ```
> 命令行这样跑是没问题的；只有在 1Panel 界面里贴编排内容时，才必须用绝对路径版。

---

## 四、看它活没活

```bash
docker logs -f pa-agent
```

正常启动会依次打印：

```
连接数据源 binance …
数据源就绪：BinanceSource
分析引擎就绪（provider=…）
守护进程启动：source=binance symbol=BTCUSDT tf=15m bars=100 poll=20s …
```

健康检查（容器自带 `HEALTHCHECK`，也可以手动跑）：

```bash
docker exec pa-agent python -m pa_agent.daemon --health-check
```

看到 `结果：健康 ✅` 就是通的。再跑一次单次分析，确认 AI 也能用：

```bash
docker exec pa-agent python -m pa_agent.daemon --once
```

成功后钉钉会收到一条消息（如果配了钉钉）。

---

## 五、目录属主不用你操心

`config / logs / trade_records / records` 这四个目录是从宿主机挂进容器的。
1Panel 建目录时属主是 `root`，而容器里程序用 `uid 10001` 跑，直接写会 `Permission denied`。

`Dockerfile` 里已改成**入口脚本先以 root 修正属主、再降权运行**：

```
deploy/docker-entrypoint.sh → mkdir -p → chown -R 10001:10001 → setpriv 降权 → 启动守护进程
```

所以**你不需要手写 `chown`**。如果宿主机上这些目录归别的用户（比如 `1000`），
在 `.env` 里加 `PA_AGENT_UID=1000` 和 `PA_AGENT_GID=1000` 即可。

万一 chown 失败（比如挂载了只读卷），程序不会崩：日志会退到 `docker logs` 输出，
配置用内存中的默认值继续跑，只是配置改动不会落盘。

---

## 六、想用真实下单时，按顺序做四件事

1. **先在交易所建 API Key**，权限只勾「读取 + 现货/合约交易」，**不要勾提现**。
2. **把服务器公网 IP 加进 Key 的 IP 白名单**（Binance / OKX 后台都有这一项）。
   如果你走代理，白名单要填**代理的出口 IP**，不是服务器自己的 IP。
3. `.env` 里填凭据，先只改 `PA_DAEMON_AUTO_TRADE=true`，**`PA_DAEMON_DRY_RUN` 保持 `true`**，
   重启容器观察一天，看 `trade_records/auto_orders.jsonl` 里的记录合不合理。
4. 确认没问题后，把 `PA_DAEMON_DRY_RUN` 改成 `false` 重启，才算真的开始下单。

想调单笔上限/杠杆/保证金模式，就编辑宿主机上的 `config/settings.json`
（容器首次启动会用 `settings.example.json` 生成一份），改完重启容器：

```bash
docker compose restart
```

---

## 七、走代理（可选）

容器里的 `127.0.0.1` 指的是**容器自己**，不是宿主机。所以代理地址必须写成宿主机在内网里的 IP：

```ini
HTTPS_PROXY=http://192.168.1.50:7890
NO_PROXY=oapi.dingtalk.com,www.pushplus.plus,api.deepseek.com,ark.cn-beijing.volces.com
```

`NO_PROXY` 别忘了加钉钉和 AI 的域名，否则通知和模型调用会绕一大圈。
如果代理装在宿主机上，Linux 下还需要给容器加 `host.docker.internal` 映射（compose 里加
`extra_hosts: ["host.docker.internal:host-gateway"]`）。

**代理下单有个真实风险**：代理链路抖动时，可能出现「请求发出去了、交易所成交了、但响应没回来」，
程序会认为失败。所以走代理时更要在 `dry_run` 下多观察几天。

---

## 八、备份与升级

**备份**（决策记录、下单历史、图表都在这两个目录里）：

```bash
cd /opt/pa-agent && tar -czf ~/pa-agent-backup-$(date +%F).tar.gz config trade_records records
```

**升级**：上传新的 `PA_Agent-server.zip` 解压覆盖，保留原来的 `.env` 和 `config/`，
然后 `docker compose up -d --build`。

---

## 九、常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `docker logs` 里报 `Permission denied` | 检查 `.env` 里 `PA_AGENT_UID` 是否与宿主机目录属主一致 |
| 一直 `行情自检失败` | 服务器连不上交易所，先跑第四节开头那两条 `curl` |
| 健康检查 `unhealthy` | 看日志里的具体报错；多数是 AI Key 没填或网络不通 |
| 钉钉收不到 | `DINGTALK_WEBHOOK_URL` 是否含 `access_token`；配了 `DINGTALK_SECRET` 就必须签名正确 |
| 内存占用 | 实测约 **38 MB**，1C1G 足够，512M 也能跑 |
