-- Agent layer: LLM call log, permanent publication-layer caches, organisation runs,
-- traces, review queue, long-term memory, drafts. LangGraph's own checkpoint tables
-- (checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations) are created
-- by the checkpointer's setup() and are not listed here.

-- Every model call, whatever the provider. Real call counts come from this table.
CREATE TABLE llm_calls (
    id              bigserial PRIMARY KEY,
    provider        text NOT NULL,
    model           text NOT NULL,
    purpose         text NOT NULL,
    cache_key       text,
    run_id          uuid,
    prompt_tokens   integer,
    output_tokens   integer,
    duration_ms     integer,
    attempts        integer NOT NULL DEFAULT 1,
    ok              boolean NOT NULL,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_created ON llm_calls (created_at);
CREATE INDEX llm_calls_purpose ON llm_calls (purpose, ok);

-- Reader: one summary per publication, shared by every organisation, kept forever.
CREATE TABLE publication_summaries (
    publication_id  bigint PRIMARY KEY REFERENCES publications (id) ON DELETE CASCADE,
    provider        text NOT NULL,
    model           text NOT NULL,
    summary         jsonb NOT NULL,
    source_chunks   integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Feedback analyst: one row per organisation submission, kept forever by feedback id.
-- key_asks is a list of {ask, quote, verified}; quotes that were not found verbatim in
-- the source text are dropped before the row is written and counted in quotes_dropped.
CREATE TABLE feedback_analyses (
    feedback_id     bigint PRIMARY KEY REFERENCES feedback (id) ON DELETE CASCADE,
    publication_id  bigint NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    provider        text NOT NULL,
    model           text NOT NULL,
    stance          text NOT NULL CHECK (stance IN ('supports', 'opposes', 'wants_changes', 'unclear')),
    confidence      real NOT NULL,
    key_asks        jsonb NOT NULL DEFAULT '[]'::jsonb,
    quotes_total    integer NOT NULL DEFAULT 0,
    quotes_dropped  integer NOT NULL DEFAULT 0,
    source          text NOT NULL,        -- 'text', 'text_en' or 'attachment'
    needs_review    boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX feedback_analyses_publication ON feedback_analyses (publication_id);

-- Meetings mapper: meetings whose subject is close to a publication, approximate.
CREATE TABLE publication_meetings (
    publication_id  bigint NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    meeting_id      text NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    score           real NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (publication_id, meeting_id)
);

-- One organisation-layer run. thread_id is the LangGraph thread (same value).
CREATE TABLE runs (
    id              uuid PRIMARY KEY,
    tr_id           text NOT NULL REFERENCES organisations (tr_id) ON DELETE CASCADE,
    status          text NOT NULL CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed')),
    stage           text,
    requested_by    text,
    options         jsonb NOT NULL DEFAULT '{}'::jsonb,
    result          jsonb,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX runs_org ON runs (tr_id, created_at DESC);
CREATE INDEX runs_status ON runs (status);

-- What each agent did, in order, for the run trace page.
CREATE TABLE run_traces (
    id              bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq             integer NOT NULL,
    agent           text NOT NULL,
    tool            text,
    input_summary   text,
    output_summary  text,
    prompt_tokens   integer NOT NULL DEFAULT 0,
    output_tokens   integer NOT NULL DEFAULT 0,
    llm_calls       integer NOT NULL DEFAULT 0,
    cache_hit       boolean,
    duration_ms     integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, seq)
);

-- Review inbox. kind: go_no_go, draft_review (the two interrupts), relevance, stance,
-- attachment, deadline, conflict, critic (escalations that need no resume).
CREATE TABLE review_items (
    id              bigserial PRIMARY KEY,
    run_id          uuid REFERENCES runs (id) ON DELETE CASCADE,
    tr_id           text,
    publication_id  bigint REFERENCES publications (id) ON DELETE CASCADE,
    feedback_id     bigint REFERENCES feedback (id) ON DELETE CASCADE,
    kind            text NOT NULL,
    blocking        boolean NOT NULL DEFAULT false,  -- true when the graph waits on it
    triggers        text[] NOT NULL DEFAULT '{}',
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'decided')),
    decision        jsonb,
    decided_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    decided_at      timestamptz
);
CREATE INDEX review_items_open ON review_items (status, created_at);
CREATE INDEX review_items_run ON review_items (run_id);

-- Long-term memory per organisation: decisions, edits, corrections, positions.
CREATE TABLE org_memory (
    id              bigserial PRIMARY KEY,
    tr_id           text NOT NULL REFERENCES organisations (tr_id) ON DELETE CASCADE,
    kind            text NOT NULL,   -- go, no_go, not_our_topic, profile_edit, draft_edit, approved, rejected, position
    publication_id  bigint REFERENCES publications (id) ON DELETE SET NULL,
    run_id          uuid REFERENCES runs (id) ON DELETE SET NULL,
    content         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX org_memory_org ON org_memory (tr_id, kind);

-- Drafts, one row per version. Never submitted by the system.
CREATE TABLE drafts (
    id              bigserial PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    tr_id           text NOT NULL,
    publication_id  bigint NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    version         integer NOT NULL DEFAULT 1,
    text            text NOT NULL,
    stance          text,
    critic          jsonb,
    status          text NOT NULL DEFAULT 'machine_draft'
                    CHECK (status IN ('machine_draft', 'approved', 'edited', 'rejected')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, publication_id, version)
);
