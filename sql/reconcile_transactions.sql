SELECT s.source_key, CASE
    WHEN t.id IS NULL THEN 'missing_integration'
    WHEN s.payload_hash != t.payload_hash THEN 'source_digest_mismatch'
    WHEN t.state != 'delivered' THEN t.state
    ELSE 'delivered' END AS outcome
FROM source_manifest s LEFT JOIN transactions t USING(source_key)
UNION ALL
SELECT t.source_key, 'missing_source'
FROM transactions t LEFT JOIN source_manifest s USING(source_key) WHERE s.source_key IS NULL
ORDER BY source_key;
