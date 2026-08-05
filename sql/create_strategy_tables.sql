-- ============================================================================
-- FundCraft 策略相关建表 / 迁移脚本（幂等，可反复执行）
-- ============================================================================
-- 用法：在 Supabase SQL Editor 中整段粘贴运行。
--
-- 设计原则（重要）：
--   1) 对【已存在的表】做结构扩展，一律使用
--        ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...
--      只新增列、不重建表、不动历史数据（如 fund_profiles 扩展字段）。
--   2) 对【新表】使用
--        CREATE TABLE IF NOT EXISTS ...
--      表已存在时自动跳过，不会报"表已存在"错误。
--   3) 因此本脚本既可跑在全新库上，也可安全跑在已有库上。
--
-- 说明：原始三张表（fund_profiles / fund_nav_history / sync_jobs）
--       由 sql/create_supabase_tables.sql 创建；本脚本只负责扩展与新增。
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 一、已有表的结构扩展（ALTER，保留历史数据）
-- ----------------------------------------------------------------------------

-- 1.1 fund_profiles：补充基金基础信息（名称 / 类型 / 跟踪指数）
--     008163=南方标普红利低波50ETF联接A(指数型-股票,跟踪标普红利低波50)
--     206018/100018=债券型基金（现金池）
alter table public.fund_profiles
    add column if not exists fund_name text;

alter table public.fund_profiles
    add column if not exists fund_type text;

alter table public.fund_profiles
    add column if not exists tracking_index text;

-- 1.2 fund_nav_history：主键 (fund_code, nav_date) 已满足按基金+日期查询，
--     无需结构变更。

-- 1.3 sync_jobs：现有结构已够用（job_name 区分初始化/增量/因子重算），
--     无需结构变更。


-- ----------------------------------------------------------------------------
-- 二、新增表
-- ----------------------------------------------------------------------------

-- 2.1 基金分红（008163 自 2023-12 起每月分红）
create table if not exists public.fund_dividends (
    fund_code          text        not null,
    ex_date            date        not null,   -- 除息日（= 权益登记日）
    dividend_per_unit  numeric     not null,   -- 每份分红（元）
    created_at         timestamptz not null default now(),
    primary key (fund_code, ex_date)
);

-- 2.2 宏观利率（当前只存 cn_10y：中国10年期国债收益率）
create table if not exists public.macro_rates_history (
    rate_code   text        not null,          -- 如 cn_10y
    rate_date   date        not null,
    rate_value  numeric     null,              -- 收益率（%）
    source      text        not null default 'bond_zh_us_rate',
    created_at  timestamptz not null default now(),
    primary key (rate_code, rate_date)
);

-- 2.3 同步水位（增量补全的依据 / 初始化判定开关）
create table if not exists public.sync_watermarks (
    entity_type text        not null,          -- fund / rate
    entity_code text        not null,          -- 008163 / cn_10y
    last_date   date        not null,          -- 已成功同步的最大日期
    source      text        null,              -- 数据来源标记
    updated_at  timestamptz not null default now(),
    primary key (entity_type, entity_code)
);

-- 2.4 派生因子（日频，满足"历史后不变 + 需拉全量表计算"两条件故入库）
create table if not exists public.fund_daily_factors (
    fund_code             text        not null,
    trade_date            date        not null,
    dividend_yield        numeric     null,    -- 指数股息率(%)
    annualized_vol        numeric     null,    -- 年化波动率(%)
    max_drawdown          numeric     null,    -- 最大回撤(%)
    dividend_yield_pctile numeric     null,    -- 股息率历史分位(0-100)
    spread                numeric     null,    -- 股息率-10Y利差(%)
    spread_pctile         numeric     null,
    dy_vol_ratio_pctile   numeric     null,    -- (股息率/波动率)分位
    drawdown_pctile       numeric     null,
    vol_pctile            numeric     null,
    score_a               numeric     null,    -- A策略综合得分
    signal_a              boolean     null,
    score_b               numeric     null,    -- B策略综合得分
    signal_b              boolean     null,
    created_at            timestamptz not null default now(),
    primary key (fund_code, trade_date)
);


-- ----------------------------------------------------------------------------
-- 三、预留表（当前策略不启用，供未来指数级数据使用）
-- ----------------------------------------------------------------------------

-- 3.1 指数日行情（预留）
create table if not exists public.index_daily_history (
    index_code   text        not null,
    trade_date   date        not null,
    open         numeric     null,
    high         numeric     null,
    low          numeric     null,
    close        numeric     not null,
    change_pct   numeric     null,
    volume       numeric     null,
    amount       numeric     null,
    rolling_pe   numeric     null,
    source       text        not null default 'csindex',
    created_at   timestamptz not null default now(),
    primary key (index_code, trade_date)
);

-- 3.2 指数估值（预留）
create table if not exists public.index_valuation_history (
    index_code      text        not null,
    trade_date      date        not null,
    pe1             numeric     null,
    pe2             numeric     null,
    dividend_yield1 numeric     null,
    dividend_yield2 numeric     null,
    source          text        not null default 'csindex',
    created_at      timestamptz not null default now(),
    primary key (index_code, trade_date)
);
