-- Matcher v2: a hub penalty per publication and a cache of model relevance judgments.

-- How close a publication sits to registrant profiles in general: the mean cosine to its
-- 200 nearest registrants, minus the mean of that value over the in-scope publications.
-- Positive means a hub (close to every profile); the matcher subtracts a share of it.
ALTER TABLE publications ADD COLUMN hub_score real;

-- Reranker: one judgment per organisation, publication and profile text. A profile edit
-- changes profile_hash, so the old judgments stop being used.
CREATE TABLE match_reranks (
    tr_id           text NOT NULL REFERENCES organisations (tr_id) ON DELETE CASCADE,
    publication_id  bigint NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    profile_hash    text NOT NULL,
    relevance       real NOT NULL CHECK (relevance >= 0 AND relevance <= 1),
    reason          text NOT NULL,
    provider        text NOT NULL,
    model           text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tr_id, publication_id, profile_hash)
);
