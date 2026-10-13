-- Preserve separate entry domains and human/bot/login categories inside a
-- ten-minute network-prefix bucket. No columns, identities or historical rows
-- are added/rewritten; previously discarded events cannot be recovered.
-- Migration runner supplies the transaction. Replays are harmless.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'site_visit_events'::regclass
          AND conname = 'site_visit_events_dedup_v2_unique'
    ) THEN
        ALTER TABLE site_visit_events
            ADD CONSTRAINT site_visit_events_dedup_v2_unique
            UNIQUE (daily_visitor_hash, page_key, dedup_bucket,
                    request_host, visitor_kind);
    END IF;
END $$;

ALTER TABLE site_visit_events
    DROP CONSTRAINT IF EXISTS site_visit_events_dedup_unique;

COMMENT ON CONSTRAINT site_visit_events_dedup_v2_unique ON site_visit_events IS
    'Ten-minute daily network-prefix dedup, scoped by public entry host and visitor category; not raw pageviews or unique people';

INSERT INTO audit_events
    (event_id, ts, actor_role, action, target_type, detail)
VALUES
    ('aud_migration_0083_site_visit_dedup_dimensions', now(), 'system',
     'site_stats.dedup_dimensions_applied', 'site_visit_events',
     jsonb_build_object('dimensions', 'daily_visitor_hash,page_key,dedup_bucket,request_host,visitor_kind',
                        'historical_rows', 'unchanged; discarded events are not recoverable'))
ON CONFLICT (event_id) DO NOTHING;
