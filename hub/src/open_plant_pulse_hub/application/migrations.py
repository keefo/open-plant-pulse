import sqlite3
from typing import Dict

DATABASE_SCHEMA_VERSION = 11

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