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

## 基金与指数配置（网页端管理）

关注列表由网页端 **「⭐ 基金配置」页** 管理，存在 Supabase 的 `fund_category` /
`fund_category_member` 两张表里。**运行期以库表为唯一来源**：按类别增删基金代码，
改完即生效，不用改文件、也不用重启。

首次部署分两步：

1. 在 Supabase SQL Editor 执行 `sql/schema.sql`（唯一的建表脚本，幂等，可反复执行）；
2. 执行一次 `python scripts/check_fund_config.py --seed`，把 `.streamlit/secrets.toml`
   里现成的配置灌进库表（只增不覆盖，可安全重复执行）。

`.streamlit/secrets.toml` 的 `[funds.categories.<类别>]` 从此只是**一次性迁移的输入**，
运行期完全不读它：**配置表没建、或数据库连不上，就是「没有基金」**，不会回退到文件配置。
格式（也是 seed 的输入）如下：

```toml
[funds.categories."红利低波"]
fund_codes = ["008163"]
panel = "红利低波"                    # 决定该类别基金的展示面板
index_codes = { "008163" = "H30269" } # 基金 -> 对应底层指数（用于拉取指数股息率）

[funds.categories."固收+"]
fund_codes = ["206018", "100018"]
panel = "固收+"
```

> 注意：类别名含中文，TOML 表头键需要用双引号包裹（`[funds.categories."红利低波"]`）。
> `panel` 可选值见 `src/storage/fund_config.py` 的 `PANEL_OPTIONS`（目前：`净值` / `固收+` / `债基` / `红利低波`），缺省为 `净值`。
> `index_codes`：基金对应的底层指数代码，用于拉取**指数股息率**作为策略因子（估值入库累积）。
> 新增基金后，请到「🗄️ 数据管理」执行一次基金层刷新，净值与档案才会入库。

### 市场指数条（同样在网页端管理）

总览页顶部的市场指数条由同一页的「📊 市场指数条」区管理（表 `ui_index_list`，
`list_key = market_indexes`）：加代码即加入、点移除即撤下，顺序即展示顺序。
同样**只从库表读**，配置表没建或数据库连不上就是空指数条。

> 注意：指数要显示行情，仍需在 `.streamlit/secrets.toml` 的 `[indexes.registry]` 里登记，
> 同步任务才会抓它的行情（未登记的指数在指数条上显示「暂无」）。
> seed 会把 `[ui.market_indexes].codes` 里的现有指数一并导入库表。

### 缓存与刷新

读取缓存有效期 12 小时（业务数据只在刷新时变化，刷新会主动清缓存）。
若数据是在别处（另一台机器 / 命令行）更新的，点「🗄️ 数据管理」页的
「🔄 重新读取服务器数据（清缓存）」即可立刻生效。

## 口令与数据访问

页面**无需登录**，只需在侧边栏输入「口令」并点击「读取数据」，程序会用该口令解密 `secrets.toml` 中的 Supabase `url` / `key` 并开始读取数据。

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

* **持久化原始数据**：基金净值（`fund_nav_history`）、基金分红（`fund_dividends`，008163 每月分红）、宏观利率（`macro_rates_history`，仅 `cn_10y` 中国10年期国债收益率）、**指数估值**（`index_valuation_history`，含指数 PE / 股息率）。
* **同步水位**（`sync_watermarks`）：记录每个实体已同步到的最大日期，作为「全量初始化 / 增量补全」的判定开关；增量补全从 `last_date - 10 天` 起重拉，兜底上游净值修正。
* **派生因子**（`fund_daily_factors`）：按「历史后不变 + 需拉全量表计算」两条件入库，包括指数股息率、年化波动率、最大回撤、历史分位与 A/B 策略得分。需要计算因子的基金由**类别面板派生**（`panel ∈ FACTOR_PANELS`，如「红利低波」）。
* **指数股息率（A2 混合口径，不污染数据库）**：策略的「股息率」因子 = **官方近期值 + 推导历史值**。
  - **官方值**（`stock_zh_index_value_csindex`）只返回近约 20 个交易日，走 `index_valuation_history` **入库累积**（source=csindex），是唯一落库的估值数据；
  - **推导历史值**（用「全收益/价格指数比」，如 H20269/H30269）在**内存中**补齐官方缺失的长历史（2013 至今），**不落库**；
  - 因子重算时两者合并、**官方值优先**，因此最近日期用官方口径，历史用推导近似（比官方约高 +0.3pp、更平滑）。
* 建表/迁移脚本见 `sql/schema.sql`（唯一的建表脚本，幂等，可反复执行），设计说明见 `docs/数据持久化与增量同步设计方案.md`。

## Stage 5 本地展示运行方式

在项目根目录下直接启动 Streamlit：

```powershell
streamlit run .\app.py
```

启动后进入展示页，支持：

* **口令访问**：侧边栏输入「口令」读取 Supabase 数据（无登录门禁）
* **分析时间范围**：侧边栏可选「近1年 / 近2年 / 近3年 / 近5年 / 全部」，**默认近 1 年**，避免每次全量拉取所有历史数据
* **数据刷新**：侧边栏「刷新数据」按钮，点击后按水位增量补全数据并重算因子，并显示最近一次刷新时间
* **总览**：只显示基金数量概览（各类别几只基金），不放详细数据
* **分类页 = 每基金一块看板**：显示基金名/代码/类别/类型/跟踪指数 + 近一周/一月收益；「红利低波」面板把**策略信号**放在最前面（A/B 得分、合成股息率、利差），净值/回撤与分红明细收进折叠区
* 同步状态与同步水位展示在「数据表」页

策略回测模块（`src/indicators/strategy_backtest.py`）已实现但暂未挂回页面，后续调参时再接入。

## 数据库

https://supabase.com/
