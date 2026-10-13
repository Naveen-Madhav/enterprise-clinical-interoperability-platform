WITH latest AS (
    SELECT e.path, MAX(e.transaction_id) AS transaction_id
    FROM expected_resources e JOIN transactions t ON t.id=e.transaction_id
    WHERE t.state='delivered' GROUP BY e.path
)
SELECT e.path, CASE
    WHEN d.path IS NULL THEN 'missing_destination'
    WHEN d.digest != e.digest THEN 'destination_digest_mismatch'
    ELSE 'matched' END AS outcome
FROM latest l JOIN expected_resources e
    ON e.path=l.path AND e.transaction_id=l.transaction_id
LEFT JOIN destination_snapshots d ON d.path=e.path ORDER BY e.path;
