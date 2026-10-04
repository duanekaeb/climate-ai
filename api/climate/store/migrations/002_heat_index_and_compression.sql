-- 002: keep hot queries fast and the time series small as history grows.
-- Runs inside one transaction owned by climate.store.migrate (no BEGIN/COMMIT here).

-- analytics.daily.heat_metrics asks "has this unit ever reported compressor heat?" on every
-- /status. For furnace or cooling-only units that was a full scan of runtime_5m.
CREATE INDEX IF NOT EXISTS idx_runtime_5m_comp_heat ON runtime_5m (unit_key) WHERE comp_heat1 > 0;

-- TimescaleDB only: compress chunks older than 60 days (5-minute history stays queryable;
-- compressed chunks are far smaller). Re-pulls and backfills touch recent days only. Any
-- failure here (an older TimescaleDB, a licence without compression) must not block startup.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        BEGIN
            ALTER TABLE readings_5m SET (timescaledb.compress, timescaledb.compress_segmentby = 'sensor_key',
                                         timescaledb.compress_orderby = 'ts');
            PERFORM add_compression_policy('readings_5m', INTERVAL '60 days', if_not_exists => true);
            ALTER TABLE runtime_5m SET (timescaledb.compress, timescaledb.compress_segmentby = 'unit_key',
                                        timescaledb.compress_orderby = 'ts');
            PERFORM add_compression_policy('runtime_5m', INTERVAL '60 days', if_not_exists => true);
            ALTER TABLE room_states SET (timescaledb.compress, timescaledb.compress_segmentby = 'room_key',
                                         timescaledb.compress_orderby = 'ts');
            PERFORM add_compression_policy('room_states', INTERVAL '60 days', if_not_exists => true);
            ALTER TABLE occupancy_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'sensor_key',
                                              timescaledb.compress_orderby = 'ts');
            PERFORM add_compression_policy('occupancy_events', INTERVAL '60 days', if_not_exists => true);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'compression not enabled: %', SQLERRM;
        END;
    END IF;
END $$;

/*
-- rollback (manual)
DROP INDEX IF EXISTS idx_runtime_5m_comp_heat;
-- SELECT remove_compression_policy('readings_5m'); ... (TimescaleDB)
DELETE FROM schema_migrations WHERE id = '002_heat_index_and_compression';
*/
