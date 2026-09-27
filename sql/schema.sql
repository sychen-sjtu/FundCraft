-- ============================================================================
-- FundCraft 数据库结构脚本（唯一维护的建表 / 迁移脚本）
-- ============================================================================
-- 用法：在 Supabase SQL Editor 中整段粘贴运行。幂等（IF NOT EXISTS / ADD COLUMN
--       IF NOT EXISTS），可反复执行；只需维护这一个文件，不用再按顺序跑多个脚本。
--
-- 本文件已合并以下旧脚本（均已删除，历史版本可在 git 记录中找回）：
--   · create_supabase_tables.sql   原「3 张初始表」
--   · create_strategy_tables.sql   原「策略扩展 + fund_snapshot_metrics」
--   · create_rebuild_tables.sql    原「ER 架构重建版建表」
--   · create_fund_config_tables.sql 原「网页端基金配置表」
--   · verify_schema_query.sql      原「表结构核对查询」→ 见文末第五节
--
-- 表清单（15 张，与代码实际访问的表一一对应）：
--   基金域   fund_profiles / fund_nav_history / fund_dividends
--   指数域   index_master / index_daily_history / index_valuation_history
--   关联表   fund_tracking_index
--   宏观策略 macro_rates_history / index_daily_factors
--   运维     sync_watermark / sync_job / fund_snapshot_metrics
--   配置域   fund_category / fund_category_member / ui_index_list
--
-- 三种库的处理方式：
--   1) 全新库   ：整段运行即可建全 15 张表。
--   2) 已有本结构：整段运行即可（只补缺失的表/列，不动已有数据）。
--   3) 旧列名库 ：若曾用旧脚本在空库上建过表（fund_nav_history.nav_date、
--      macro_rates_history.rate_date、index_valuation_history.pe1/pe2、
--      index_daily_history.rolling_pe、fund_daily_factors、sync_watermarks、
--      sync_jobs 等），CREATE TABLE IF NOT EXISTS 会跳过这些旧表，导致结构不符。
--      请先备份，再执行文末【旧结构清理段】，然后重跑本脚本。
--
-- 约定：时间序列统一 trade_date；来源统一 source（official / csindex /
--       external_import / bond_zh_us_rate）；字段注释即口径说明。
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

-- 1.3 基金分红（除息日 + 每份分红；累计净值 = 单位净值 + 累计每份分红）
create table if not exists public.fund_dividends (
    fund_code         text        not null,
    ex_date           date        not null,     -- 除息日（= 权益登记日）
    dividend_per_unit numeric     not null,     -- 每份分红（元）
    source            text        not null default 'official',
    created_at        timestamptz not null default now(),
    primary key (fund_code, ex_date)
);


-- ----------------------------------------------------------------------------
-- 二、指数域（实体：指数 + 行情 + 估值）
-- ----------------------------------------------------------------------------

-- 2.1 指数档案（指数注册表；条目由同步流程按配置写入，SQL 不硬编码）
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
    -- 若建外键会与「配置先入库（sync_config）」的同步顺序冲突，完整性由配置源保证。
    foreign key (index_code) references public.index_master (index_code)
);


-- ----------------------------------------------------------------------------
-- 四、宏观 / 策略 / 运维域
-- ----------------------------------------------------------------------------

