-- =====================================================================
-- mart_associates: one row per associate (anonymous) and team
-- =====================================================================
-- Answers: "How many tasks does each associate do per day?"
--          "What is their quality?"
--          "How long do their tasks take?"
--
--   tasks_per_active_day = tasks / days they merged something
--       (days off aren't counted against anyone)
--   quality_pct          = % of their tasks that were NOT reverted

DROP TABLE IF EXISTS mart_associates;

CREATE TABLE mart_associates AS
SELECT
    associate,
    team,
    COUNT(*)                                          AS tasks,
    COUNT(DISTINCT merged_day)                        AS active_days,
    ROUND(COUNT(*) * 1.0 / COUNT(DISTINCT merged_day), 2) AS tasks_per_active_day,
    SUM(was_reverted)                                 AS errors,
    ROUND(100.0 - 100.0 * SUM(was_reverted) / COUNT(*), 1) AS quality_pct,
    ROUND(AVG(review_comments), 1)                    AS avg_review_comments,
    ROUND(MEDIAN(cycle_hours), 1)                     AS median_hours_to_deliver,
    ROUND(SUM(time_spent_hours), 1)                   AS tracked_hours
FROM fct_tasks
GROUP BY associate, team;
