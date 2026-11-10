# 本地配置说明

本目录下的**运行时文件**默认已被 `.gitignore` 忽略，不会进入 Git 仓库。

仓库同样**不会上传**：`records/`（分析落盘）、`experience/`（经验库内容）、`logs/`、`trade_records/`（交易 CSV/截图）、`.env`、根目录临时图片与个人笔记等。仅源代码、`prompt_engineering/` 策略文本、`tests/` 与 `docs/` 说明文档会进入 GitHub。

## 首次使用

1. 复制模板为本地配置：

   ```cmd
   copy config\settings.example.json config\settings.json
   ```

2. 启动程序，在 **设置** 中填写你的 **API Key**。

   也可直接编辑 `config/settings.json` 中的 `base_url`、`model` 等字段。

3. `config/exception_state.json` 由程序在需要时自动创建，一般无需手动复制。结构可参考 `exception_state.example.json`。

## `settings.json` 字段说明

配置分为多个顶层组：`provider`、`general`、`prompt`、`validation`、`pushplus`、`dingtalk`、`binance`、`okx`、`auto_trade`、`tushare`。

### provider — AI 提供商

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider.model` | string | `"deepseek-flash"` | 模型名称（须与网关支持的名称一致） |
| `provider.base_url` | string | `"https://api.deepseek.com"` | OpenAI 兼容 API 根地址。DeepSeek：`https://api.deepseek.com`；火山方舟：`https://ark.cn-beijing.volces.com/api/v3`（模型自动轮换 + 额度用完自动降级，见下）；MiMo：`https://api.xiaomimimo.com/v1`（程序自动处理 `enable_thinking` 与 `reasoning_content` 回放） |
| `provider.api_key` | string | `""` | API Key（明文，内存中临时使用；不持久化到文件） |
| `provider.api_key_encrypted` | string | `""` | 加密后的 Key；留空表示未配置（通过 GUI 保存时自动加密写入） |
| `provider.thinking` | bool | `true` | 是否启用思考/推理类扩展参数（依模型与网关而定）。关闭可 3–5 倍提速但分析质量下降 |
| `provider.reasoning_effort` | string | `"high"` | 推理深度：`low` / `medium` / `high` / `max` |
| `provider.context_window` | int | `2000000` | 用于上下文占用提示的窗口大小（tokens） |

**火山方舟（Ark）网关的特殊行为**（`base_url` 含 `volces.com` 或 `volcengine` 时自动生效）：

- **模型别名映射**：`deepseek-flash` → `deepseek-v4-1-flash-economy-261017`、`deepseek` / `deepseek-chat` → `deepseek-v4-1-flash-261017`、`glm-flash` → `glm-5-3-flash-260828`、`doubao-flash` → `doubao-seed-2-1-lite-260915`。填别名即可，真实 ID 也直接可用。
- **模型池轮换**：每次分析自动换下一个模型（13 个模型的顺序见 `pa_agent/ai/ark_gateway.py` 的 `ARK_ROTATION_POOL`）。游标存在 `config/ark_rotation_state.json`，重启后继续；**只改内存，不改 settings.json**。
- **额度用完自动降级**：主模型失败时先把同网关其余 12 个模型逐个试完，再走公共免费池；账号级欠费（`AccountOverdue` / `arrears`）会直接跳过整条方舟链，不再逐个换模型白跑。
- **参数差异**：`thinking` 只发 `enabled` / `disabled`（不发 `adaptive`），强度固定 `low`（忽略 `reasoning_effort` 的 high/max）；`max_tokens` 上限 32768；**不发送 `temperature`**（部分模型只接受 `1`）。
- 工具栏会显示「方舟 思考: 开 · low · deepseek-v4-1-flash-economy-261017」，tooltip 说明轮换模型数。


**`ai_backups` — 用户自填的备用 AI 提供商**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `ai_backups[].name` | string | `""` | 显示名，只用于界面识别；留空时自动用模型名填充 |
| `ai_backups[].base_url` | string | — | OpenAI 兼容根地址，尾部 `/` 会被自动去掉 |
| `ai_backups[].model` | string | `"deepseek-flash"` | 模型名 |
| `ai_backups[].api_key` | string | `""` | 该提供商的 Key（明文） |
| `ai_backups[].enabled` | bool | `true` | 关掉后该项不参与尝试 |

