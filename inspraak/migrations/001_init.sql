-- Foundation schema: register, Have Your Say snapshot, meetings, documents, vectors.
-- Embedding dimension 384 = paraphrase-multilingual-MiniLM-L12-v2.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE ingest_runs (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    ok          boolean,
    stats       jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- EU Transparency Register, one row per registrant.
CREATE TABLE organisations (
    tr_id                     text PRIMARY KEY,
    name                      text NOT NULL,
    name_latin                text,
    acronym                   text,
    entity_form               text,
    website                   text,
    category                  text,
    registration_date         timestamptz,
    last_update               timestamptz,
    head_country              text,
    head_city                 text,
    head_postcode             text,
    eu_country                text,
    eu_city                   text,
    goals                     text,
    levels_of_interest        text[] NOT NULL DEFAULT '{}',
    interests                 text[] NOT NULL DEFAULT '{}',
    eu_legislative_proposals  text,
    eu_forums                 text,
    communication_activities  text,
    groupings                 text,
    interest_represented      text,
    is_member_of              text,
    organisation_members      text,
    members_count             numeric,
    members_fte               numeric,
    ep_accredited             integer,
    fin_year_start            date,
    fin_year_end              date,
    costs_min                 numeric,
    costs_max                 numeric,
    total_budget              numeric,
    financial                 jsonb,
    profile_text              text,
    profile_embedding         vector(384),
    export_date               timestamptz
);
CREATE INDEX organisations_name_trgm ON organisations USING gin (lower(name) gin_trgm_ops);
CREATE INDEX organisations_acronym ON organisations (lower(acronym));
CREATE INDEX organisations_country ON organisations (head_country);
CREATE INDEX organisations_category ON organisations (category);
CREATE INDEX organisations_embedding ON organisations
    USING hnsw (profile_embedding vector_cosine_ops);

-- Have Your Say initiatives (all of them, from the search listing).
CREATE TABLE initiatives (
    id                 bigint PRIMARY KEY,
    reference          text,
    short_title        text,
    act_type           text,
    status             text,
    stage              text,
    feedback_status    text,
    feedback_start     timestamp,
    feedback_end       timestamp,
    topics             text[] NOT NULL DEFAULT '{}',
    dg                 text,
    unit               text,
    dossier_summary    text,
    policy_areas       text[] NOT NULL DEFAULT '{}',
    published_at       timestamp,
    modified_at        timestamp,
    listed_at          timestamptz,
    detail_fetched_at  timestamptz
);
CREATE INDEX initiatives_feedback_end ON initiatives (feedback_end);
CREATE INDEX initiatives_title_trgm ON initiatives USING gin (lower(short_title) gin_trgm_ops);

-- One feedback period (roadmap, call for evidence, proposal, ...) of an initiative.
CREATE TABLE publications (
    id               bigint PRIMARY KEY,
    initiative_id    bigint NOT NULL REFERENCES initiatives (id) ON DELETE CASCADE,
    type             text,
    stage            text,
    reference        text,
    title            text,
    feedback_status  text,
    published_at     timestamp,
    end_date         timestamp,
    total_feedback   integer,
    in_scope         boolean NOT NULL DEFAULT false,
    scope_reason     text,
    feedback_fetched_at  timestamptz,
    feedback_truncated   boolean NOT NULL DEFAULT false,
    org_feedback_count     integer,
    citizen_feedback_count integer,
    attachments      jsonb NOT NULL DEFAULT '[]'::jsonb,
    match_text       text,
    embedding        vector(384)
);
CREATE INDEX publications_initiative ON publications (initiative_id);
CREATE INDEX publications_scope ON publications (in_scope, end_date);

-- Feedback rows. There are no name columns at all. Citizen rows keep only the
-- fields needed for counting; the CHECK below is the last line of the privacy guard.
CREATE TABLE feedback (
    id                bigint PRIMARY KEY,
    publication_id    bigint NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    user_type         text NOT NULL,
    is_citizen        boolean NOT NULL,
    country           text,
    language          text,
    date_feedback     timestamp,
    organization      text,
    tr_number         text,
    company_size      text,
    scope             text,
    governance_level  text,
    visibility        text,
    feedback_text     text,
    feedback_text_en  text,
    attachment_count  integer NOT NULL DEFAULT 0,
    embedding         vector(384),
    CONSTRAINT citizen_rows_hold_no_content CHECK (
        NOT is_citizen
        OR (feedback_text IS NULL AND feedback_text_en IS NULL
            AND organization IS NULL AND tr_number IS NULL)
    )
);
CREATE INDEX feedback_publication ON feedback (publication_id, user_type);
CREATE INDEX feedback_tr ON feedback (tr_number) WHERE tr_number IS NOT NULL;
CREATE INDEX feedback_embedding ON feedback USING hnsw (embedding vector_cosine_ops);

-- Files: Commission documents of a publication and attachments of organisation feedback.
CREATE TABLE documents (
    document_id     text PRIMARY KEY,
    kind            text NOT NULL CHECK (kind IN ('proposal', 'feedback_attachment')),
    publication_id  bigint NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    feedback_id     bigint REFERENCES feedback (id) ON DELETE CASCADE,
    title           text,
    filename        text,
    language        text,
    doc_type        text,
    category        text,
    pages_declared  integer,
    size_bytes      bigint,
    sha256          text,
    fetched_at      timestamptz,
    parse_status    text NOT NULL DEFAULT 'pending',
    parse_error     text,
    pages_extracted integer,
    chars_extracted integer
);
CREATE INDEX documents_publication ON documents (publication_id, kind);
CREATE INDEX documents_feedback ON documents (feedback_id);

CREATE TABLE document_pages (
    document_id  text NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    page_no      integer NOT NULL,
    text         text NOT NULL,
    PRIMARY KEY (document_id, page_no)
);

-- Chunks never cross a page, so a chunk cites exactly one page.
CREATE TABLE chunks (
    id           bigserial PRIMARY KEY,
    document_id  text NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    page_no      integer NOT NULL,
    chunk_no     integer NOT NULL,
    text         text NOT NULL,
    embedding    vector(384),
    UNIQUE (document_id, page_no, chunk_no)
);
CREATE INDEX chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);

-- Commission meetings with interest representatives, mandate 2024 to 2029.
CREATE TABLE meetings (
    id                 text PRIMARY KEY,
    cabinet            text,
    ec_representative  text,
    ec_title           text,
    meeting_date       date,
    location           text,
    org_names_raw      text,
    tr_ids_raw         text,
    subject            text,
    embedding          vector(384),
    file_date          date
);
CREATE INDEX meetings_date ON meetings (meeting_date);
CREATE INDEX meetings_embedding ON meetings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE meeting_orgs (
    meeting_id  text NOT NULL REFERENCES meetings (id) ON DELETE CASCADE,
    position    integer NOT NULL,
    tr_id       text NOT NULL,
    org_name    text,
    PRIMARY KEY (meeting_id, position)
);
CREATE INDEX meeting_orgs_tr ON meeting_orgs (tr_id);
