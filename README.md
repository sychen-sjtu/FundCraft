# FundCraft

个人基金数据分析与红利低波策略看板，主要功能包括：

* 基金历史净值抓取与本地存储
* 基金收益率、最大回撤、波动率等指标计算
* 红利低波策略的择时评分与买入信号判断
* Supabase 云数据库读写与同步
* Streamlit 本地调试与云端部署展示

## 秘钥加密用法

如果你要把 `url` 和 `key` 以密文方式写入 `.streamlit/secrets.toml`，可以分别执行两次加密任务，每次只加密一个值：

```powershell
python -m src.security.encrypt_cli
```

脚本会依次要求你输入：

1. 需要加密的明文内容
2. 加密口令

然后输出一段可直接写入 `secrets.toml` 的密文。你分别对 `url` 和 `key` 运行两次即可。

示例配置格式如下：

```toml
[supabase]
url = "enc:..."
key = "enc:..."
```

解密时，程序会从 `.streamlit/secrets.toml` 读取密文，并根据口令自动解密。

## 基金代码配置

基金代码统一在 `.streamlit/secrets.toml` 的 `[funds]` 段配置，正式抓取任务会从这里读取：

```toml
[funds]
fund_codes = ["008163", "206018", "100018"]
```

## 登录与解密

`access_password` 用于登录校验，支持在 `.streamlit/secrets.toml` 中配置为列表，多个口令都可以通过验证：

```toml
[security]
access_password = ["123456", "654321"]
```

登录通过后，页面会再要求你输入单独的解密口令。只有输入解密口令后，程序才会开始读取和解密 Supabase 的 `url` 与 `key`。

## 正式任务运行方式

正式业务逻辑放在 `src/` 对应目录中，通过 `python -m` 运行：

* 阶段 2 抓取基金净值到本地：

```powershell
python -m src.fetchers.fetch_runner
```

* 阶段 3 本地指标分析与报告：

```powershell
python -m src.indicators.local_analysis
```

* 阶段 4 同步到 Supabase（会提示输入解密口令）：

```powershell
python -m src.storage.sync_runner
```

* 策略数据同步（分红 + cn_10y 利率 + 净值，自动判断全量初始化/增量补全并重算因子）：

```powershell
python -m src.storage.strategy_sync_runner                # 全部：所有基金 + cn_10y
python -m src.storage.strategy_sync_runner --entity fund --code 008163 --mode init
python -m src.storage.strategy_sync_runner --entity fund --code 008163 --mode incremental
python -m src.storage.strategy_sync_runner --entity rate --code cn_10y
```

`scripts/` 目录下保留的脚本仅用于临时测试与手动验证。

## 策略数据与派生因子

* **持久化原始数据**：基金净值（`fund_nav_history`）、基金分红（`fund_dividends`，008163 每月分红）、宏观利率（`macro_rates_history`，仅 `cn_10y` 中国10年期国债收益率）。
* **同步水位**（`sync_watermarks`）：记录每个实体已同步到的最大日期，作为「全量初始化 / 增量补全」的判定开关；增量补全从 `last_date - 10 天` 起重拉，兜底上游净值修正。
* **派生因子**（`fund_daily_factors`）：按「历史后不变 + 需拉全量表计算」两条件入库，包括合成股息率、年化波动率、最大回撤、历史分位与 A/B 策略得分。
* **合成股息率**：无现成每日股息率接口，用「过去 365 天累计分红 / 当日单位净值」合成（分红按除息日归属）。
* 建表/迁移脚本见 `sql/create_strategy_tables.sql`（幂等，可反复执行），设计说明见 `docs/数据持久化与增量同步设计方案.md`。

## Stage 5 本地展示运行方式

在项目根目录下直接启动 Streamlit：

```powershell
streamlit run .\app.py
```

启动后进入展示页，支持：

* 登录口令 + 解密口令门禁
* **分析时间范围**：侧边栏可选「近1年 / 近2年 / 近3年 / 近5年 / 全部」，**默认近 1 年**，避免每次全量拉取所有历史数据
* **数据刷新**：侧边栏「刷新数据」按钮，点击后按水位增量补全数据并重算因子，并显示最近一次刷新时间
* 基金总览、单基金明细、净值走势、回撤走势、同步状态与同步水位

## 数据库

https://supabase.com/