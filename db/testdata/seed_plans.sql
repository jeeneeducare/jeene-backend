-- A small but realistic content set, for running the API against a local database.
--
-- Written because nothing in this repo could be exercised locally: the integration tests
-- are all written against the Supabase seed, so a developer without those credentials
-- could only ever run the unit tests. Four tickets of Jeene Mode were built that way, and
-- the first thing a real database found was a table missing from version control.
--
-- Deliberately not a copy of production. It is the smallest shape that exercises the
-- decisions: two chapters so a prerequisite can cross one, questions at every difficulty
-- and type so selectors have something to filter, a student with a record that triggers
-- a foundation step and names a weak concept, and figures on both sides of the reveal so
-- the boundary can be checked rather than assumed.
--
--   createdb jeene_test
--   psql jeene_test -f ../../jeene-plugin/db/schema.sql
--   psql jeene_test -f db/backend_schema.sql
--   psql jeene_test -f db/testdata/seed_plans.sql

BEGIN;

INSERT INTO tenants (tenant_id, display_name, type)
VALUES ('JEENE_MASTER', 'Jeene', 'master') ON CONFLICT DO NOTHING;

INSERT INTO exams (exam_id, name) VALUES ('NEET', 'NEET') ON CONFLICT DO NOTHING;

-- --- the tree -------------------------------------------------------------------------
-- subject -> class -> chapter -> topic -> subtopic -> concept, which is the shape every
-- query in the repo walks.

INSERT INTO nodes (node_id, tenant_id, type, title, slug, parent_id, depth, display_order,
                   subject_id, class_level, ncert_chapter_number, status, search_keywords)
VALUES
  ('phy',        'JEENE_MASTER', 'subject', 'Physics',  'physics',  NULL,  0, 0, 'phy', NULL, NULL, 'published', NULL),
  ('phy_11',     'JEENE_MASTER', 'class',   'Class 11', 'class-11', 'phy', 1, 0, 'phy', 11,   NULL, 'published', NULL),

  -- The earlier chapter. Its concepts are what a prerequisite can point back at.
  ('phy_11_ch5', 'JEENE_MASTER', 'chapter', 'Laws of Motion', 'laws-of-motion',
   'phy_11', 2, 0, 'phy', 11, 5, 'published', ARRAY['force','newton','motion']),
  ('phy_11_ch5_t1', 'JEENE_MASTER', 'topic',    'Newton''s laws', 'newtons-laws',
   'phy_11_ch5', 3, 0, 'phy', 11, NULL, 'published', NULL),
  ('phy_11_ch5_s1', 'JEENE_MASTER', 'subtopic', 'The second law', 'second-law',
   'phy_11_ch5_t1', 4, 0, 'phy', 11, NULL, 'published', NULL),
  ('c_newton2',     'JEENE_MASTER', 'concept',  'Newton''s second law', 'newtons-second-law',
   'phy_11_ch5_s1', 5, 0, 'phy', 11, NULL, 'published', ARRAY['force','acceleration','mass']),

  -- The chapter under test.
  ('phy_11_ch8', 'JEENE_MASTER', 'chapter', 'Gravitation', 'gravitation',
   'phy_11', 2, 1, 'phy', 11, 8, 'published', ARRAY['gravity','force','field']),
  ('phy_11_ch8_t1', 'JEENE_MASTER', 'topic', 'Gravitational field', 'gravitational-field',
   'phy_11_ch8', 3, 0, 'phy', 11, NULL, 'published', NULL),
  ('phy_11_ch8_s1', 'JEENE_MASTER', 'subtopic', 'Acceleration due to gravity',
   'acceleration-due-to-gravity', 'phy_11_ch8_t1', 4, 0, 'phy', 11, NULL, 'published', NULL),
  ('c_height',   'JEENE_MASTER', 'concept', 'Variation of g with height', 'g-with-height',
   'phy_11_ch8_s1', 5, 0, 'phy', 11, NULL, 'published', ARRAY['gravity','height','force']),
  ('c_depth',    'JEENE_MASTER', 'concept', 'Variation of g with depth', 'g-with-depth',
   'phy_11_ch8_s1', 5, 1, 'phy', 11, NULL, 'published', ARRAY['gravity','depth']),
  ('c_latitude', 'JEENE_MASTER', 'concept', 'Variation of g with latitude', 'g-with-latitude',
   'phy_11_ch8_s1', 5, 2, 'phy', 11, NULL, 'published', ARRAY['gravity','rotation']),

  -- Unpublished, so every query can be checked for honouring status.
  ('c_hidden',   'JEENE_MASTER', 'concept', 'Draft concept', 'draft-concept',
   'phy_11_ch8_s1', 5, 3, 'phy', 11, NULL, 'draft', NULL)
