create table if not exists public.fund_profiles (
    fund_code text primary key,
    created_at timestamptz not null default now()
);

create table if not exists public.fund_nav_history (
    fund_code text not null,
    nav_date date not null,
    unit_nav numeric not null,
    daily_return numeric null,
    created_at timestamptz not null default now(),
    primary key (fund_code, nav_date)
);

create table if not exists public.sync_jobs (
    log_id text primary key,
    job_name text not null,
    status text not null,
    message text null,
    row_count integer not null default 0,
    executed_at timestamptz not null default now()
);