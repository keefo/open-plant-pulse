import sqlite3
from typing import Dict

DATABASE_SCHEMA_VERSION = 33

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
           that is what a scale measures; kilograms are an entry convenience.

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