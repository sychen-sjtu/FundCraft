-- ============================================================================
-- FundCraft 表结构核对查询（阶段 1：检查表是否正确）
-- 在 Supabase SQL Editor 整段运行，把结果贴回给助手比对即可。
-- 输出四块：表清单 / 字段明细 / 主键 / 外键。
-- 提示：SQL Editor 会为每条语句各出一个结果标签页，请逐个查看；
-- 也可直接运行下面【0. 单条合并查询（推荐）】一次拿到全部字段+主键。
-- ============================================================================

-- 0) 单条合并查询（推荐）：字段明细 + 主键，一个结果集
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

-- 1) 全部 public 表（检查是否有旧表残留，如 fund_daily_factors/sync_watermarks/sync_jobs）
select table_name
from information_schema.tables
where table_schema = 'public'
order by table_name;

-- 2) 逐表字段：列名 / 类型 / 可空 / 默认值
select table_name, ordinal_position, column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public'
order by table_name, ordinal_position;

-- 3) 主键
select tc.table_name, kcu.column_name, kcu.ordinal_position
from information_schema.table_constraints tc
join information_schema.key_column_usage kcu
  on tc.constraint_name = kcu.constraint_name
 and tc.table_schema = kcu.table_schema
where tc.constraint_type = 'PRIMARY KEY'
  and tc.table_schema = 'public'
order by tc.table_name, kcu.ordinal_position;

-- 4) 外键（fund_tracking_index 应有两条）
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
