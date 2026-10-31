import sqlite3
from typing import Dict

DATABASE_SCHEMA_VERSION = 64

MIGRATIONS: Dict[int, str] = {
    1: """
        CREATE TABLE care_events (
            event_id TEXT PRIMARY KEY,
            sensor_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence TEXT NOT NULL,
            changes_json TEXT NOT NULL
        );
        CREATE INDEX care_events_by_sensor_kind_time
        ON care_events(sensor_id, kind, detected_at);
        CREATE TABLE sensor_readings (
            reading_id INTEGER PRIMARY KEY,
            sensor_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            soil_temperature_c REAL NOT NULL,
            moisture_percent REAL NOT NULL,
            conductivity_us_cm INTEGER NOT NULL,
            air_temperature_c REAL,
            air_humidity_percent REAL,
            soil_ph REAL,
            nitrogen_mg_kg INTEGER,
            phosphorus_mg_kg INTEGER,
            potassium_mg_kg INTEGER,
            UNIQUE(sensor_id, sequence, observed_at)
        );
        CREATE INDEX sensor_readings_by_sensor_time
        ON sensor_readings(sensor_id, observed_at DESC);
    """,
    2: """
        CREATE TABLE plants (
            plant_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            profile_id TEXT,
            room TEXT,
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
        );
        CREATE TABLE sensors (
            sensor_id TEXT PRIMARY KEY,
            identity_kind TEXT NOT NULL,
            identity_value TEXT NOT NULL UNIQUE,
            enrollment_status TEXT NOT NULL DEFAULT 'unclaimed'
                CHECK (enrollment_status IN ('unclaimed', 'enrolled', 'archived')),
            display_name TEXT,
            room TEXT,
            plant_id INTEGER REFERENCES plants(plant_id),
            profile_id TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            latest_rssi INTEGER,
            transport TEXT NOT NULL,
            contract_version INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
        );
        CREATE TABLE advertisements (
            advertisement_id INTEGER PRIMARY KEY,
            sensor_id TEXT REFERENCES sensors(sensor_id),
            packet_id INTEGER,
            received_at TEXT NOT NULL,
            transport TEXT NOT NULL,
            source_adapter TEXT NOT NULL,
            observed_identifier TEXT NOT NULL,
            rssi INTEGER,
            contract_version INTEGER,
            payload_sha256 TEXT NOT NULL,
            decode_status TEXT NOT NULL
                CHECK (decode_status IN ('accepted', 'duplicate', 'rejected')),
            decode_error TEXT
        );
        CREATE INDEX advertisements_by_sensor_time
        ON advertisements(sensor_id, advertisement_id DESC);
        CREATE INDEX advertisements_by_status_time
        ON advertisements(decode_status, advertisement_id DESC);

        INSERT INTO sensors (
            sensor_id, identity_kind, identity_value, enrollment_status,
            first_seen_at, last_seen_at, transport, contract_version
        )
        SELECT sensor_id, 'legacy', sensor_id, 'enrolled',
               MIN(received_at), MAX(received_at), 'simulation', 0
        FROM sensor_readings
        GROUP BY sensor_id;

        ALTER TABLE sensor_readings RENAME TO sensor_readings_v1;
        DROP INDEX sensor_readings_by_sensor_time;
        CREATE TABLE sensor_readings (
            reading_id INTEGER PRIMARY KEY,
            advertisement_id INTEGER UNIQUE
                REFERENCES advertisements(advertisement_id) ON DELETE RESTRICT,
            sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
            sequence INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            soil_temperature_c REAL,
            moisture_percent REAL,
            conductivity_us_cm INTEGER,
            air_temperature_c REAL,
            air_humidity_percent REAL,
            soil_ph REAL,
            nitrogen_mg_kg INTEGER,
            phosphorus_mg_kg INTEGER,
            potassium_mg_kg INTEGER,
            soil_source_status TEXT NOT NULL DEFAULT 'available'
                CHECK (soil_source_status IN ('available', 'unavailable')),
            air_source_status TEXT NOT NULL DEFAULT 'available'
                CHECK (air_source_status IN ('available', 'unavailable')),
            contract_version INTEGER NOT NULL DEFAULT 0,
            UNIQUE(sensor_id, sequence, observed_at)
        );
        INSERT INTO sensor_readings (
            reading_id, sensor_id, sequence, observed_at, received_at,
            soil_temperature_c, moisture_percent, conductivity_us_cm,
            air_temperature_c, air_humidity_percent, soil_ph,
            nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg
        )
        SELECT reading_id, sensor_id, sequence, observed_at, received_at,
               soil_temperature_c, moisture_percent, conductivity_us_cm,
               air_temperature_c, air_humidity_percent, soil_ph,
               nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg
        FROM sensor_readings_v1;
        DROP TABLE sensor_readings_v1;
        CREATE INDEX sensor_readings_by_sensor_time
        ON sensor_readings(sensor_id, observed_at DESC);
    """,
    3: """
        ALTER TABLE sensors ADD COLUMN moisture_low_percent REAL
            CHECK (moisture_low_percent IS NULL OR
                   (moisture_low_percent >= 0 AND moisture_low_percent <= 100));
        ALTER TABLE sensors ADD COLUMN conductivity_high_us_cm INTEGER
            CHECK (conductivity_high_us_cm IS NULL OR conductivity_high_us_cm >= 0);
        ALTER TABLE sensors ADD COLUMN expected_interval_minutes INTEGER NOT NULL DEFAULT 30
            CHECK (expected_interval_minutes BETWEEN 1 AND 1440);
        ALTER TABLE sensors ADD COLUMN replaced_by_sensor_id TEXT
            REFERENCES sensors(sensor_id);
        CREATE INDEX sensors_by_enrollment_last_seen
        ON sensors(enrollment_status, last_seen_at DESC);
        CREATE INDEX sensors_by_replacement
        ON sensors(replaced_by_sensor_id);
    """,
    4: """
        CREATE TABLE hub_settings (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            reporting_interval_minutes INTEGER NOT NULL
                CHECK (reporting_interval_minutes BETWEEN 1 AND 1440)
        );
        INSERT INTO hub_settings (singleton_id, reporting_interval_minutes)
        VALUES (1, 30);
        UPDATE sensors SET expected_interval_minutes = 30;
    """,
    5: """
        CREATE TEMP TABLE sensor_identity_renames (
            old_id TEXT PRIMARY KEY,
            new_id TEXT NOT NULL UNIQUE
        );
        INSERT INTO sensor_identity_renames (old_id, new_id)
        SELECT sensor_id, 'sensor:' || substr(sensor_id, 5)
        FROM sensors
        WHERE length(sensor_id) = 16
          AND sensor_id LIKE 'opp:%'
          AND substr(sensor_id, 5) NOT GLOB '*[^0-9a-f]*';

        INSERT OR IGNORE INTO sensors (
            sensor_id, identity_kind, identity_value, enrollment_status,
            display_name, room, plant_id, profile_id, first_seen_at, last_seen_at,
            latest_rssi, transport, contract_version, archived,
            moisture_low_percent, conductivity_high_us_cm,
            expected_interval_minutes, replaced_by_sensor_id
        )
        SELECT renames.new_id, sensors.identity_kind, renames.new_id,
               sensors.enrollment_status, sensors.display_name, sensors.room,
               sensors.plant_id, sensors.profile_id, sensors.first_seen_at,
               sensors.last_seen_at, sensors.latest_rssi, sensors.transport,
               sensors.contract_version, sensors.archived,
               sensors.moisture_low_percent, sensors.conductivity_high_us_cm,
               sensors.expected_interval_minutes, sensors.replaced_by_sensor_id
        FROM sensors
        JOIN sensor_identity_renames AS renames ON renames.old_id = sensors.sensor_id;

        UPDATE advertisements
        SET sensor_id = (
            SELECT new_id FROM sensor_identity_renames WHERE old_id = advertisements.sensor_id
        )
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        UPDATE OR IGNORE sensor_readings
        SET sensor_id = (
            SELECT new_id FROM sensor_identity_renames WHERE old_id = sensor_readings.sensor_id
        )
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        DELETE FROM sensor_readings
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        UPDATE care_events
        SET sensor_id = (
            SELECT new_id FROM sensor_identity_renames WHERE old_id = care_events.sensor_id
        )
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        UPDATE sensors
        SET replaced_by_sensor_id = (
            SELECT new_id
            FROM sensor_identity_renames
            WHERE old_id = sensors.replaced_by_sensor_id
        )
        WHERE replaced_by_sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        DELETE FROM sensors
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        DROP TABLE sensor_identity_renames;
    """,
    6: """
        CREATE TEMP TABLE sensor_identity_renames (
            old_id TEXT PRIMARY KEY,
            new_id TEXT NOT NULL UNIQUE
        );
        INSERT INTO sensor_identity_renames (old_id, new_id)
        SELECT sensor_id, 'sensor-' || lower(substr(sensor_id, 8))
        FROM sensors
        WHERE length(sensor_id) = 19
          AND sensor_id LIKE 'sensor:%'
          AND substr(sensor_id, 8) NOT GLOB '*[^0-9a-fA-F]*';

        INSERT OR IGNORE INTO sensors (
            sensor_id, identity_kind, identity_value, enrollment_status,
            display_name, room, plant_id, profile_id, first_seen_at, last_seen_at,
            latest_rssi, transport, contract_version, archived,
            moisture_low_percent, conductivity_high_us_cm,
            expected_interval_minutes, replaced_by_sensor_id
        )
        SELECT renames.new_id, sensors.identity_kind, renames.new_id,
               sensors.enrollment_status, sensors.display_name, sensors.room,
               sensors.plant_id, sensors.profile_id, sensors.first_seen_at,
               sensors.last_seen_at, sensors.latest_rssi, sensors.transport,
               sensors.contract_version, sensors.archived,
               sensors.moisture_low_percent, sensors.conductivity_high_us_cm,
               sensors.expected_interval_minutes, sensors.replaced_by_sensor_id
        FROM sensors
        JOIN sensor_identity_renames AS renames ON renames.old_id = sensors.sensor_id;

        UPDATE advertisements
        SET sensor_id = (
            SELECT new_id FROM sensor_identity_renames WHERE old_id = advertisements.sensor_id
        )
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        UPDATE OR IGNORE sensor_readings
        SET sensor_id = (
            SELECT new_id FROM sensor_identity_renames WHERE old_id = sensor_readings.sensor_id
        )
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        DELETE FROM sensor_readings
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        UPDATE care_events
        SET sensor_id = (
            SELECT new_id FROM sensor_identity_renames WHERE old_id = care_events.sensor_id
        )
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        UPDATE sensors
        SET replaced_by_sensor_id = (
            SELECT new_id
            FROM sensor_identity_renames
            WHERE old_id = sensors.replaced_by_sensor_id
        )
        WHERE replaced_by_sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        DELETE FROM sensors
        WHERE sensor_id IN (SELECT old_id FROM sensor_identity_renames);
        DROP TABLE sensor_identity_renames;
    """,
    7: """
        ALTER TABLE sensors ADD COLUMN device_config_revision INTEGER NOT NULL DEFAULT 0
            CHECK (device_config_revision BETWEEN 0 AND 4294967295);
        ALTER TABLE sensors ADD COLUMN device_config_applied_revision INTEGER NOT NULL DEFAULT 0
            CHECK (device_config_applied_revision BETWEEN 0 AND 4294967295);
        ALTER TABLE sensors ADD COLUMN device_config_attempted_at TEXT;
        ALTER TABLE sensors ADD COLUMN device_config_error TEXT;
        UPDATE sensors SET expected_interval_minutes = 5
        WHERE expected_interval_minutes < 5;
        UPDATE sensors
        SET device_config_revision = 1
        WHERE enrollment_status = 'enrolled' AND display_name IS NOT NULL;
    """,
    8: """
        ALTER TABLE sensors ADD COLUMN sensor_reporting_interval_minutes INTEGER
            CHECK (sensor_reporting_interval_minutes IS NULL OR
                   sensor_reporting_interval_minutes BETWEEN 5 AND 1440);
        UPDATE sensors
        SET sensor_reporting_interval_minutes = expected_interval_minutes
        WHERE device_config_revision > 0
          AND device_config_revision = device_config_applied_revision;
    """,
    9: """
        ALTER TABLE sensors ADD COLUMN expected_interval_seconds INTEGER NOT NULL DEFAULT 1800
            CHECK (expected_interval_seconds BETWEEN 1 AND 86400);
        ALTER TABLE sensors ADD COLUMN sensor_reporting_interval_seconds INTEGER
            CHECK (sensor_reporting_interval_seconds IS NULL OR
                   sensor_reporting_interval_seconds BETWEEN 1 AND 86400);
        UPDATE sensors
        SET expected_interval_seconds = expected_interval_minutes * 60,
            sensor_reporting_interval_seconds = sensor_reporting_interval_minutes * 60;
        UPDATE sensors
        SET device_config_applied_revision = 0,
            device_config_error = NULL
        WHERE device_config_revision > 0;
    """,
    10: """
        ALTER TABLE advertisements ADD COLUMN service_data BLOB;
    """,
    11: """
        ALTER TABLE sensors ADD COLUMN onboarding_state TEXT NOT NULL DEFAULT 'onboarding'
            CHECK (onboarding_state IN ('onboarding', 'onboarded'));
        ALTER TABLE sensors ADD COLUMN wifi_enabled INTEGER NOT NULL DEFAULT 0
            CHECK (wifi_enabled IN (0, 1));
        ALTER TABLE sensors ADD COLUMN wifi_state TEXT NOT NULL DEFAULT 'off'
            CHECK (wifi_state IN ('off', 'pending', 'joined', 'failed'));
        ALTER TABLE sensors ADD COLUMN wifi_failure TEXT
            CHECK (wifi_failure IS NULL OR wifi_failure IN (
                'wrong_password', 'network_not_found', 'association_timeout',
                'no_address', 'unsupported_band'));
        ALTER TABLE sensors ADD COLUMN wifi_address TEXT;
        UPDATE sensors SET onboarding_state = 'onboarded' WHERE enrollment_status != 'unclaimed';
        ALTER TABLE hub_settings ADD COLUMN wifi_ssid TEXT;
    """,
    12: """
        CREATE TABLE rooms (
            room_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            aspect TEXT NOT NULL DEFAULT 'unknown'
                CHECK (aspect IN ('unknown', 'north', 'east', 'south', 'west')),
            light TEXT NOT NULL DEFAULT 'unknown'
                CHECK (light IN ('unknown', 'low', 'medium', 'bright')),
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        ALTER TABLE sensors ADD COLUMN room_id INTEGER REFERENCES rooms(room_id);
        INSERT INTO rooms (name)
            SELECT DISTINCT TRIM(room) FROM sensors
            WHERE room IS NOT NULL AND TRIM(room) != '';
        UPDATE sensors
        SET room_id = (SELECT room_id FROM rooms WHERE rooms.name = TRIM(sensors.room))
        WHERE room IS NOT NULL AND TRIM(room) != '';
    """,
    13: """
        /* Widening a CHECK means rebuilding the table, and sensors holds a
           foreign key into rooms, which the single transaction this runner uses
           cannot safely drop and recreate. Renaming the column and adding a
           replacement keeps the constraint honest without touching the key.
           The old column stays behind, unused, with its own default. */
        ALTER TABLE rooms RENAME COLUMN aspect TO superseded_aspect;
        ALTER TABLE rooms ADD COLUMN aspect TEXT NOT NULL DEFAULT 'unknown'
            CHECK (aspect IN ('unknown', 'none', 'several',
                              'north', 'north_east', 'east', 'south_east',
                              'south', 'south_west', 'west', 'north_west'));
        UPDATE rooms SET aspect = superseded_aspect;
    """,
    14: """
        /* Forgetting a sensor has to reach the sensor, and the sensor may be
           asleep or out of range when the person clicks. The intent is recorded
           and carried out the next time it is heard from. */
        ALTER TABLE sensors ADD COLUMN release_pending INTEGER NOT NULL DEFAULT 0
            CHECK (release_pending IN (0, 1));
    """,
    15: """
        /* Reported by the sensor when the hub connects. Nothing in an
           advertisement carries it, so it is only known after a conversation. */
        ALTER TABLE sensors ADD COLUMN firmware_version TEXT;
        /* When that answer was last obtained. A version cached for ever would
           keep naming the firmware a sensor ran before it was reflashed, so the
           hub re-asks and the interface can say how old the answer is. */
        ALTER TABLE sensors ADD COLUMN station_checked_at TEXT;
    """,
    16: """
        /* Firmware images the hub holds, identified by what they contain.
           The digest is the identity: the same image uploaded twice is one row
           and one file, and a sensor is told a digest rather than a file name. */
        CREATE TABLE firmware_images (
            digest TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            project TEXT NOT NULL,
            idf_version TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            built_at TEXT,
            uploaded_at TEXT NOT NULL
        );
        /* What a sensor was last asked to install, and how far it got. The
           target version is kept beside the digest so a finished update can
           still be described after its image is deleted. */
        ALTER TABLE sensors ADD COLUMN firmware_update_digest TEXT;
        ALTER TABLE sensors ADD COLUMN firmware_update_version TEXT;
        ALTER TABLE sensors ADD COLUMN firmware_update_state TEXT NOT NULL DEFAULT 'idle';
        ALTER TABLE sensors ADD COLUMN firmware_update_id INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE sensors ADD COLUMN firmware_update_percent INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE sensors ADD COLUMN firmware_update_error TEXT;
        ALTER TABLE sensors ADD COLUMN firmware_update_started_at TEXT;
    """,
    17: """
        /* Contract v3. A report is identified by the sensor's own report ID and
           may arrive as two packets, so readings are keyed by (sensor, report
           ID) instead of the 8-bit packet ID, and the time a reading was taken
           may be unknown. Both tables are rebuilt: a NOT NULL and a CHECK cannot
           be relaxed in place.

           sensor_readings holds a foreign key into advertisements, and this
           runner keeps foreign keys enforced inside one transaction, so the old
           advertisements cannot simply be dropped while readings point at it.
           The new readings table is made to point at the new advertisements
           table, both old tables are dropped child first, and the renames that
           follow carry the reference along to the final names. */
        CREATE TABLE advertisements_v17 (
            advertisement_id INTEGER PRIMARY KEY,
            sensor_id TEXT REFERENCES sensors(sensor_id),
            report_id INTEGER
                CHECK (report_id IS NULL OR report_id BETWEEN 1 AND 4294967295),
            packet_kind TEXT
                CHECK (packet_kind IS NULL OR
                       packet_kind IN ('main', 'supplementary', 'beacon')),
            /* The 8-bit packet ID of contract v2, kept so nothing already
               recorded is lost. Nothing reads it. */
            legacy_packet_id INTEGER,
            received_at TEXT NOT NULL,
            transport TEXT NOT NULL,
            source_adapter TEXT NOT NULL,
            observed_identifier TEXT NOT NULL,
            rssi INTEGER,
            contract_version INTEGER,
            payload_sha256 TEXT NOT NULL,
            decode_status TEXT NOT NULL
                CHECK (decode_status IN ('accepted', 'duplicate', 'conflict', 'rejected')),
            decode_error TEXT,
            service_data BLOB
        );
        INSERT INTO advertisements_v17 (
            advertisement_id, sensor_id, legacy_packet_id, received_at, transport,
            source_adapter, observed_identifier, rssi, contract_version,
            payload_sha256, decode_status, decode_error, service_data
        )
        SELECT advertisement_id, sensor_id, packet_id, received_at, transport,
               source_adapter, observed_identifier, rssi, contract_version,
               payload_sha256, decode_status, decode_error, service_data
        FROM advertisements;

        CREATE TABLE sensor_readings_v17 (
            reading_id INTEGER PRIMARY KEY,
            /* The main packet the reading came from. */
            advertisement_id INTEGER UNIQUE
                REFERENCES advertisements_v17(advertisement_id) ON DELETE RESTRICT,
            sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
            /* Null for readings stored before contract v3. */
            report_id INTEGER
                CHECK (report_id IS NULL OR report_id BETWEEN 1 AND 4294967295),
            /* The 8-bit packet ID or sample number earlier readings carried,
               kept so nothing already recorded is lost. Nothing reads it. */
            legacy_packet_id INTEGER,
            /* Null when the sensor did not know the time. received_at is never
               copied in its place. */
            observed_at TEXT,
            received_at TEXT NOT NULL,
            soil_temperature_c REAL,
            moisture_percent REAL,
            conductivity_us_cm INTEGER,
            air_temperature_c REAL,
            air_humidity_percent REAL,
            soil_ph REAL,
            nitrogen_mg_kg INTEGER,
            phosphorus_mg_kg INTEGER,
            potassium_mg_kg INTEGER,
            battery_percent INTEGER
                CHECK (battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100),
            battery_voltage_v REAL,
            soil_source_status TEXT NOT NULL DEFAULT 'available'
                CHECK (soil_source_status IN ('available', 'unavailable')),
            air_source_status TEXT NOT NULL DEFAULT 'available'
                CHECK (air_source_status IN ('available', 'unavailable')),
            contract_version INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO sensor_readings_v17 (
            reading_id, advertisement_id, sensor_id, legacy_packet_id, observed_at,
            received_at, soil_temperature_c, moisture_percent, conductivity_us_cm,
            air_temperature_c, air_humidity_percent, soil_ph, nitrogen_mg_kg,
            phosphorus_mg_kg, potassium_mg_kg, soil_source_status,
            air_source_status, contract_version
        )
        SELECT reading_id, advertisement_id, sensor_id, sequence, observed_at,
               received_at, soil_temperature_c, moisture_percent, conductivity_us_cm,
               air_temperature_c, air_humidity_percent, soil_ph, nitrogen_mg_kg,
               phosphorus_mg_kg, potassium_mg_kg, soil_source_status,
               air_source_status, contract_version
        FROM sensor_readings;

        DROP TABLE sensor_readings;
        DROP TABLE advertisements;
        ALTER TABLE advertisements_v17 RENAME TO advertisements;
        ALTER TABLE sensor_readings_v17 RENAME TO sensor_readings;
        CREATE INDEX advertisements_by_sensor_time
        ON advertisements(sensor_id, advertisement_id DESC);
        CREATE INDEX advertisements_by_status_time
        ON advertisements(decode_status, advertisement_id DESC);
        CREATE INDEX sensor_readings_by_sensor_time
        ON sensor_readings(sensor_id, observed_at DESC);
        /* The report key. Repeated advertising of one report is absorbed here. */
        CREATE UNIQUE INDEX sensor_readings_by_report
        ON sensor_readings(sensor_id, report_id) WHERE report_id IS NOT NULL;

        /* What each report's supplementary packet said, kept whichever packet
           arrived first. It is copied onto the reading once both are here, and
           stays so that the packet repeating can be told from a conflict. */
        CREATE TABLE report_supplements (
            sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
            report_id INTEGER NOT NULL CHECK (report_id BETWEEN 1 AND 4294967295),
            received_at TEXT NOT NULL,
            battery_percent INTEGER
                CHECK (battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100),
            battery_voltage_v REAL,
            soil_ph REAL,
            nitrogen_mg_kg INTEGER,
            phosphorus_mg_kg INTEGER,
            potassium_mg_kg INTEGER,
            force_report INTEGER NOT NULL DEFAULT 0 CHECK (force_report IN (0, 1)),
            PRIMARY KEY (sensor_id, report_id)
        );
    """,
    18: """
        /* Contract v3 durable delivery. A forced report is now an ordinary
           report, so the supplementary packet no longer marks one and the
           column goes. The table is rebuilt rather than altered so that the
           migration does not depend on the SQLite version's DROP COLUMN.
           Nothing references report_supplements, so it can simply be
           replaced. */
        CREATE TABLE report_supplements_v18 (
            sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
            report_id INTEGER NOT NULL CHECK (report_id BETWEEN 1 AND 4294967295),
            received_at TEXT NOT NULL,
            battery_percent INTEGER
                CHECK (battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100),
            battery_voltage_v REAL,
            soil_ph REAL,
            nitrogen_mg_kg INTEGER,
            phosphorus_mg_kg INTEGER,
            potassium_mg_kg INTEGER,
            PRIMARY KEY (sensor_id, report_id)
        );
        INSERT INTO report_supplements_v18 (
            sensor_id, report_id, received_at, battery_percent, battery_voltage_v,
            soil_ph, nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg
        )
        SELECT sensor_id, report_id, received_at, battery_percent, battery_voltage_v,
               soil_ph, nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg
        FROM report_supplements;
        DROP TABLE report_supplements;
        ALTER TABLE report_supplements_v18 RENAME TO report_supplements;

        /* When the hub last told the sensor this report is stored, which is
           when the sensor may drop it. Null for a report never acknowledged,
           every report stored before this migration included. */
        ALTER TABLE sensor_readings ADD COLUMN acknowledged_at TEXT;

        /* A report with a conflict is never acknowledged, and that is asked
           every time a report is complete or repeated. */
        CREATE INDEX advertisements_conflicts_by_report
        ON advertisements(sensor_id, report_id) WHERE decode_status = 'conflict';
    """,
    19: """
        /* The supplementary packet's battery group now says whether the pack
           is charging. Null wherever the sensor did not say, which is every
           report stored before this migration. Both places a supplement's
           battery is kept get it, so that a repeated packet is still told
           from a conflict. */
        ALTER TABLE report_supplements ADD COLUMN battery_charging INTEGER
            CHECK (battery_charging IS NULL OR battery_charging IN (0, 1));
        ALTER TABLE sensor_readings ADD COLUMN battery_charging INTEGER
            CHECK (battery_charging IS NULL OR battery_charging IN (0, 1));
    """,
    20: """
        /* The two packets of a report are now called packet 1 and packet 2,
           in the database as everywhere else. Nothing else changes.

           The packet kind is named in a CHECK on advertisements, which cannot
           be altered in place, so advertisements is rebuilt the way migration
           17 rebuilt it: sensor_readings holds a foreign key into it and
           foreign keys stay enforced inside this transaction, so the readings
           table is rebuilt beside it, pointing at the new advertisements, and
           the old tables are dropped child first. Every row keeps its ID. */
        CREATE TABLE advertisements_v20 (
            advertisement_id INTEGER PRIMARY KEY,
            sensor_id TEXT REFERENCES sensors(sensor_id),
            report_id INTEGER
                CHECK (report_id IS NULL OR report_id BETWEEN 1 AND 4294967295),
            packet_kind TEXT
                CHECK (packet_kind IS NULL OR
                       packet_kind IN ('packet1', 'packet2', 'beacon')),
            /* The 8-bit packet ID of contract v2, kept so nothing already
               recorded is lost. Nothing reads it. */
            legacy_packet_id INTEGER,
            received_at TEXT NOT NULL,
            transport TEXT NOT NULL,
            source_adapter TEXT NOT NULL,
            observed_identifier TEXT NOT NULL,
            rssi INTEGER,
            contract_version INTEGER,
            payload_sha256 TEXT NOT NULL,
            decode_status TEXT NOT NULL
                CHECK (decode_status IN ('accepted', 'duplicate', 'conflict', 'rejected')),
            decode_error TEXT,
            service_data BLOB
        );
        INSERT INTO advertisements_v20 (
            advertisement_id, sensor_id, report_id, packet_kind, legacy_packet_id,
            received_at, transport, source_adapter, observed_identifier, rssi,
            contract_version, payload_sha256, decode_status, decode_error,
            service_data
        )
        SELECT advertisement_id, sensor_id, report_id,
               CASE packet_kind
                   WHEN 'main' THEN 'packet1'
                   WHEN 'supplementary' THEN 'packet2'
                   ELSE packet_kind
               END,
               legacy_packet_id, received_at, transport, source_adapter,
               observed_identifier, rssi, contract_version, payload_sha256,
               decode_status, decode_error, service_data
        FROM advertisements;

        CREATE TABLE sensor_readings_v20 (
            reading_id INTEGER PRIMARY KEY,
            /* The packet 1 the reading came from. */
            advertisement_id INTEGER UNIQUE
                REFERENCES advertisements_v20(advertisement_id) ON DELETE RESTRICT,
            sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
            /* Null for readings stored before contract v3. */
            report_id INTEGER
                CHECK (report_id IS NULL OR report_id BETWEEN 1 AND 4294967295),
            /* The 8-bit packet ID or sample number earlier readings carried,
               kept so nothing already recorded is lost. Nothing reads it. */
            legacy_packet_id INTEGER,
            /* Null when the sensor did not know the time. received_at is never
               copied in its place. */
            observed_at TEXT,
            received_at TEXT NOT NULL,
            soil_temperature_c REAL,
            moisture_percent REAL,
            conductivity_us_cm INTEGER,
            air_temperature_c REAL,
            air_humidity_percent REAL,
            soil_ph REAL,
            nitrogen_mg_kg INTEGER,
            phosphorus_mg_kg INTEGER,
            potassium_mg_kg INTEGER,
            battery_percent INTEGER
                CHECK (battery_percent IS NULL OR battery_percent BETWEEN 0 AND 100),
            battery_voltage_v REAL,
            soil_source_status TEXT NOT NULL DEFAULT 'available'
                CHECK (soil_source_status IN ('available', 'unavailable')),
            air_source_status TEXT NOT NULL DEFAULT 'available'
                CHECK (air_source_status IN ('available', 'unavailable')),
            contract_version INTEGER NOT NULL DEFAULT 0,
            /* When the hub last told the sensor this report is stored. */
            acknowledged_at TEXT,
            battery_charging INTEGER
                CHECK (battery_charging IS NULL OR battery_charging IN (0, 1))
        );
        INSERT INTO sensor_readings_v20 (
            reading_id, advertisement_id, sensor_id, report_id, legacy_packet_id,
            observed_at, received_at, soil_temperature_c, moisture_percent,
            conductivity_us_cm, air_temperature_c, air_humidity_percent, soil_ph,
            nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg, battery_percent,
            battery_voltage_v, soil_source_status, air_source_status,
            contract_version, acknowledged_at, battery_charging
        )
        SELECT reading_id, advertisement_id, sensor_id, report_id, legacy_packet_id,
               observed_at, received_at, soil_temperature_c, moisture_percent,
               conductivity_us_cm, air_temperature_c, air_humidity_percent, soil_ph,
               nitrogen_mg_kg, phosphorus_mg_kg, potassium_mg_kg, battery_percent,
               battery_voltage_v, soil_source_status, air_source_status,
               contract_version, acknowledged_at, battery_charging
        FROM sensor_readings;

        DROP TABLE sensor_readings;
        DROP TABLE advertisements;
        ALTER TABLE advertisements_v20 RENAME TO advertisements;
        ALTER TABLE sensor_readings_v20 RENAME TO sensor_readings;
        CREATE INDEX advertisements_by_sensor_time
        ON advertisements(sensor_id, advertisement_id DESC);
        CREATE INDEX advertisements_by_status_time
        ON advertisements(decode_status, advertisement_id DESC);
        CREATE INDEX advertisements_conflicts_by_report
        ON advertisements(sensor_id, report_id) WHERE decode_status = 'conflict';
        CREATE INDEX sensor_readings_by_sensor_time
        ON sensor_readings(sensor_id, observed_at DESC);
        CREATE UNIQUE INDEX sensor_readings_by_report
        ON sensor_readings(sensor_id, report_id) WHERE report_id IS NOT NULL;

        /* Nothing references the packet-2 table and it has no CHECK naming a
           packet, so a rename carries every row and its primary key along. */
        ALTER TABLE report_supplements RENAME TO report_packet2;
    """,
    21: """
        /* Grow lights. A light's settings belong to its driver and are kept as
           JSON the hub never looks inside. A light belongs to at most one plant,
           so two schedules never compete for it, and the schedule belongs to the
           plant, so it survives the plant's sensor being replaced. */
        CREATE TABLE lights (
            light_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            driver TEXT NOT NULL,
            config TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE TABLE plant_lights (
            light_id TEXT PRIMARY KEY REFERENCES lights(light_id) ON DELETE CASCADE,
            plant_id INTEGER NOT NULL REFERENCES plants(plant_id) ON DELETE CASCADE
        );
        CREATE INDEX plant_lights_by_plant ON plant_lights(plant_id);
        CREATE TABLE plant_light_schedules (
            plant_id INTEGER PRIMARY KEY REFERENCES plants(plant_id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            on_time TEXT NOT NULL,
            off_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (on_time != off_time)
        );
        CREATE TABLE light_events (
            event_id INTEGER PRIMARY KEY,
            light_id TEXT NOT NULL REFERENCES lights(light_id) ON DELETE CASCADE,
            at TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('schedule', 'watchdog', 'manual')),
            wanted INTEGER NOT NULL CHECK (wanted IN (0, 1)),
            outcome TEXT NOT NULL CHECK (outcome IN ('confirmed', 'unconfirmed', 'unreachable')),
            detail TEXT
        );
        CREATE INDEX light_events_by_light ON light_events(light_id, event_id DESC);
    """,
    22: """
        /* The plant journey's figures, kept rather than counted again on every
           poll. Nothing here is a second source of truth: every column can be
           rebuilt from the readings and care events it was derived from, which
           is how a plant from before this table gets its first row.

           What depends on the passing of time is deliberately absent. A light
           that is on now, and a watering window that becomes missed twenty-four
           hours after nothing happened, are worked out when the page asks. A
           stored number cannot become true by itself. */
        CREATE TABLE plant_journey (
            sensor_id TEXT PRIMARY KEY REFERENCES sensors(sensor_id) ON DELETE CASCADE,
            first_reading_at TEXT,
            last_reading_at TEXT,
            watering_count INTEGER NOT NULL DEFAULT 0,
            fertilizing_count INTEGER NOT NULL DEFAULT 0,
            /* Windows already answered: a watering came, late or not. The ones
               still open are counted at read time, because their answer is the
               clock. */
            settled_missed_count INTEGER NOT NULL DEFAULT 0,
            /* Light the plant has had, in whole seconds, from intervals that
               have closed. A lamp on right now is added when the page asks. */
            lighting_seconds INTEGER NOT NULL DEFAULT 0,
            /* Which lamps are on, and since when: {"light-a": "2026-09-28T08:00:00Z"} */
            lighting_open_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
    """,
    23: """
        /* The plant is the subject; a sensor is an instrument attached to it.
           A watering happened to a plant, and the sensor is only how the hub
           came to know, so a care event carries both: the plant it belongs to,
           and the sensor that noticed, where one did.

           This is what lets a plant exist with no sensor at all, and it means
           replacing a sensor no longer orphans everything that happened to the
           plant it was watching. */
        CREATE TABLE care_events_v23 (
            event_id TEXT PRIMARY KEY,
            plant_id INTEGER REFERENCES plants(plant_id) ON DELETE CASCADE,
            sensor_id TEXT,
            kind TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence TEXT NOT NULL,
            changes_json TEXT NOT NULL,
            /* An event belongs to something. One of the two is enough: events
               a sensor produced before anybody claimed it have no plant yet,
               and are adopted when it is enrolled. */
            CHECK (plant_id IS NOT NULL OR sensor_id IS NOT NULL)
        );
        INSERT INTO care_events_v23 (
            event_id, plant_id, sensor_id, kind, detected_at, title, summary,
            confidence, changes_json
        )
        SELECT events.event_id, sensors.plant_id, events.sensor_id, events.kind,
               events.detected_at, events.title, events.summary, events.confidence,
               events.changes_json
        FROM care_events AS events
        LEFT JOIN sensors ON sensors.sensor_id = events.sensor_id;
        DROP TABLE care_events;
        ALTER TABLE care_events_v23 RENAME TO care_events;
        CREATE INDEX care_events_by_plant ON care_events(plant_id, detected_at DESC);
        CREATE INDEX care_events_by_sensor ON care_events(sensor_id, detected_at DESC);

        /* The journey is the plant's lifetime, so it is keyed by the plant.
           A sensor nobody has claimed has no plant and no journey; enrolling
           it gives it both. */
        CREATE TABLE plant_journey_v23 (
            plant_id INTEGER PRIMARY KEY REFERENCES plants(plant_id) ON DELETE CASCADE,
            first_reading_at TEXT,
            last_reading_at TEXT,
            watering_count INTEGER NOT NULL DEFAULT 0,
            fertilizing_count INTEGER NOT NULL DEFAULT 0,
            settled_missed_count INTEGER NOT NULL DEFAULT 0,
            lighting_seconds INTEGER NOT NULL DEFAULT 0,
            lighting_open_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        INSERT OR REPLACE INTO plant_journey_v23 (
            plant_id, first_reading_at, last_reading_at, watering_count,
            fertilizing_count, settled_missed_count, lighting_seconds,
            lighting_open_json, updated_at
        )
        SELECT sensors.plant_id, journey.first_reading_at, journey.last_reading_at,
               journey.watering_count, journey.fertilizing_count,
               journey.settled_missed_count, journey.lighting_seconds,
               journey.lighting_open_json, journey.updated_at
        FROM plant_journey AS journey
        JOIN sensors ON sensors.sensor_id = journey.sensor_id
        WHERE sensors.plant_id IS NOT NULL;
        DROP TABLE plant_journey;
        ALTER TABLE plant_journey_v23 RENAME TO plant_journey;
    """,
    24: """
        /* A plant that stands on its own: no sensor, so nothing about it is
           discovered. Where it lives, when it was taken on, and how often it
           usually wants water, which is the fallback until there is enough
           weight history to answer better. */
        ALTER TABLE plants ADD COLUMN room_id INTEGER REFERENCES rooms(room_id);
        ALTER TABLE plants ADD COLUMN created_at TEXT;
        ALTER TABLE plants ADD COLUMN expected_watering_days INTEGER
            CHECK (expected_watering_days IS NULL OR
                   expected_watering_days BETWEEN 1 AND 365);
        /* Plants that came from a sensor already have a room on the sensor;
           carry it across so the two agree from the start. */
        UPDATE plants SET room_id = (
            SELECT sensors.room_id FROM sensors
            WHERE sensors.plant_id = plants.plant_id AND sensors.room_id IS NOT NULL
            LIMIT 1
        );
    """,
    25: """
        /* Every weighing of a pot, kept.

           For a pot, mass is the only measure of water with no soil-type
           assumption and no calibration: the probe reads two centimetres
           around its needles, the scale reads the whole pot. Grams, because
           that is what a scale measures, and the only unit the page
           ever offered after kilograms were dropped.

           kind says what the weighing was for. 'watered' is the ceiling, set
           once drainage stops; 'thirsty' is the floor, set when the plant
           wanted water; everything else is just where it is now. They are rows
           rather than two columns on the plant, so a repot or a season's
           growth adds a new ceiling and the old one stays in the history.

           source is 'manual' now. A load cell would write 'scale' rows into
           this same table, which is why it is not called manual_weights. */
        CREATE TABLE plant_weights (
            weight_id INTEGER PRIMARY KEY,
            plant_id INTEGER NOT NULL REFERENCES plants(plant_id) ON DELETE CASCADE,
            at TEXT NOT NULL,
            grams INTEGER NOT NULL CHECK (grams > 0),
            source TEXT NOT NULL CHECK (source IN ('manual', 'scale')),
            kind TEXT NOT NULL CHECK (kind IN ('reading', 'watered', 'thirsty')),
            note TEXT
        );
        CREATE INDEX plant_weights_by_plant ON plant_weights(plant_id, at DESC);
    """,
    26: """
        /* Whether the pot was on the scale at that moment.

           A weighing is an observation: the pot weighed 8.38 kg at noon. An
           anchor typed in from memory is a claim about the pot in a state it
           is not in now -- 'full, it weighs 17.4 kg' -- and dating that claim
           today does not mean the pot weighs 17.4 kg today. Only observed
           rows say what the pot weighs now or how fast it is drying; every
           row, observed or not, can serve as the newest anchor. */
        ALTER TABLE plant_weights ADD COLUMN observed INTEGER NOT NULL DEFAULT 1
            CHECK (observed IN (0, 1));
    """,
    27: """
        /* Which kind of the kind it is.

           A profile is the species: how dry the pot may get, what the roots
           tolerate. A variety is the named plant within it -- Marble Queen
           rather than Golden pothos -- which changes how fast it grows and
           how much light it wants to stay itself, but not what the roots
           will take. The profile holds the list; the plant holds the choice,
           by id, so renaming one in the catalog does not rename it here. */
        ALTER TABLE plants ADD COLUMN variety TEXT;
    """,
    28: """
        /* Photographs of a plant, and which one stands for it.

           A plant's history here is otherwise all numbers, and numbers do not
           show a new leaf or a scorched one. The file lives on disk named by
           the digest of its contents; this table says what it is a picture
           of. Two plants may be filed under the same photograph -- a shelf of
           them -- which is two rows and one file.

           The cover is the one shown where there is room for only one. It is
           a column on the plant rather than a flag on the photo, because
           "which is the cover" is a question about the plant and a flag would
           let two of them answer yes. */
        CREATE TABLE plant_photos (
            photo_id INTEGER PRIMARY KEY,
            plant_id INTEGER NOT NULL REFERENCES plants(plant_id) ON DELETE CASCADE,
            digest TEXT NOT NULL,
            content_type TEXT NOT NULL,
            bytes INTEGER NOT NULL CHECK (bytes > 0),
            added_at TEXT NOT NULL,
            caption TEXT
        );
        CREATE INDEX plant_photos_by_plant ON plant_photos(plant_id, added_at DESC);
        ALTER TABLE plants ADD COLUMN cover_photo_id INTEGER
            REFERENCES plant_photos(photo_id) ON DELETE SET NULL;
    """,
    29: """
        /* When the photograph was taken, as against when it was uploaded.

           A plant's record is meant to outlast the year it was started in,
           and a picture from three summers ago filed under the day somebody
           got round to uploading it would put it at the wrong end of that.
           The camera knows; the browser reads it off the file and sends it,
           because the picture itself arrives stripped of its metadata.

           Null where nothing said: added_at stands in, and is not pretended
           to be the same thing. */
        ALTER TABLE plant_photos ADD COLUMN taken_at TEXT;
    """,
    30: """
        /* When the plant came home, which is not when the hub met it.

           created_at is the day a row was made, and most of these rows were
           made long after the plant arrived. How long something has lived
           here is worth saying on its card, and only its owner knows it, so
           it is asked for rather than inferred. Null until then, and
           created_at stands in. */
        ALTER TABLE plants ADD COLUMN acquired_at TEXT;
    """,
    31: """
        /* The order the sections of a plant's page are read in.

           One order for the whole household rather than one per plant:
           somebody who wants the next watering at the top wants it at the
           top of every plant, and setting that nine times is not a feature.

           Kept on the hub rather than in the browser so it follows whoever
           set it from the phone to the desk. A list of section names as
           JSON; names the hub does not know are dropped on the way in, and
           sections missing from it keep their built-in place, so a release
           that adds a section does not need everybody to rearrange. */
        ALTER TABLE hub_settings ADD COLUMN plant_section_order TEXT;
    """,
    32: """
        /* Kinds of care event the log is to leave out.

           A plant under a lamp collects fifty lighting lines a fortnight,
           and somebody looking for when they last fed it has to read past
           all of them. Which kinds are worth seeing is a standing
           preference rather than a plant's property, so it is kept here
           beside the section order and applies to every plant. */
        ALTER TABLE hub_settings ADD COLUMN care_log_hidden_kinds TEXT;
    """,
    33: """
        /* Whether this plant's cover follows its newest photograph.

           A plant photographed every few weeks to watch it grow wants the
           most recent picture on its card, and saying so once is better
           than choosing again every time. Per plant rather than for the
           household: one plant is a record of growth and the next has one
           good picture of it that nothing will beat. */
        ALTER TABLE plants ADD COLUMN cover_follows_latest INTEGER NOT NULL DEFAULT 0
            CHECK (cover_follows_latest IN (0, 1));
    """,
    34: """
        /* What the plant cost and where it came from.

           The at-home date already says when; a household keeping plants
           for years wants the other two halves of the same sentence. Where
           answers the question a dying plant raises -- whether the shop is
           worth going back to -- and only the receipt knows it.

           Money is whole cents, never a float: a price typed as dollars and
           stored as 12.99 comes back as 12.989999999999998, and a sum over
           a shelf of plants drifts. The display currency is the household's
           and is not stored, because a hub serves one home. */
        ALTER TABLE plants ADD COLUMN purchase_price_cents INTEGER
            CHECK (purchase_price_cents IS NULL OR purchase_price_cents >= 0);
        ALTER TABLE plants ADD COLUMN purchase_place TEXT;
    """,
    35: """
        /* What this probe reads against what the liquid actually is.

           The soil probe answers pH on a scale of its own: in salted tap
           water that should be near neutral it reports about 5.0. One
           measured reference turns that into a usable number, and the
           correction belongs here rather than in the firmware so it can be
           changed without a cable.

           A calibration is only true under the conditions it was taken in,
           so those are kept beside it: what the probe read, what the
           reference said, when, and the conductivity at the time. The
           probe's answer drifts with conductivity, and a correction found
           at 790 uS/cm is not promised to hold at 8000. Kept per sensor,
           because the offset describes one piece of hardware. */
        ALTER TABLE sensors ADD COLUMN soil_ph_offset REAL NOT NULL DEFAULT 0;
        ALTER TABLE sensors ADD COLUMN soil_ph_reference REAL;
        ALTER TABLE sensors ADD COLUMN soil_ph_reference_raw REAL;
        ALTER TABLE sensors ADD COLUMN soil_ph_reference_at TEXT;
        ALTER TABLE sensors ADD COLUMN soil_ph_reference_ec INTEGER;
    """,
    36: """
        /* A scale the hub polls, rather than a probe that advertises itself.

           It is a sensor like any other -- it is adopted, named, given a
           room and pointed at a plant -- but it is reached over HTTP at an
           address instead of heard over Bluetooth, so that address is what
           identifies it. Everything it reports goes into plant_weights,
           where the hand-typed weights already go, so the water use,
           forecast and drainage built on them need no new path.

           The poll interval is the sensor's own; how often a weight is
           kept is separate, because the scale answers ten times a second
           and a pot does not change that fast. */
        ALTER TABLE sensors ADD COLUMN scale_address TEXT;
        ALTER TABLE sensors ADD COLUMN scale_keep_every_seconds INTEGER NOT NULL DEFAULT 1800;
        ALTER TABLE sensors ADD COLUMN scale_keep_change_grams INTEGER NOT NULL DEFAULT 20;
        /* What the scale last said, so the console can show it without
           waiting for the next weight to be worth keeping. */
        ALTER TABLE sensors ADD COLUMN scale_grams INTEGER;
        ALTER TABLE sensors ADD COLUMN scale_status TEXT;
        ALTER TABLE sensors ADD COLUMN scale_stable INTEGER;
        ALTER TABLE sensors ADD COLUMN scale_read_at TEXT;
        ALTER TABLE sensors ADD COLUMN scale_error TEXT;
    """,
    37: """
        /* What a scale says about itself beyond the weight.

           Firmware 1.0.0 can weigh once every few minutes instead of ten
           times a second, powering the amplifier down in between, and a
           reading then arrives with an age: it may have been taken up to a
           whole interval ago. Stamping it with the time it was fetched
           would have put a five-minute-old weight on the wrong minute, and
           the drying rate is worked out from exactly those minutes.

           seq is kept so the same reading fetched twice is stored once. */
        ALTER TABLE sensors ADD COLUMN scale_interval_seconds INTEGER;
        ALTER TABLE sensors ADD COLUMN scale_firmware TEXT;
        ALTER TABLE sensors ADD COLUMN scale_build TEXT;
        ALTER TABLE sensors ADD COLUMN scale_seq INTEGER;
    """,
    38: """
        /* Keeping a weight because it moved turned out not to be wanted.

           A pot is weighed on a timer and that is enough: a watering shows
           up at the next one, and the extra rule only made the record
           lumpier in a way nobody asked to read. The column goes rather
           than lingering as a setting nothing consults. */
        ALTER TABLE sensors DROP COLUMN scale_keep_change_grams;
    """,
    39: """
        /* A third anchor, below thirsty: what the pot weighs dry.

           Thirsty is a decision -- the weight at which this plant should be
           watered -- and it is set wherever the owner wants the cycle to
           end. Dry is a fact about the pot: substrate with no plant-available
           water left in it. The two were being asked to do one job, and a
           percentage measured from a decision is a percentage of nothing in
           particular. Measured from dry it is the fraction of the water the
           pot can actually hold, which is the number a person means when
           they ask how wet it is.

           The kind column is spelt out in a CHECK, and SQLite cannot widen
           one in place, so the table is rebuilt. Every row is carried over
           unchanged; nothing is reinterpreted. */
        CREATE TABLE plant_weights_rebuilt (
            weight_id INTEGER PRIMARY KEY,
            plant_id INTEGER NOT NULL REFERENCES plants(plant_id) ON DELETE CASCADE,
            at TEXT NOT NULL,
            grams INTEGER NOT NULL CHECK (grams > 0),
            source TEXT NOT NULL CHECK (source IN ('manual', 'scale')),
            kind TEXT NOT NULL CHECK (kind IN ('reading', 'watered', 'thirsty', 'dry')),
            note TEXT,
            observed INTEGER NOT NULL DEFAULT 1 CHECK (observed IN (0, 1))
        );
        INSERT INTO plant_weights_rebuilt
            (weight_id, plant_id, at, grams, source, kind, note, observed)
        SELECT weight_id, plant_id, at, grams, source, kind, note, observed
        FROM plant_weights;
        DROP TABLE plant_weights;
        ALTER TABLE plant_weights_rebuilt RENAME TO plant_weights;
        CREATE INDEX plant_weights_by_plant ON plant_weights(plant_id, at DESC);
    """,
    40: """
        /* What a scale has left in its cell.

           It does not come with the weight: the weight endpoint answers
           what it weighs and nothing else, and the charge is in the status
           the scale's own page uses. So it is asked for separately and far
           less often -- a battery does not move between one weighing and
           the next, and every request costs the radio a wake. */
        ALTER TABLE sensors ADD COLUMN scale_battery_volts REAL;
        ALTER TABLE sensors ADD COLUMN scale_battery_percent INTEGER;
    """,
    41: """
        /* Which Zigbee devices have been let in.

           A device that joins the radio network is not thereby adopted: it
           has announced itself, nothing more. Keeping the accepted ones here
           means an unexpected arrival stays visible and inert until somebody
           says yes, rather than quietly appearing among the working sensors.

           A JSON list of EUI-64s, the only identifier a Zigbee device keeps
           across rejoins. */
        ALTER TABLE hub_settings ADD COLUMN zigbee_accepted TEXT;
    """,
    42: """
        /* Zigbee devices are sensors, not a species of their own.

           Version 41 kept the accepted ones in a list here, which was a
           second sensor registry standing next to the first: no names, no
           plants, no rooms, no archiving, because none of the machinery for
           those knew such a thing existed. The sensors table already has
           every column it needs -- a stable identity, an enrollment status
           that includes "unclaimed", a plant and a room -- so a device that
           joins the radio becomes a row there like anything else. */
        ALTER TABLE hub_settings DROP COLUMN zigbee_accepted;
    """,
    43: """
        /* When a scale's weight is worth writing down.

           Until now this was only an interval: keep one reading every so
           many seconds. That gives a dense drying curve, which is what the
           forecast wants, and it makes two days hard to compare, because
           each day's weights were taken at whatever time the clock happened
           to land on.

           A time of day is the other useful answer: one weight a day, taken
           at the same hour, so a week of them is a week of like for like.
           Null means the interval is in charge, which is what every scale
           has done so far. */
        ALTER TABLE sensors ADD COLUMN scale_keep_at_hour INTEGER;
    """,
    44: """
        /* A battery's charge over time, so how fast it drains is visible.

           One row an hour per sensor, not one per reading. A scale
           reporting every ten seconds would otherwise write three million
           rows a year to describe a curve that moves a few percent a day,
           and a chart covering a year cannot draw them anyway. An hour is
           finer than anything a battery does and keeps a decade under a
           hundred thousand rows.

           The hour is the key, so a second reading in the same hour
           replaces the first rather than accumulating. Last writer wins:
           the most recent sample in an hour is the one worth keeping. */
        CREATE TABLE battery_history (
            sensor_id   TEXT NOT NULL,
            hour        TEXT NOT NULL,   /* YYYY-MM-DDTHH:00:00Z */
            percent     INTEGER,
            volts       REAL,
            PRIMARY KEY (sensor_id, hour)
        );
        CREATE INDEX battery_history_by_time ON battery_history (sensor_id, hour DESC);
    """,
    45: """
        /* Which board an image was built for.

           The soil probe is an ESP32-C3 and the scale and gateway are C6.
           Until now the library refused anything that was not a C3, so the
           scale's firmware could not be stored at all -- and once it can
           be, the hub has to know which board each image belongs to so it
           never offers a C3 image to a C6. */
        ALTER TABLE firmware_images ADD COLUMN chip TEXT NOT NULL DEFAULT 'esp32c3';
    """,
    46: """
        /* The name a Zigbee device answers to when its Wi-Fi is on.

           The device announces this over Zigbee when it joins and every
           ten minutes after, and the hub kept it in memory -- so a hub
           restart lost it until the next announcement. That was merely
           untidy while Wi-Fi was always on, and became a blocker once it
           normally is not: an update has to switch Wi-Fi on and then know
           where to send the image, and waiting up to ten minutes to be
           told a name the hub has known for months is not a plan. */
        ALTER TABLE sensors ADD COLUMN zigbee_host TEXT;
    """,
    49: """
        /* When the Zigbee gateway started, and why.

           A coordinator that restarts loses the children it relays for,
           and until its device table was persisted that meant silently
           discarding every reading from a device it could no longer
           place. The restarts themselves were invisible: nothing recorded
           them, so three evenings of silence looked like three separate
           mysteries rather than one cause.

           Kept in the database rather than in memory because the thing
           that used to restart the gateway was the hub restarting, which
           would have wiped the evidence along with it. */
        CREATE TABLE gateway_restarts (
            restart_id INTEGER PRIMARY KEY AUTOINCREMENT,
            at         TEXT NOT NULL,
            reason     TEXT,
            ran_for_s  INTEGER
        );
        CREATE INDEX gateway_restarts_by_time ON gateway_restarts (at DESC);
    """,
    47: """
        /* What every Zigbee device said, kept for a week.

           Until now a report was judged the instant it arrived and then
           thrown away: only the last one survived, in memory, for the
           control panel to display. That left no room to be clever and
           none to be wrong. A weight wanted at six in the morning had to
           wait for the device to speak at six in the morning, because
           there was nothing to look back at; a hub restarted at midnight
           knew nothing until the next report; and a reading judged wrongly
           was judged wrongly forever, with no record to re-read.

           So the raw report is kept as the device sent it -- its own
           values, under its own names, with the signal it arrived on --
           and the judging happens over that. A week is enough to answer
           every question anybody has asked of it so far and small enough
           that nobody has to think about the size: a device reporting
           every half hour leaves under four hundred rows.

           The values are stored as the device's own JSON rather than
           split into columns, because the next device will have different
           ones and a schema change per device model is not a plan. */
        CREATE TABLE zigbee_reports (
            report_id INTEGER PRIMARY KEY,
            eui TEXT NOT NULL,
            at TEXT NOT NULL,
            kind TEXT,
            values_json TEXT NOT NULL,
            lqi INTEGER,
            rssi INTEGER
        );
        CREATE INDEX zigbee_reports_by_device ON zigbee_reports(eui, at DESC);
        CREATE INDEX zigbee_reports_by_age ON zigbee_reports(at);
    """,
    50: """
        /* How much light this fixture lands on its plant.

           PPFD -- photosynthetic photon flux density, micromoles of light
           in the 400-700 nm band per square metre per second -- measured
           once at the leaf, because a fixed lamp at a fixed height
           delivers a fixed amount. Everything else about a day's light is
           then arithmetic over the switching log the hub already keeps:

               DLI = ppfd * seconds lit / 1e6

           Kept as one number per light rather than multiplied into each
           day, so a better measurement later corrects the whole history
           instead of only the days after it. A rough figure is still
           useful: it scales every day equally, so the comparison between
           this week and last is right even when the absolute is not. */
        ALTER TABLE lights ADD COLUMN ppfd_umol_m2s REAL;
    """,
    51: """
        /* Light reaching a plant belongs to the pair, not the fixture.

           One panel can cover a bench of plants, and what each one
           receives depends on where it stands under it -- directly
           beneath at 30 cm, or off to the side at 80. Hung on the light,
           a single PPFD claimed they all get the same, which is the one
           thing that is certainly untrue.

           So the figure moves onto plant_lights, and that table stops
           being one-light-one-plant: its key becomes the pair. A light
           keeps only what a light does, which is switch on and off. */
        CREATE TABLE plant_lights_rebuilt (
            light_id TEXT NOT NULL REFERENCES lights(light_id) ON DELETE CASCADE,
            plant_id INTEGER NOT NULL REFERENCES plants(plant_id) ON DELETE CASCADE,
            ppfd_umol_m2s REAL,
            PRIMARY KEY (light_id, plant_id)
        );
        INSERT INTO plant_lights_rebuilt (light_id, plant_id, ppfd_umol_m2s)
            SELECT plant_lights.light_id, plant_lights.plant_id, lights.ppfd_umol_m2s
            FROM plant_lights
            JOIN lights ON lights.light_id = plant_lights.light_id;
        DROP TABLE plant_lights;
        ALTER TABLE plant_lights_rebuilt RENAME TO plant_lights;
        CREATE INDEX plant_lights_by_plant ON plant_lights(plant_id);
        ALTER TABLE lights DROP COLUMN ppfd_umol_m2s;
    """,
    52: """
        /* The hours belong to the lamp, not to the plant.

           A light is one switch. When a panel covers several plants, their
           schedules were separate rows that could disagree, and nothing
           physical could honour both -- the hub picked one and said
           nothing. Hung on the light, the question cannot be asked twice.

           A plant now records only which lamps it stands under, and how
           much of each reaches it. Schedules carry over by way of
           plant_lights; where several plants shared one lamp the earliest
           plant's window wins, which is exactly the one the hub was
           already following. */
        CREATE TABLE light_schedules (
            light_id TEXT PRIMARY KEY REFERENCES lights(light_id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            on_time TEXT NOT NULL,
            off_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (on_time != off_time)
        );
        INSERT INTO light_schedules (light_id, enabled, on_time, off_time, updated_at)
            SELECT plant_lights.light_id,
                   plant_light_schedules.enabled,
                   plant_light_schedules.on_time,
                   plant_light_schedules.off_time,
                   plant_light_schedules.updated_at
            FROM plant_lights
            JOIN plant_light_schedules
                ON plant_light_schedules.plant_id = plant_lights.plant_id
            WHERE plant_lights.plant_id = (
                SELECT MIN(inner_pair.plant_id) FROM plant_lights AS inner_pair
                WHERE inner_pair.light_id = plant_lights.light_id
            );
        DROP TABLE plant_light_schedules;
    """,
    53: """
        /* Light reaching a plant is the plant's, because that is what a
           meter reads.

           Held on the plant-light pair it asked for a figure nobody can
           take: a PAR meter at the leaf measures everything arriving at
           once, and cannot say how much of it came from which lamp. The
           number a person can actually produce is one per plant, measured
           where it stands.

           It is kept whether or not a lamp is linked -- a plant's spot has
           a brightness before the hub switches anything -- while the daily
           total still needs a lamp, since only a switching log says for
           how long. */
        ALTER TABLE plants ADD COLUMN ppfd_umol_m2s REAL;
        UPDATE plants SET ppfd_umol_m2s = (
            SELECT MAX(plant_lights.ppfd_umol_m2s) FROM plant_lights
            WHERE plant_lights.plant_id = plants.plant_id
        );
        ALTER TABLE plant_lights DROP COLUMN ppfd_umol_m2s;
    """,
    54: """
        /* How long a plant is lit, where nothing the hub switches can say.

           A lamp on a mechanical timer, or a bright window, lights a plant
           just as a hub-controlled panel does -- there is simply no
           switching log to read the hours from. Without them the daily
           total cannot be worked out at all, and a measured PPFD sat on
           the plant doing nothing.

           Typed hours are a standing assumption rather than an
           observation: they do not know about the day the blinds stayed
           shut. So they are kept apart from the logged ones and the page
           says which it is showing, rather than letting a figure somebody
           typed look like a figure something measured. */
        ALTER TABLE plants ADD COLUMN light_hours_per_day REAL;
    """,
    64: """
        -- What else is on the scale with the plant.
        --
        -- A probe in the soil, a cache pot, a saucer: things that are
        -- weighed with the pot and are not the plant. The weight that
        -- is stored is the plant's, so everything built on it -- the
        -- drying rate, the anchors, the forecast -- keeps meaning
        -- what it meant.
        ALTER TABLE plants ADD COLUMN weight_offset_grams INTEGER NOT NULL DEFAULT 0;

        -- And what was taken off this particular weighing, so the
        -- number read off the scale can still be worked out. Storing
        -- only the net would hide what was measured; storing the
        -- gross as well would be two figures to keep agreeing.
        ALTER TABLE plant_weights ADD COLUMN offset_grams INTEGER NOT NULL DEFAULT 0;
    """,
    63: """
        -- How long a pulse cycle is, when the fan is asked for less
        -- than it can turn at.
        --
        -- This fan cannot run below about a quarter speed without
        -- buzzing, and a quarter speed for a whole minute removes
        -- ten points of humidity from a box that is only ever two or
        -- three over. The answer is not a gain: it is to run the fan
        -- at its floor for part of a window and leave it off for the
        -- rest, which is the same trick a slow PWM plays on a heater.
        ALTER TABLE fan_control ADD COLUMN pulse_window_s INTEGER NOT NULL DEFAULT 120;
    """,
    62: """
        -- What the fan was told, so the control can be judged.
        --
        -- Written when the speed changes rather than on every tick:
        -- the loop wakes twice a minute and the fan moves a few times
        -- an hour, so a row per tick would be thousands a day saying
        -- the same thing. A step between two rows is the fan holding.
        CREATE TABLE IF NOT EXISTS fan_log (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            at       TEXT NOT NULL,
            speed    REAL NOT NULL,
            rh       REAL,
            mode     TEXT NOT NULL,
            why      TEXT
        );
        CREATE INDEX IF NOT EXISTS fan_log_by_time ON fan_log (at);
    """,
    61: """
        -- One box, one fan. A single row rather than a table of fans
        -- and drivers: there is one enclosure with one exhaust fan,
        -- and a second would be a reason to generalise, not a reason
        -- to have guessed.
        CREATE TABLE IF NOT EXISTS fan_control (
            singleton_id  INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            enabled       INTEGER NOT NULL DEFAULT 0,
            mode          TEXT NOT NULL DEFAULT 'off'
                          CHECK (mode IN ('off', 'manual', 'auto')),
            moonraker_url TEXT NOT NULL DEFAULT 'http://vzbot.local',
            fan_name      TEXT NOT NULL DEFAULT 'Exhaust_fan',
            sensor_id     TEXT,
            target_rh     REAL NOT NULL DEFAULT 65,
            -- Per %RH. Ki and Kd are per minute, because a reading
            -- every seven minutes makes per-second gains unreadable.
            kp            REAL NOT NULL DEFAULT 0.05,
            ki            REAL NOT NULL DEFAULT 0.002,
            -- Zero on purpose: a difference between two readings
            -- minutes apart, each rounded to a tenth, is mostly noise.
            kd            REAL NOT NULL DEFAULT 0,
            manual_speed  REAL NOT NULL DEFAULT 0.1,
            -- Below this the fan hums and does nothing.
            min_speed     REAL NOT NULL DEFAULT 0.05,
            max_speed     REAL NOT NULL DEFAULT 1.0,
            -- A controller acting on a reading older than this is
            -- guessing, so it stops and the fan goes off.
            stale_after_s INTEGER NOT NULL DEFAULT 1800
        );
        INSERT OR IGNORE INTO fan_control (singleton_id) VALUES (1);
    """,
    60: """
        -- "Full at" stops being the same thing as "it was watered".
        --
        -- Both were a row of kind 'watered': the Set button wrote one
        -- with observed=0, a watering wrote one with observed=1, and
        -- the ceiling was whichever came last. So every watering
        -- silently redefined how much water the pot can hold -- and
        -- once the hub started tagging waterings by itself, the
        -- ceiling moved with no one touching it.
        --
        -- 'full' is the stated ceiling, and only the Set button
        -- writes it. 'watered' goes back to meaning the pot was
        -- watered, which is an event and not a measurement of
        -- capacity.
        -- `kind` is a CHECK constraint and SQLite cannot alter one,
        -- so the table is rebuilt around the wider list.
        CREATE TABLE plant_weights_with_full (
            weight_id INTEGER PRIMARY KEY,
            plant_id INTEGER NOT NULL REFERENCES plants(plant_id) ON DELETE CASCADE,
            at TEXT NOT NULL,
            grams INTEGER NOT NULL CHECK (grams > 0),
            source TEXT NOT NULL CHECK (source IN ('manual', 'scale')),
            kind TEXT NOT NULL CHECK (kind IN ('reading', 'watered', 'thirsty', 'dry', 'full')),
            note TEXT,
            observed INTEGER NOT NULL DEFAULT 1 CHECK (observed IN (0, 1))
        );
        INSERT INTO plant_weights_with_full
            (weight_id, plant_id, at, grams, source, kind, note, observed)
        SELECT weight_id, plant_id, at, grams, source, kind, note, observed
        FROM plant_weights;
        DROP TABLE plant_weights;
        ALTER TABLE plant_weights_with_full RENAME TO plant_weights;
        CREATE INDEX plant_weights_by_plant ON plant_weights(plant_id, at DESC);

        UPDATE plant_weights SET kind = 'full'
        WHERE kind = 'watered' AND COALESCE(observed, 1) = 0;

        -- A plant whose ceiling was only ever a real weighing keeps
        -- the number it had, now stated outright rather than inferred
        -- from the last watering.
        INSERT INTO plant_weights (plant_id, at, grams, source, kind, observed)
        SELECT w.plant_id, w.at, w.grams, 'manual', 'full', 0
        FROM plant_weights w
        WHERE w.kind = 'watered'
          AND w.at = (SELECT MAX(at) FROM plant_weights
                      WHERE plant_id = w.plant_id AND kind = 'watered')
          AND NOT EXISTS (SELECT 1 FROM plant_weights f
                          WHERE f.plant_id = w.plant_id AND f.kind = 'full');
    """,
    59: """
        -- A rule is one line under one measurement on one device.
        --
        -- Nothing here names a kind of device: a rule on
        -- air_temperature_c works on anything that reports it, which
        -- is what stops a new sensor arriving with no way to alarm on
        -- it. `test` is above, below or true -- true because `wet` is
        -- a boolean and bending it into `above 0.5` would be a trick
        -- the next reader has to undo.
        --
        -- repeat_seconds: 0 sounds once, otherwise it sounds again
        -- every N seconds until the reading leaves the condition or
        -- somebody presses stop.
        CREATE TABLE IF NOT EXISTS alert_rules (
            rule_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id      TEXT NOT NULL REFERENCES sensors(sensor_id) ON DELETE CASCADE,
            measurement    TEXT NOT NULL,
            test           TEXT NOT NULL CHECK (test IN ('above', 'below', 'true')),
            threshold      REAL,
            sound          TEXT,
            repeat_seconds INTEGER NOT NULL DEFAULT 0,
            push           INTEGER NOT NULL DEFAULT 1,
            enabled        INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS alert_rules_by_sensor
            ON alert_rules (sensor_id);

        -- The leak alarm was written into the service. It becomes a
        -- rule like any other, seeded here for every leak sensor
        -- already enrolled so nobody's alarm stops working at an
        -- upgrade. Water repeats until somebody comes.
        INSERT INTO alert_rules (sensor_id, measurement, test, sound,
                                 repeat_seconds, push)
        SELECT sensor_id, 'wet', 'true', 'Submarine', 20, 1
        FROM sensors
        WHERE transport = 'zigbee-leak' AND enrollment_status = 'enrolled';
    """,
    58: """
        -- Where to send an alarm. The whole URL, because
        -- https://ntfy.sh/<topic> and the same path on somebody's own
        -- server are one string, and splitting it into a server and a
        -- topic would be a switch for a decision the address states.
        -- NULL means nothing is sent, which is what a hub nobody has
        -- configured should do.
        ALTER TABLE hub_settings ADD COLUMN ntfy_url TEXT;
    """,
    57: """
        -- How often a Zigbee device is asked to report, per device.
        -- NULL means the hub's default, so a device nobody has opinions
        -- about keeps working and a row written before this existed
        -- means "whatever the default is now" rather than "never".
        ALTER TABLE sensors ADD COLUMN zigbee_report_max_seconds INTEGER;
        ALTER TABLE sensors ADD COLUMN zigbee_report_sensitivity TEXT;
    """,
    56: """
        -- How long raw Zigbee reports are kept. NULL means for ever,
        -- which is the default: the reports are small, disk is cheap,
        -- and a week's window had already thrown away the history
        -- somebody wanted to chart. Anyone who would rather not keep
        -- them can set a number of days and the hub will prune to it.
        ALTER TABLE hub_settings ADD COLUMN zigbee_report_keep_days INTEGER;
    """,
    55: """
        /* How old a plant is, as the date it started rather than a number
           of months.

           Age decides what its symptoms mean. A young monstera makes
           entire leaves because it is young, not because it is dark, and
           a page that reads that as too little light tells somebody to
           move a perfectly happy plant. The same goes for a strelitzia
           that has not flowered and a jasmine that has not bloomed.

           A date rather than an age because an age is wrong the day after
           it is typed, and nobody comes back to correct it. This is when
           the plant began -- sown, propagated, or its best guess -- and
           is not acquired_at, which is when it came here: a plant bought
           mature is old and newly arrived. */
        ALTER TABLE plants ADD COLUMN started_at TEXT;
    """,
    48: """
        /* The hub's own time zone, named rather than inferred.

           Until now anything that needed a wall clock -- the hour a scale
           writes the day's weight, the day a watering belongs to -- read it
           off whatever the machine believed, and the scale schedule did not
           even do that: it read the hour off UTC, so six in the morning
           meant eleven the night before in Vancouver.

           Reading the machine's zone instead would have fixed that one and
           left the setting invisible, which is the wrong trade for a hub
           somebody may reach from a phone in another country, or run on a
           server that keeps UTC because servers do. So it is written down,
           as an IANA name, and everything that needs an hour of the day
           asks for it.

           NULL means the machine's own zone, which is the right default for
           a hub running on the kitchen computer and the only answer
           available before anybody has chosen. */
        ALTER TABLE hub_settings ADD COLUMN timezone TEXT;
    """,
}


def migrate_database(database: sqlite3.Connection) -> None:
    """Apply every pending SQLite migration in its own transaction."""
    current = int(database.execute("PRAGMA user_version").fetchone()[0])
    if current > DATABASE_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than supported version "
            f"{DATABASE_SCHEMA_VERSION}"
        )
    for version in range(current + 1, DATABASE_SCHEMA_VERSION + 1):
        script = MIGRATIONS[version]
        try:
            database.executescript(
                f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version={version};\nCOMMIT;"
            )
        except sqlite3.Error:
            database.rollback()
            raise