ON CONFLICT (node_id) DO NOTHING;

-- An authored, cross-chapter prerequisite: exactly the case the pipeline does not yet
-- fill and the feature was asked for.
UPDATE nodes SET prerequisite_node_ids = ARRAY['c_newton2'] WHERE node_id = 'c_height';

-- --- questions --------------------------------------------------------------------------
-- Twelve per concept: three types x (easy, medium, hard, ungraded), so a selector has
-- something to filter and `unrated` is a real case rather than a hypothetical.

INSERT INTO questions (question_id, tenant_id, question_type, question_text, options_json,
                       correct_option_ids, explanation_json, difficulty, status, source)
SELECT
  concept || '_' || qtype || '_' || COALESCE(diff, 'unrated'),
  'JEENE_MASTER',
  qtype,
  'STEM ' || concept || ' ' || qtype || ' ' || COALESCE(diff, 'unrated'),
  '[{"id":"a","text":"OPTION-A"},{"id":"b","text":"OPTION-B"},
    {"id":"c","text":"OPTION-C"},{"id":"d","text":"OPTION-D"}]'::jsonb,
  ARRAY['b'],
  '{"format":"text","text":"WORKED-SOLUTION-TEXT"}'::jsonb,
  diff,
  'published',
  'in-house'
FROM unnest(ARRAY['c_height','c_depth','c_latitude','c_newton2']) AS concept
CROSS JOIN unnest(ARRAY['mcq','pyq','ncert_exemplar']) AS qtype
CROSS JOIN unnest(ARRAY['easy','medium','hard',NULL]) AS diff
ON CONFLICT (question_id) DO NOTHING;

INSERT INTO question_concept_mappings (question_id, concept_node_id, is_primary)
SELECT q.question_id, split_part(q.question_id, '_' || q.question_type, 1), true
  FROM questions q
ON CONFLICT DO NOTHING;

-- One question tagged to a second concept, so "bucket totals cannot be summed" is a real
-- condition in the data rather than only an assertion in a comment.
INSERT INTO question_concept_mappings (question_id, concept_node_id, is_primary)
VALUES ('c_height_mcq_easy', 'c_depth', false)
ON CONFLICT DO NOTHING;

-- A question that arrived with an unreleased paper. It must be invisible to browsing,
-- to the inventory's counts, and to every selector.
INSERT INTO questions (question_id, tenant_id, question_type, question_text, options_json,
                       correct_option_ids, difficulty, status, source)
VALUES ('c_height_paper_q1', 'JEENE_MASTER', 'mcq', 'STEM from an unreleased paper',
        '[{"id":"a","text":"A"}]'::jsonb, ARRAY['a'], 'medium', 'published', 'test_paper')
ON CONFLICT DO NOTHING;
INSERT INTO question_concept_mappings (question_id, concept_node_id, is_primary)
VALUES ('c_height_paper_q1', 'c_height', true) ON CONFLICT DO NOTHING;
INSERT INTO tests (test_id, tenant_id, title, status, released_at)
VALUES ('unreleased_mock', 'JEENE_MASTER', 'Unreleased mock', 'published', NULL)
ON CONFLICT DO NOTHING;
INSERT INTO test_questions (test_id, question_id, position)
VALUES ('unreleased_mock', 'c_height_paper_q1', 1) ON CONFLICT DO NOTHING;

-- --- figures ----------------------------------------------------------------------------
-- One on the stem and one on the worked solution, so the boundary can be checked against
-- data rather than trusted.

INSERT INTO question_figures (figure_id, question_id, placement, image_url, display_order, source)
VALUES
  ('fig_stem_1', 'c_height_mcq_easy', 'stem',
   'https://cdn.example/STEM-FIGURE.png', 0, 'ncert'),
  ('fig_expl_1', 'c_height_mcq_easy', 'explanation',
   'https://cdn.example/SOLUTION-FIGURE.png', 0, 'ncert')
ON CONFLICT (figure_id) DO NOTHING;