-- 4.1 宏观利率（cn_10y 国债收益率；国债期货 TF/T 也走这张表）
create table if not exists public.macro_rates_history (
    rate_code  text        not null,            -- cn_10y / bond_futures_tf / bond_futures_t
    trade_date date        not null,
    rate_value numeric     null,                -- 收益率 / 收盘价(%)
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

-- 4.5 基金低频快照指标（akshare 派生：规模日更 / 持仓季度更 / 净值派生指标 24h 缓存）
--     用途：总览对比表与详情页核心指标直接读库，避免冷缓存时实时调 akshare 拉全历史
create table if not exists public.fund_snapshot_metrics (
    fund_code            text        not null,
    fund_scale           numeric     null,   -- 基金规模（亿元）
    scale_updated_at     timestamptz null,   -- 规模抓取时间
    bond_report_period   text        null,   -- 债券持仓报告期（如 2026年2季度）
    bond_categories      jsonb       null,   -- 债券类别相对占比 [{label,pct}]
    bond_nav_pct         jsonb       null,   -- 债券类别占净值 [{label,pct}]
    bond_total_nav_pct   numeric     null,   -- 披露债券持仓占净值合计(%)
    bond_count           integer     null,   -- 披露债券只数
    bond_no_stock        boolean     null,   -- 是否不含股票
    bond_has_convertible boolean     null,   -- 是否含可转债
    holdings_updated_at  timestamptz null,   -- 持仓抓取时间
    fund_metrics         jsonb       null,   -- 净值派生指标（年化/回撤/卡玛/年限）
    fund_metrics_updated_at timestamptz null,
    bond_metrics         jsonb       null,   -- 债基派生指标（最大回撤/修复天数）
    bond_metrics_updated_at timestamptz null,
    updated_at           timestamptz not null default now(),
    primary key (fund_code)
);


-- ----------------------------------------------------------------------------
-- 五、配置域（关注列表；网页端「⭐ 基金配置」页读写）
-- ----------------------------------------------------------------------------
-- 说明：基金类别与关注列表存在这两张表里，由网页端增删；.streamlit/secrets.toml 的
--       [funds.categories] 退化为「首次导入的种子 + 表不可用时的兜底」。
--       同步任务与页面读同一份配置，因此网页上加的基金会被刷新任务拉到。

-- 5.1 基金类别（决定总览页分组顺序、详情页展示面板）
create table if not exists public.fund_category (
    category_name text        primary key,
    panel         text        not null default '净值',   -- 净值 / 固收+ / 债基 / 红利低波
    sort_order    integer     not null default 100,      -- 展示顺序（小的在前）
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- 5.2 类别成员（= 需要关注的基金；删掉一行即不再关注）
--     注意：index_code 刻意不建外键（与 fund_tracking_index 同理）——「先加基金、后登记
--     指数」的顺序会与外键冲突；引用完整性由同步时的自动补登记保证。
create table if not exists public.fund_category_member (
    category_name text        not null,
    fund_code     text        not null,
    index_code    text        null,                      -- 对应策略底层指数（可空）
    sort_order    integer     not null default 100,      -- 类别内展示顺序（小的在前）
    created_at    timestamptz not null default now(),
    primary key (category_name, fund_code),
    foreign key (category_name) references public.fund_category (category_name) on delete cascade
);

-- 5.3 查询索引（按类别取成员 / 反查某只基金属于哪些类别）
create index if not exists idx_fund_category_member_fund
    on public.fund_category_member (fund_code);

-- 5.4 界面指数列表（当前用途：总览页顶部市场指数条；list_key 区分用途，
--     预留 compare_indexes 供详情页对比下拉使用）。顺序 = sort_order 升序。
create table if not exists public.ui_index_list (
    list_key   text        not null,                      -- market_indexes / compare_indexes
    index_code text        not null,
    sort_order integer     not null default 100,
    created_at timestamptz not null default now(),
    primary key (list_key, index_code)
);


-- ----------------------------------------------------------------------------
-- 六、旧库补列迁移（幂等；已有列自动跳过）
-- ----------------------------------------------------------------------------
-- 场景：若 fund_snapshot_metrics 是本文件之前建的（缺 4 个衍生指标列），
--       靠这几条补齐，不必重建表、不动数据。
alter table public.fund_snapshot_metrics
    add column if not exists fund_metrics jsonb;
alter table public.fund_snapshot_metrics
    add column if not exists fund_metrics_updated_at timestamptz;
alter table public.fund_snapshot_metrics
    add column if not exists bond_metrics jsonb;
alter table public.fund_snapshot_metrics
    add column if not exists bond_metrics_updated_at timestamptz;
-- 类别成员展示顺序（网页端「⭐ 基金配置」支持上下移动；老库补列后按 created_at 兜底排序）
alter table public.fund_category_member
    add column if not exists sort_order integer not null default 100;


-- ----------------------------------------------------------------------------
-- 【可选】旧结构清理段（默认注释；仅当确认要丢弃旧数据并重建时执行）
--   删除顺序：先删有外键/关联的表，再删实体表。
--   ⚠️ 执行前务必先导出备份；执行后再整段运行本文件重建。
-- ----------------------------------------------------------------------------
-- drop table if exists public.fund_category_member;
-- drop table if exists public.fund_category;
-- drop table if exists public.ui_index_list;
-- drop table if exists public.fund_tracking_index;
-- drop table if exists public.fund_dividends;
-- drop table if exists public.fund_nav_history;
-- drop table if exists public.fund_profiles;
-- drop table if exists public.index_daily_factors;
-- drop table if exists public.index_valuation_history;
-- drop table if exists public.index_daily_history;
-- drop table if exists public.index_master;
-- drop table if exists public.macro_rates_history;
-- drop table if exists public.fund_snapshot_metrics;
-- drop table if exists public.sync_watermark;
-- drop table if exists public.sync_job;
-- -- 旧命名表（存在即说明是旧结构；确认无用后再删）
-- drop table if exists public.fund_daily_factors;
-- drop table if exists public.sync_watermarks;
-- drop table if exists public.sync_jobs;


-- ----------------------------------------------------------------------------
-- 【可选】只读校验查询（不写数据；排查结构问题时单独选中执行即可）
-- ----------------------------------------------------------------------------

-- 7.1 字段明细 + 主键（推荐：一个结果集看清全部表结构）
select c.table_name,
       c.column_name,
       c.data_type,
       c.is_nullable,
       c.column_default,
       coalesce(pk.pk_cols, '') as primary_key
from information_schema.columns c
left join (
    select kcu.table_name,
           string_agg(kcu.column_name, ',' order by kcu.ordinal_position) as pk_cols
    from information_schema.table_constraints tc
    join information_schema.key_column_usage kcu
      on tc.constraint_name = kcu.constraint_name
     and tc.table_schema = kcu.table_schema
    where tc.constraint_type = 'PRIMARY KEY'
      and tc.table_schema = 'public'
    group by kcu.table_name
) pk on pk.table_name = c.table_name
where c.table_schema = 'public'
order by c.table_name, c.ordinal_position;

-- 7.2 全部 public 表（检查是否有旧命名表残留）
select table_name
from information_schema.tables
where table_schema = 'public'
order by table_name;

-- 7.3 外键（正常应只看到 fund_tracking_index→index_master 与
--     fund_category_member→fund_category 两组）
select tc.table_name, kcu.column_name,
       ccu.table_name as ref_table, ccu.column_name as ref_column
from information_schema.table_constraints tc
join information_schema.key_column_usage kcu
  on tc.constraint_name = kcu.constraint_name
 and tc.table_schema = kcu.table_schema
join information_schema.constraint_column_usage ccu
  on tc.constraint_name = ccu.constraint_name
 and tc.table_schema = ccu.table_schema
where tc.constraint_type = 'FOREIGN KEY'
  and tc.table_schema = 'public'
order by tc.table_name, kcu.column_name;
