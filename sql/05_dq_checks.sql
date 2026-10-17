-- =====================================================================
-- dq_checks: what's missing or doubtful in the data
-- =====================================================================
-- Shown on the dashboard so readers know what to trust.
-- n = how many records are affected, pct = as a share of all tasks.

DROP TABLE IF EXISTS dq_checks;

CREATE TABLE dq_checks AS
SELECT 'Days where rows saved differ from GitLab''s own count' AS check_name,
       COUNT(*) AS n, NULL AS pct
FROM ingest_days
WHERE rows_landed <> api_total

UNION ALL
SELECT 'Tasks with no team label',
       SUM(team = '(no team label)'),
       ROUND(100.0 * SUM(team = '(no team label)') / COUNT(*), 1)
FROM fct_tasks

UNION ALL
SELECT 'Tasks with time tracking filled in',
       COUNT(time_spent_hours),
       ROUND(100.0 * COUNT(time_spent_hours) / COUNT(*), 1)
FROM fct_tasks

UNION ALL
SELECT 'Bot / automation MRs left out',
       SUM(is_bot), ROUND(100.0 * SUM(is_bot) / COUNT(*), 1)
FROM stg_merge_requests

UNION ALL
SELECT 'Reverts whose original task is older than the loaded period',
       SUM(original_mr_id IS NULL), NULL
FROM stg_reverts

UNION ALL
SELECT 'Raw records skipped (bad JSON, missing id or merge date)',
       skipped_records, NULL
FROM stg_load_stats;