-- --- other material ----------------------------------------------------------------------

INSERT INTO node_videos (node_id, youtube_id, tenant_id, title, channel, status, position)
VALUES
  ('phy_11_ch8_s1', 'aaaaaaaaaaa', 'JEENE_MASTER', 'Variation of g, derived', 'Jeene', 'published', 0),
  ('phy_11_ch8',    'bbbbbbbbbbb', 'JEENE_MASTER', 'Gravitation, whole chapter', 'Jeene', 'published', 0)
ON CONFLICT DO NOTHING;

INSERT INTO chapter_notes (chapter_id, tenant_id, title, pdf_url, page_count, status)
VALUES ('phy_11_ch8', 'JEENE_MASTER', 'Gravitation — chapter notes',
        'https://cdn.example/NOTES-PDF-URL.pdf', 14, 'published')
ON CONFLICT DO NOTHING;

INSERT INTO question_explanations (question_id, tenant_id, text, model, prompt_version,
                                   source_hash, status)
VALUES ('c_height_mcq_easy', 'JEENE_MASTER', 'AI-EXPLANATION-TEXT', 'seed', 1, 'x', 'published')
ON CONFLICT DO NOTHING;

-- --- students ------------------------------------------------------------------------------

INSERT INTO users (firebase_uid, tenant_id, display_name, class_level, target_exam)
VALUES
  -- Has a record: weak on the prerequisite (which should trigger a foundation step) and
  -- weak on one concept in scope (which should be named by the weak-spots step).
  ('student-with-history', 'JEENE_MASTER', 'Asha', 11, 'NEET'),
  -- Knows nothing about anything. The planner must not invent a weakness for them.
  ('student-fresh', 'JEENE_MASTER', 'Bala', 11, 'NEET'),
  -- For tests that write attempts. Keeping them off the other two is what stops one
  -- test's answers becoming another test's "student record".
  ('student-scratch', 'JEENE_MASTER', 'Chandni', 11, 'NEET')
ON CONFLICT DO NOTHING;

-- Attempts are deterministic and idempotent, both deliberately.
--
-- The first draft used `LIMIT 6` with no ORDER BY and a plain `gen_random_uuid()`, so
-- re-running the seed picked a different six questions and inserted them again. The
-- student's accuracy on the prerequisite drifted from 33% to 75% between runs, and a
-- foundation step appeared and disappeared with it. A fixture that is not repeatable is
-- worse than no fixture: it makes a passing test meaningless and a failing one a mystery.

-- Six on the prerequisite, two right. Enough evidence, under half correct — which is
-- what the foundation rule asks for.
INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id, is_correct,
                      time_spent_ms, created_at)
SELECT md5('seed:newton:' || q.question_id)::uuid, 'student-with-history',
       'JEENE_MASTER', q.question_id, q.rn <= 2, 30000, now() - interval '10 days'
  FROM (SELECT question_id, row_number() OVER (ORDER BY question_id) AS rn
          FROM questions WHERE question_id LIKE 'c_newton2%'
         ORDER BY question_id LIMIT 6) q
ON CONFLICT (attempt_id) DO NOTHING;

-- Nine on one in-scope concept, three right: the weak spot the plan should name.
INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id, is_correct,
                      time_spent_ms, created_at)
SELECT md5('seed:depth:' || q.question_id)::uuid, 'student-with-history',
       'JEENE_MASTER', q.question_id, q.rn <= 3, 30000, now() - interval '5 days'
  FROM (SELECT question_id, row_number() OVER (ORDER BY question_id) AS rn
          FROM questions WHERE question_id LIKE 'c_depth%'
         ORDER BY question_id LIMIT 9) q
ON CONFLICT (attempt_id) DO NOTHING;

-- The same question answered wrong and then right, so "latest attempt wins" is a real
-- condition in the data rather than only an assertion in a test.
INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id, is_correct,
                      time_spent_ms, created_at)
VALUES
  (md5('seed:lat:wrong')::uuid, 'student-with-history', 'JEENE_MASTER',
   'c_latitude_mcq_easy', false, 30000, now() - interval '3 days'),
  (md5('seed:lat:right')::uuid, 'student-with-history', 'JEENE_MASTER',
   'c_latitude_mcq_easy', true, 20000, now() - interval '1 day')
ON CONFLICT (attempt_id) DO NOTHING;

COMMIT;
