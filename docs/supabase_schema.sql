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

-- Keywords. `module` is 'newspaper' (websites + e-paper) or 'youtube'.
-- Separate watchlists — YouTube keywords do not affect newspaper scans.
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

-- Mentions — the shared detections table (website articles + e-paper pages + YouTube).
create table if not exists mentions (
    id                   bigint generated always as identity primary key,
    module               varchar(16)  not null,          -- 'newspaper' | 'epaper' | 'youtube'
    external_id          varchar(512) not null,          -- article url / paper:city:date:pN / video_id
    source               varchar(128) not null,          -- 'Dawn', 'Jang', 'Geo News', ...
    section              varchar(128),
    title                text not null,
    url                  text not null,
    matched_keywords     jsonb not null default '[]'::jsonb,
    keyword_media        jsonb not null default '{}'::jsonb,
    keyword_hits         jsonb not null default '{}'::jsonb, -- YouTube per-keyword timestamps
    snippet              text,
    summary              text,
    relevance            varchar(32),                    -- Directly/Tangentially/Not Relevant
    sentiment            varchar(16),                    -- Positive/Critical/Neutral
    screenshot_path      text,
    full_screenshot_path text,
    deeplink_seconds     integer,                        -- YouTube first-match timestamp
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

-- YouTube channels + bulletin schedules
create table if not exists youtube_channels (
    id                   bigint generated always as identity primary key,
    channel_id           varchar(64)  not null,
    name                 varchar(255) not null default '',
    handle               varchar(128) not null default '',
    url                  text         not null default '',
    uploads_playlist_id  varchar(64)  not null default '',
    timezone             varchar(64)  not null default 'Asia/Karachi',
    media_source         varchar(32)  not null default 'authorized',
    media_source_config  jsonb        not null default '{}'::jsonb,
    active               boolean      not null default true,
    created_at           timestamptz  not null default now(),
    constraint uq_youtube_channel_id unique (channel_id)
);

create table if not exists bulletin_slots (
    id                bigint generated always as identity primary key,
    channel_id        bigint not null references youtube_channels(id) on delete cascade,
    local_time        varchar(8)  not null,              -- HH:MM:SS
    label             varchar(64) not null default '',
    title_rules       jsonb not null default '[]'::jsonb,
    min_duration_sec  integer not null default 180,
    max_duration_sec  integer not null default 3600,
    enabled           boolean not null default true,
    effective_from    varchar(10),
    effective_to      varchar(10),
    constraint uq_bulletin_slot_channel_time unique (channel_id, local_time)
);
create index if not exists ix_bulletin_slot_channel on bulletin_slots (channel_id);

create table if not exists youtube_bulletins (
    id                    bigint generated always as identity primary key,
    channel_db_id         bigint not null references youtube_channels(id) on delete cascade,
    slot_id               bigint not null references bulletin_slots(id) on delete cascade,
    slot_date             varchar(10) not null,
    video_id              varchar(32),
    title                 text not null default '',
    published_at          timestamptz,
    duration_seconds      integer,
    discovery_status      varchar(24) not null default 'waiting',
    transcription_status  varchar(24) not null default 'pending',
    attempts              integer not null default 0,
    error                 text,
    candidates            jsonb not null default '[]'::jsonb,
    last_processed_at     timestamptz,
    created_at            timestamptz not null default now(),
    constraint uq_yt_bulletin_slot_date unique (channel_db_id, slot_id, slot_date)
);
create index if not exists ix_yt_bulletin_date on youtube_bulletins (slot_date);
create index if not exists ix_yt_bulletin_video on youtube_bulletins (video_id);

create table if not exists transcripts (
    id                bigint generated always as identity primary key,
    video_id          varchar(32) not null,
    bulletin_id       bigint references youtube_bulletins(id) on delete set null,
    channel_id        varchar(64),
    source            varchar(255) not null default '',
    title             text not null default '',
    url               text not null default '',
    language          varchar(8),
    text              text not null default '',
    segments          jsonb not null default '[]'::jsonb,
    duration_seconds  integer,
    transcriber       varchar(32) not null default 'groq',
    model             varchar(64) not null default '',
    confidence        jsonb not null default '{}'::jsonb,
    version           integer not null default 1,
    is_live           boolean not null default false,
    created_at        timestamptz not null default now(),
    constraint uq_transcript_video_id unique (video_id)
);

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
