-- =====================================================================
-- mart_daily: tasks and errors per team per day
-- =====================================================================
-- Answers: "How many tasks did each team complete per day?"
--          "How many errors happened per day?"
--
-- An error is counted on the day it was undone (revert_day), because
-- that's the day the team found out about it.

DROP TABLE IF EXISTS mart_daily;

CREATE TABLE mart_daily AS
SELECT
    team,
    merged_day                      AS day,
    COUNT(*)                        AS tasks_completed,
    SUM(work_type = 'bug')          AS bug_fixes,
    COUNT(DISTINCT associate)       AS active_associates,
    MEDIAN(cycle_hours)             AS median_hours_to_deliver,
    0                               AS errors
FROM fct_tasks
GROUP BY team, merged_day

UNION ALL

SELECT
    team,
    revert_day                      AS day,
    0, 0, 0, NULL,
    COUNT(*)                        AS errors
FROM fct_tasks
WHERE was_reverted = 1
GROUP BY team, revert_day;

-- Note: a team/day can appear twice (once for tasks, once for errors).
-- Add them up when reading:  SELECT day, SUM(tasks_completed), SUM(errors) ...
