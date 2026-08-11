-- ============================================================================
-- FundCraft 数据库重建（ER 架构版）建表脚本
-- ============================================================================
-- 依据：docs/数据库重建-数据定义.md（数据定义与校验清单）
-- 用法：在 Supabase SQL Editor 中整段粘贴运行；幂等（IF NOT EXISTS），可反复执行。
--
-- 与旧库（create_supabase_tables.sql / create_strategy_tables.sql）的关键差异：
--   · fund_nav_history        : nav_date -> trade_date，新增 adjusted_nav（复权净值）
--   · index_valuation_history : pe1/pe2/dividend_yield1/2 -> pe_ttm/pe_lyr/dividend_yield
--   · index_daily_history     : 去掉 rolling_pe，新增 index_type(price/total_return)
--   · fund_daily_factors      -> index_daily_factors（策略因子下沉到指数层，index_code 主键）
--   · sync_watermarks/sync_jobs -> sync_watermark / sync_job
--   · 新增 index_master / fund_tracking_index（ER 实体 + 映射表 + 外键）
--   · 剔除：推导股息率、模拟基准/市场指数、区间收益等（见重建文档第三节剔除清单）
--
-- 说明：
--   · 本脚本为「重建」用途：新库直接全跑；旧库若已存在同名表会被 IF NOT EXISTS 跳过，
--     需要彻底重建时请先执行文末【可选】旧表清理段（务必先备份导出）。
--   · 时间序列统一 trade_date；来源统一 source（official / csindex / external_import）。
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 一、基金域（实体：基金产品）
-- ----------------------------------------------------------------------------

-- 1.1 基金档案（ETF 与场外基金合表，is_etf 区分）
create table if not exists public.fund_profiles (
    fund_code   text        primary key,
    fund_name   text        null,
    fund_type   text        null,               -- ETF / LOF / FOF / 场外开放式
    is_etf      boolean     not null default false,
    benchmark   text        null,               -- 业绩比较基准 / 跟踪标的
    source      text        not null default 'official',
    created_at  timestamptz not null default now()
);

-- 1.2 基金净值历史（单位净值 + 复权净值 + 日收益率）
create table if not exists public.fund_nav_history (
    fund_code    text        not null,
    trade_date   date        not null,
    unit_nav     numeric     not null,          -- 单位净值
    adjusted_nav numeric     null,              -- 复权净值（日增长率累乘推导）
    daily_return numeric     null,              -- 日收益率(%)
    source       text        not null default 'official',
    created_at   timestamptz not null default now(),
    primary key (fund_code, trade_date)
);

-- 1.3 基金分红
create table if not exists public.fund_dividends (
    fund_code         text        not null,
    ex_date           date        not null,     -- 除息日
    dividend_per_unit numeric     not null,     -- 每份分红(元)
    source            text        not null default 'official',
    created_at        timestamptz not null default now(),
    primary key (fund_code, ex_date)
);


-- ----------------------------------------------------------------------------
-- 二、指数域（实体：指数 + 行情 + 估值）
-- ----------------------------------------------------------------------------

-- 2.1 指数档案
create table if not exists public.index_master (
    index_code      text        primary key,
    index_name      text        null,
    index_category  text        not null,       -- strategy / benchmark / broad
    is_total_return boolean     not null default false,  -- 是否全收益指数
    exchange        text        null,           -- SSE / SZSE 等
    source          text        not null default 'csindex',
    created_at      timestamptz not null default now()
);

-- 2.2 指数日行情（统一一张表：价格指数 + 全收益指数，index_type 区分）
create table if not exists public.index_daily_history (
    index_code  text        not null,
    trade_date  date        not null,
    open        numeric     null,
    high        numeric     null,
    low         numeric     null,
    close       numeric     not null,           -- 收盘点位（价格 or 全收益）
    change_pct  numeric     null,               -- 涨跌幅(%)
    volume      numeric     null,               -- 成交量
    amount      numeric     null,               -- 成交额
    index_type  text        not null default 'price',   -- price / total_return
    source      text        not null default 'csindex',
    created_at  timestamptz not null default now(),
    primary key (index_code, trade_date)
);

-- 2.3 指数估值（统一：PE-TTM / PE-静态 / 股息率）
create table if not exists public.index_valuation_history (
    index_code     text        not null,
    trade_date     date        not null,
    pe_ttm         numeric     null,            -- 市盈率-TTM
    pe_lyr         numeric     null,            -- 市盈率-静态(LYR)
    dividend_yield numeric     null,            -- 股息率(%)
    source         text        not null default 'csindex',
    created_at     timestamptz not null default now(),
    primary key (index_code, trade_date)
);


