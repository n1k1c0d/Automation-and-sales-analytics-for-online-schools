WITH matching_leads AS (
    SELECT id, manager_id, source, created_at
    FROM leads
    WHERE (CAST(:manager_id AS uuid) IS NULL OR manager_id = CAST(:manager_id AS uuid))
      AND (CAST(:source AS text) IS NULL OR source = CAST(:source AS text))
),
submitted_applications AS NOT MATERIALIZED (
    SELECT lead_id, submitted_at
    FROM bank_applications
    WHERE status IN ('submitted', 'approved', 'declined')
),
successful_payments AS NOT MATERIALIZED (
    SELECT lead_id, paid_at, amount_rub
    FROM payments
    WHERE status = 'paid'
),
first_lessons AS (
    SELECT lessons.lead_id, min(lessons.held_at) AS first_held_at
    FROM lessons
    JOIN matching_leads ON matching_leads.id = lessons.lead_id
    WHERE lessons.status = 'completed'
      AND lessons.held_at IS NOT NULL
    GROUP BY lessons.lead_id
),
cohort AS (
    SELECT
        first_lessons.lead_id,
        first_lessons.first_held_at,
        EXISTS (
            SELECT 1
            FROM submitted_applications
            WHERE submitted_applications.lead_id = first_lessons.lead_id
              AND submitted_at > first_held_at
              AND submitted_at < :cutoff_end_utc
        ) AS bank_conversion,
        EXISTS (
            SELECT 1
            FROM successful_payments
            WHERE successful_payments.lead_id = first_lessons.lead_id
              AND paid_at > first_held_at
              AND paid_at < :cutoff_end_utc
        ) AS payment_conversion
    FROM first_lessons
    WHERE first_held_at >= :period_start_utc AND first_held_at < :period_end_utc
),
events (lead_id, occurred_at, kind, amount_rub, bank_conversion, payment_conversion) AS (
    SELECT id, created_at, 'lead', CAST(NULL AS numeric), false, false
    FROM matching_leads
    WHERE created_at >= :period_start_utc AND created_at < :period_end_utc

    UNION ALL

    SELECT lead_id, scheduled_at, 'scheduled_lesson', NULL, false, false
    FROM lessons
    WHERE scheduled_at >= :period_start_utc AND scheduled_at < :period_end_utc

    UNION ALL

    SELECT lead_id, held_at, 'completed_lesson', NULL, false, false
    FROM lessons
    WHERE status = 'completed'
      AND held_at >= :period_start_utc AND held_at < :period_end_utc

    UNION ALL

    SELECT lead_id, submitted_at, 'bank_application', NULL, false, false
    FROM submitted_applications
    WHERE submitted_at >= :period_start_utc AND submitted_at < :period_end_utc

    UNION ALL

    SELECT lead_id, paid_at, 'payment', amount_rub, false, false
    FROM successful_payments
    WHERE paid_at >= :period_start_utc AND paid_at < :period_end_utc

    UNION ALL

    SELECT lead_id, first_held_at, 'cohort', NULL, bank_conversion, payment_conversion
    FROM cohort
),
facts AS (
    SELECT
        events.lead_id,
        matching_leads.manager_id,
        matching_leads.source,
        CAST(timezone(:business_zone, events.occurred_at) AS date) AS day,
        events.kind,
        events.amount_rub,
        events.bank_conversion,
        events.payment_conversion
    FROM events
    JOIN matching_leads ON matching_leads.id = events.lead_id

    UNION ALL

    -- Each calendar day contributes a row even when it has no sales activity.
    SELECT
        NULL, NULL, NULL, CAST(:start_date AS date) + day_offset, 'calendar', NULL, false, false
    FROM generate_series(0, CAST(:end_date AS date) - CAST(:start_date AS date)) AS days(day_offset)
),
metrics AS (
    SELECT
        CASE
            WHEN grouping(day) = 0 THEN 'day'
            WHEN grouping(manager_id) = 0 THEN 'manager'
            WHEN grouping(source) = 0 THEN 'source'
            ELSE 'total'
        END AS grain,
        day,
        manager_id,
        source,
        count(*) FILTER (WHERE kind = 'lead') AS leads,
        count(*) FILTER (WHERE kind = 'scheduled_lesson') AS scheduled_lessons,
        count(*) FILTER (WHERE kind = 'completed_lesson') AS completed_lessons,
        count(DISTINCT lead_id) FILTER (WHERE kind = 'bank_application') AS bank_application_leads,
        count(DISTINCT lead_id) FILTER (WHERE kind = 'payment') AS paying_leads,
        coalesce(sum(amount_rub), 0.00) AS paid_amount_rub,
        count(*) FILTER (WHERE kind = 'cohort') AS cohort_leads,
        count(*) FILTER (WHERE bank_conversion) AS cohort_bank_application_leads,
        count(*) FILTER (WHERE payment_conversion) AS cohort_paying_leads
    FROM facts
    GROUP BY GROUPING SETS ((), (day), (manager_id), (source))
    HAVING (grouping(manager_id) = 1 OR manager_id IS NOT NULL)
       AND (grouping(source) = 1 OR source IS NOT NULL)
)
SELECT
    metrics.*,
    managers.name AS manager_name,
    round(paid_amount_rub / nullif(paying_leads, 0), 2) AS average_paid_check_rub,
    round(100.0 * cohort_bank_application_leads / nullif(cohort_leads, 0), 2)
        AS lesson_to_bank_application_pct,
    round(100.0 * cohort_paying_leads / nullif(cohort_leads, 0), 2)
        AS lesson_to_payment_pct
FROM metrics
LEFT JOIN managers ON managers.id = metrics.manager_id
ORDER BY grain, day, manager_name, manager_id, source
