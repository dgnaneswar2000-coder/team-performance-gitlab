-- =====================================================================
-- mart_teams: one row per team for the whole period
-- =====================================================================
-- Answers: "Which teams deliver the most, with the fewest errors?"

DROP TABLE IF EXISTS mart_teams;

CREATE TABLE mart_teams AS
SELECT
    team,
    COUNT(*)                                          AS tasks,
    COUNT(DISTINCT associate)                         AS associates,
    SUM(was_reverted)                                 AS errors,
    ROUND(100.0 - 100.0 * SUM(was_reverted) / COUNT(*), 1) AS quality_pct,
    ROUND(100.0 * SUM(work_type = 'bug') / COUNT(*), 1)    AS pct_bug_fixes,
    ROUND(AVG(review_comments), 1)                    AS avg_review_comments,
    ROUND(MEDIAN(cycle_hours), 1)                     AS median_hours_to_deliver
FROM fct_tasks
GROUP BY team
ORDER BY tasks DESC;