- **尝试顺序**：主通道 → 方舟模型池（主通道是方舟时）→ GLM 备用（`provider.backup_glm_api_key`）→ **`ai_backups` 按数组顺序** → 公共免费池。
- 与 `provider.fallbacks` 分开存放：`fallbacks` 是运行时派生链（方舟池 / GLM 会被程序反复重写），手填的备用项必须独立存才不会被冲掉。链构建时两者合并，派生链在前。
- 只填了部分字段（缺 Base URL / 模型 / Key 之一）或 `enabled=false` 的条目会被跳过，不会发起请求。
- 界面入口：顶部菜单「AI 模型设置」→ 分组「备用 AI 提供商（按顺序尝试）」，支持 + 添加 / 上移 / 下移 / 删除 / 测试选中（测试会真发一次最小对话）。


### general — 通用设置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `general.last_data_source` | string | `"binance"` | K 线数据来源：`binance` / `okx`（GUI 下拉选项）；`ashare` / `akshare` / `tushare` / `eastmoney_futures` 仅代码支持。历史值 `mt5` / `tradingview` 会自动迁移为 `binance` |
| `general.last_symbol` | string | `"BTCUSDT"` | 默认品种，中性写法（如 `BTCUSDT`、`ETHUSDT`），OKX 也接受 `BTC-USDT` |
| `general.last_timeframe` | string | `"15m"` | 默认周期，如 `1m`、`5m`、`15m`、`1h`、`4h`、`1d` |
| `general.analysis_bar_count` | int | `100` | 提交分析时使用的 K 线数量（2–5000） |
| `general.refresh_interval_ms` | int | `1000` | 图表自动刷新间隔（毫秒） |
| `general.context_warning_threshold_pct` | float | `80.0` | 上下文占用警告阈值（百分比） |
| `general.decision_stance` | string | `"balanced"` | 阶段二交易倾向：`conservative` / `balanced` / `aggressive` / `extreme_aggressive` |
| `general.incremental_max_new_bars` | int | `10` | 增量分析触发阈值：新增已收盘 K 线 ≤ 此值时自动走增量模式（0–500） |
| `general.auto_resume_chart_after_analysis` | bool | `false` | 分析结束后是否自动恢复「图表实时更新」 |
| `general.keep_analysis` | bool | `false` | 持续跟踪分析：新 K 线收盘时自动触发新一轮分析 |
| `general.cancel_keep_analysis_on_retry` | bool | `false` | 校验失败触发重试后自动关闭 `keep_analysis` |
| `general.alert_on_order_opportunity` | bool | `true` | 阶段二给出交易方案时播放警报音、弹窗提示，并自动切换到「决策」页 |
| `general.decision_flow_auto_play` | bool | `true` | 决策树可视化自动播放 |
| `general.decision_flow_play_seconds` | int | `50` | 决策树可视化自动播放时长（秒） |
| `general.decision_flow_default_zoom_pct` | int | `600` | 决策树可视化默认缩放百分比（≥10） |
| `general.stream_pane_font_pt` | int | `11` | 「实时」页等宽字体字号（pt，8–28） |
| `general.chart_seq_label_font_pt` | int | `11` | K 线图上序号标签的字号（pt，6–24） |

### binance / okx — 交易所凭据

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `binance.api_key` | string | `""` | Binance API Key（行情不需要；连接测试与自动交易需要） |
| `binance.api_secret` | string | `""` | Binance API Secret |
| `binance.testnet` | bool | `false` | 是否走 Binance 测试网 |
| `okx.api_key` | string | `""` | OKX API Key |
| `okx.api_secret` | string | `""` | OKX API Secret |
| `okx.passphrase` | string | `""` | OKX Passphrase（创建 Key 时设置的，**不是**登录密码） |
| `okx.demo` | bool | `false` | 是否走 OKX 模拟盘（发 `x-simulated-trading: 1`） |

### dingtalk — 钉钉机器人

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `dingtalk.enabled` | bool | `false` | 是否启用钉钉推送 |
| `dingtalk.webhook_url` | string | `""` | 群机器人 Webhook 完整地址（`https://oapi.dingtalk.com/robot/send?access_token=...`） |
| `dingtalk.secret` | string | `""` | 加签密钥（机器人「加签」方式下的 `SEC` 开头串）；未加签留空 |
| `dingtalk.at_mobiles` | list[str] | `[]` | 需要 @ 的手机号列表 |
| `dingtalk.at_all` | bool | `false` | 是否 @ 所有人 |
| `dingtalk.notify_on_order_only` | bool | `true` | 仅在出下单信号时推送 |
| `dingtalk.msg_type` | string | `"markdown"` | 消息类型：`markdown` / `text` |

