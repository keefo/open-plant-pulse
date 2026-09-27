import sqlite3
from typing import Dict

DATABASE_SCHEMA_VERSION = 18

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