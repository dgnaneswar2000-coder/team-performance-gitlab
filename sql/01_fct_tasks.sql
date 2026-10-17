-- =====================================================================
-- fct_tasks: one row per completed task
-- =====================================================================
-- A "task" is a merge request (MR) that was merged by a person.
--   * Bots are left out: they're automation, not associates.
--   * Reverts are left out: undoing a mistake isn't new work. Instead,
--     the task that got undone is marked was_reverted = 1 (an "error").
--
-- This is the table the dashboard filters (by date, team, work type...).
-- Every other table in this folder is a summary of it.

DROP TABLE IF EXISTS fct_tasks;

CREATE TABLE fct_tasks AS
SELECT
    t.mr_id,
    t.merged_day,                                    -- YYYY-MM-DD, UTC
    t.associate,                                     -- anonymous ID
    COALESCE(t.team, '(no team label)')       AS team,
    COALESCE(t.work_type, '(unclassified)')   AS work_type,   -- feature / bug / maintenance
    t.review_comments,
    t.cycle_hours,                                   -- hours from opening to merging
    t.time_spent_hours,                              -- GitLab time tracking (usually empty)
    CASE WHEN r.revert_mr_id IS NULL THEN 0 ELSE 1 END AS was_reverted,
    r.revert_day,                                    -- the day the error was undone
    t.web_url
FROM stg_merge_requests AS t
LEFT JOIN stg_reverts   AS r ON r.original_mr_id = t.mr_id
WHERE t.is_bot = 0
  AND t.is_revert = 0
  AND t.associate IS NOT NULL;