### auto_trade — 自动交易

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `auto_trade.enabled` | bool | `false` | 总开关。**默认关闭**，关闭时完全不评估 |
| `auto_trade.dry_run` | bool | `true` | 试运行：只把意图写进 `trade_records/auto_orders.jsonl`，不真正下单 |
| `auto_trade.venue` | string | `"binance"` | 下单交易所：`binance` / `okx` |
| `auto_trade.min_confidence` | int | `60` | 最低置信度门槛（低于则拒绝） |
| `auto_trade.max_notional_usdt` | float | `50.0` | 单笔最大名义金额（USDT），超过会被压到上限而不是拒绝 |
| `auto_trade.max_daily_orders` | int | `10` | 每日真实下单数上限（试运行不计入） |
| `auto_trade.allowed_symbols` | list[str] | `[]` | 交易对白名单，空表示不限制 |
| `auto_trade.require_stop_loss` | bool | `true` | 决策没给止损价时拒绝下单 |
| `auto_trade.market_type` | string | `"spot"` | 市场类型：`spot`（现货）/ `futures`（U 本位永续合约） |
| `auto_trade.margin_per_trade_usdt` | float | `10.0` | 每单保证金（USDT）。合约时「名义金额 = 保证金 × 杠杆」，优先于 `max_notional_usdt` |
| `auto_trade.leverage` | int | `1` | 杠杆倍数，夹到 1~125；`market_type="spot"` 时强制 1 |
| `auto_trade.order_type` | string | `"MARKET"` | 下单类型：`MARKET`（市价）/ `LIMIT`（限价，用决策里的入场价） |
| `auto_trade.margin_mode` | string | `"isolated"` | 保证金模式：`isolated`（逐仓）/ `cross`（全仓）。OKX 随订单 `tdMode` 传递 |
| `auto_trade.max_price_deviation_pct` | float | `0.3` | 跨所价差保护：行情源与下单所不同时，偏离超过该百分比就跳过本次下单。`0` = 关闭 |

### prompt — Prompt 组装调优

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `prompt.stage2_load_full_strategy_library` | bool | `false` | 阶段二是否加载全部 22 个策略文件（通常仅路由匹配的策略文件） |
| `prompt.experience_max_entries` | int | `3` | 经验库最大加载条目数（0–10） |
| `prompt.experience_max_chars_per_entry` | int | `400` | 每条经验最大字符数（100–4000） |
| `prompt.stage1_inject_pattern_briefs` | bool | `true` | 阶段一是否注入模式判定表和速查 brief（减少 missed tags） |

### validation — 校验与重试

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `validation.normalization_mode` | string | `"lenient"` | 归一化模式：`strict`（严格拒绝异常值）/ `lenient`（容忍轻微偏差） |
| `validation.stage1_coherence_checks` | bool | `false` | 阶段一跨字段一致性检查（闸门 trace、逐 K 摘要、模式标签等） |
| `validation.stage2_coherence_checks` | bool | `false` | 阶段二诊断与 trace 交叉检查 |
| `validation.trace_semantic_checks` | bool | `false` | 语义一致性检查（方向/信号逻辑冲突检测） |
| `validation.strict_bar_by_bar_features` | bool | `false` | 严格逐 K 特征校验（开启后对特征字段做严格验证） |
| `validation.disable_truncation_repair` | bool | `false` | 禁用流式 JSON 截断尾部修复 |
| `validation.retry_enabled` | bool | `true` | 校验失败时是否自动重试 |
| `validation.retry_max` | int | `3` | 格式错误（category a）最大重试次数（0–5） |
| `validation.retry_max_semantic` | int | `1` | 语义错误（category c）最大重试次数（0–3） |
| `validation.retry_stage2` | bool | `true` | 阶段二校验失败时是否重试 |

## 安全提醒

- **不要**将 `config/settings.json`、`config/exception_state.json` 提交到 Git。
- 交易所与钉钉凭据都保存在 `config/settings.json` 里，**明文存储**，请勿随手分享该文件。
- 若曾误提交 API Key，请立即在服务商处**作废并轮换**密钥。
- 建议在仓库根目录执行：`powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1`