-- ----------------------------------------------------------------------------
-- 三、关联表（实体间桥梁）
-- ----------------------------------------------------------------------------

-- 3.1 基金 -> 指数 映射（M:N；role 区分 strategy / benchmark）
create table if not exists public.fund_tracking_index (
    fund_code  text        not null,
    index_code text        not null,
    role       text        not null default 'strategy',   -- strategy / benchmark
    created_at timestamptz not null default now(),
    primary key (fund_code, index_code),
    -- 仅 index_code 建外键；fund_code 不建外键：fund_profiles 在后续基金同步阶段才入库，
    -- 若建外键会与「配置先入库（sync_config）」的同步顺序冲突；完整性由同一 TOML 配置源保证。
    foreign key (index_code) references public.index_master (index_code)
);


-- ----------------------------------------------------------------------------
-- 四、宏观 / 策略 / 运维域
-- ----------------------------------------------------------------------------

-- 4.1 宏观利率（cn_10y）
create table if not exists public.macro_rates_history (
    rate_code  text        not null,            -- cn_10y
    trade_date date        not null,
    rate_value numeric     null,                -- 收益率(%)
    source     text        not null default 'official',
    created_at timestamptz not null default now(),
    primary key (rate_code, trade_date)
);

-- 4.2 指数策略因子（派生，指数层；同一指数多基金共用信号）
create table if not exists public.index_daily_factors (
    index_code                text        not null,
    trade_date                date        not null,
    dividend_yield            numeric     null,  -- 指数股息率(%)
    annualized_volatility     numeric     null,  -- 年化波动率(%)
    max_drawdown              numeric     null,  -- 最大回撤(%)
    dividend_yield_percentile numeric     null,  -- 股息率历史分位(0-100)
    spread                    numeric     null,  -- 利差 = 股息率 - cn_10y
    spread_percentile         numeric     null,
    dy_vol_ratio_percentile   numeric     null,  -- (股息率/波动率)分位
    drawdown_percentile       numeric     null,
    volatility_percentile     numeric     null,
    score_a                   numeric     null,  -- A 策略综合得分
    signal_a                  boolean     null,
    score_b                   numeric     null,  -- B 策略综合得分
    signal_b                  boolean     null,
    created_at                timestamptz not null default now(),
    primary key (index_code, trade_date)
);

-- 4.3 同步水位（增量补全依据）
create table if not exists public.sync_watermark (
    entity_type text        not null,           -- fund / index / rate
    entity_code text        not null,
    last_date   date        not null,
    source      text        null,
    updated_at  timestamptz not null default now(),
    primary key (entity_type, entity_code)
);

-- 4.4 同步日志
create table if not exists public.sync_job (
    log_id      text        primary key,
    job_name    text        not null,
    status      text        not null,           -- success / partial / failed
    message     text        null,
    row_count   integer     not null default 0,
    executed_at timestamptz not null default now()
);


-- ----------------------------------------------------------------------------
-- 五、映射 / 注册表（不在 SQL 中硬编码，由 TOML 配置驱动）
-- ----------------------------------------------------------------------------
-- 指数注册表（index_master）与基金→指数映射（fund_tracking_index）属「配置」，
-- **不以 SQL 种子写入**，以 .streamlit/secrets.toml 为唯一配置源：
--   1. [indexes.registry]              —— 定义指数（名称/类别/是否全收益/交易所）
--   2. [funds.categories.*].index_codes —— 定义基金→指数映射
--   3. 由同步流程 src/storage/strategy_sync_runner.sync_config()
--      从 TOML 读取并 upsert 到 index_master / fund_tracking_index
-- 调整 TOML 后重跑同步即可生效，无需改表结构。


-- ----------------------------------------------------------------------------
-- 【可选】旧表清理（仅当要彻底重建、且已备份旧数据时执行；默认注释掉）
--     执行顺序：先删有外键/关联的表，再删实体表。
-- ----------------------------------------------------------------------------
-- drop table if exists public.fund_tracking_index;
-- drop table if exists public.fund_dividends;
-- drop table if exists public.fund_nav_history;
-- drop table if exists public.fund_profiles;
-- drop table if exists public.index_daily_factors;
-- drop table if exists public.index_valuation_history;
-- drop table if exists public.index_daily_history;
-- drop table if exists public.index_master;
-- drop table if exists public.macro_rates_history;
-- drop table if exists public.sync_watermark;
-- drop table if exists public.sync_job;
-- -- 旧命名表（若旧库还残留，一并清理；清理前务必导出备份）
-- drop table if exists public.fund_daily_factors;
-- drop table if exists public.sync_watermarks;
-- drop table if exists public.sync_jobs;
