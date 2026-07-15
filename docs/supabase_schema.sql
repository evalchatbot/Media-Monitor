-- ==========================================================================
-- Media Monitoring — Supabase / PostgreSQL schema
-- ==========================================================================
-- Run this in the Supabase SQL Editor (or `psql`) BEFORE pointing the app at
-- Supabase. Table + column names match the SQLAlchemy models exactly, so the
-- ORM works against these tables directly, and the app's create_all() is a
-- no-op afterwards (it only creates missing tables).
--
-- RLS note: tables created here have Row Level Security OFF by default, which
-- is what we want — the app connects with the Postgres/service credentials over
-- a direct connection, not the anon key, so it is a trusted server-side client.
-- Do NOT expose these tables through the public (anon) PostgREST API. If you
-- want defense-in-depth, enable RLS and add a service-role-only policy.
-- ==========================================================================

-- Keywords. `module` is kept for schema compat and is always 'newspaper' now;
-- every keyword is matched on BOTH newspaper websites and e-paper pages.
create table if not exists keywords (
    id          bigint generated always as identity primary key,
    text        varchar(255) not null,
    language    varchar(8)  not null default 'en',       -- 'en' | 'ur'
    module      varchar(16) not null default 'newspaper',
    active      boolean     not null default true,
    created_at  timestamptz not null default now(),
    constraint uq_keyword_text_lang_module unique (text, language, module)
);
create index if not exists ix_keywords_module_active on keywords (module, active);

-- Mentions — the shared detections table (website articles + e-paper pages).
create table if not exists mentions (
    id                   bigint generated always as identity primary key,
    module               varchar(16)  not null,          -- 'newspaper' | 'epaper'
    external_id          varchar(512) not null,          -- article url / paper:city:date:pN
    source               varchar(128) not null,          -- 'Dawn', 'Jang', ...
    section              varchar(128),
    title                text not null,
    url                  text not null,
    matched_keywords     jsonb not null default '[]'::jsonb,
    snippet              text,
    summary              text,
    relevance            varchar(32),                    -- Directly/Tangentially/Not Relevant
    sentiment            varchar(16),                    -- Positive/Critical/Neutral
    screenshot_path      text,
    full_screenshot_path text,
    deeplink_seconds     integer,                        -- legacy (unused)
    published_at         timestamptz,
    detected_at          timestamptz not null default now(),
    notified             boolean not null default false,
    constraint uq_mention_module_extid unique (module, external_id)
);
create index if not exists ix_mention_detected_at on mentions (detected_at desc);
create index if not exists ix_mention_module on mentions (module);
-- Fast keyword filtering (matched_keywords is a JSON array of strings):
create index if not exists ix_mention_keywords_gin on mentions using gin (matched_keywords);

-- Article cache — fetched article/video body text so keyword re-matching is
-- instant and never re-scrapes (module 'newspaper' or 'youtube').
create table if not exists article_cache (
    id          bigint generated always as identity primary key,
    module      varchar(16)  not null default 'newspaper',
    external_id varchar(512) not null,
    source      varchar(128) not null,
    section     varchar(128),
    title       text not null,
    url         text not null,
    body        text not null default '',
    fetched_at  timestamptz not null default now(),
    constraint uq_cache_module_extid unique (module, external_id)
);
create index if not exists ix_cache_fetched_at on article_cache (fetched_at);

-- E-paper pages — one row per page of a paper's daily print edition. The page
-- scan is an image; `ocr_text` holds its Claude-vision reading (done once),
-- and keyword matching runs on that text.
create table if not exists epaper_pages (
    id          bigint generated always as identity primary key,
    paper       varchar(32)  not null,                  -- slug: 'jang', 'dawn', ...
    source      varchar(128) not null,                  -- display: 'Jang', 'Dawn'
    city        varchar(32)  not null default 'lahore',
    date        varchar(10)  not null,                  -- 'YYYY-MM-DD'
    page_no     integer      not null,
    image_url   text         not null,
    image_path  text,
    viewer_url  text         not null default '',
    ocr_text    text         not null default '',
    ocr_status  varchar(16)  not null default 'pending', -- pending|done|failed|no_key
    regions     jsonb        not null default '[]'::jsonb, -- [{box:{l,t,r,b %}, text}] per article (image-map papers)
    fetched_at  timestamptz  not null default now(),
    constraint uq_epaper_page unique (paper, city, date, page_no)
);
create index if not exists ix_epaper_date on epaper_pages (date);

-- Migration from the YouTube-era schema (run once if upgrading):
--   drop table if exists transcripts;
--   drop table if exists youtube_channels;
--   delete from mentions      where module = 'youtube';
--   delete from article_cache where module = 'youtube';
--   delete from keywords      where module = 'youtube';

-- Scrape runs — audit trail (uptime + blocked-scrape alerts).
create table if not exists scrape_runs (
    id               bigint generated always as identity primary key,
    source           varchar(128) not null,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    status           varchar(16) not null default 'ok', -- 'ok' | 'blocked' | 'error'
    articles_found   integer not null default 0,
    mentions_created integer not null default 0,
    error            text
);
create index if not exists ix_scrape_started_at on scrape_runs (started_at desc);

-- ==========================================================================
-- Optional: enable RLS with a service-role-only policy (defense in depth).
-- Leave commented unless you understand the implications; the app uses the
-- direct Postgres connection, which bypasses RLS regardless.
-- ==========================================================================
-- alter table keywords      enable row level security;
-- alter table mentions      enable row level security;
-- alter table article_cache enable row level security;
-- alter table epaper_pages  enable row level security;
-- alter table scrape_runs   enable row level security;
-- (No policies added => only the service_role / postgres role can read/write.)
