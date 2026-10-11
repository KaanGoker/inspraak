-- A grouped call for evidence is one publication shared by several initiatives
-- (publication 25641 hangs under initiatives 18872, 18873 and 18874). The link table
-- keeps every parent; publications.initiative_id stays as the lowest parent id.

CREATE TABLE publication_initiatives (
    publication_id  bigint NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    initiative_id   bigint NOT NULL REFERENCES initiatives (id) ON DELETE CASCADE,
    PRIMARY KEY (publication_id, initiative_id)
);
CREATE INDEX publication_initiatives_initiative ON publication_initiatives (initiative_id);

INSERT INTO publication_initiatives (publication_id, initiative_id)
SELECT id, initiative_id FROM publications
ON CONFLICT DO NOTHING;
