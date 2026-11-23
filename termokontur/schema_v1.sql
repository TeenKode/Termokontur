-- Base schema for Termokontur databases without a user_version.

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    building TEXT NOT NULL,
    floor TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'Кабинет',
    min_temp REAL NOT NULL DEFAULT 18,
    max_temp REAL NOT NULL DEFAULT 24,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(building, name),
    CHECK(min_temp < max_temp)
);
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    value REAL NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('manual', 'max', 'ha')),
    reporter TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    external_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS readings_room_time ON readings(room_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS room_activity (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    changed_at TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE INDEX IF NOT EXISTS room_activity_at ON room_activity(room_id,changed_at,id);
CREATE TABLE IF NOT EXISTS pairing_codes (
    digest TEXT PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_bindings (
    user_id TEXT NOT NULL,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    PRIMARY KEY(user_id, room_id)
);
CREATE TABLE IF NOT EXISTS max_binding_periods (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS binding_open_period
    ON max_binding_periods(user_id,room_id) WHERE ends_at IS NULL;
CREATE INDEX IF NOT EXISTS binding_room_period ON max_binding_periods(room_id,starts_at,ends_at);
CREATE TABLE IF NOT EXISTS max_users (
    user_id TEXT PRIMARY KEY,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_account (
    id INTEGER PRIMARY KEY CHECK(id=1),
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS building_accounts (
    username TEXT PRIMARY KEY,
    building TEXT NOT NULL CHECK(building<>''),
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    session_key TEXT NOT NULL,
    max_user_id TEXT REFERENCES max_users(user_id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS max_inbox (
    event_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('processing', 'complete')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_outbox (
    event_id TEXT PRIMARY KEY REFERENCES max_inbox(event_id),
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS temperature_alert_state (
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    user_id TEXT NOT NULL,
    last_status TEXT NOT NULL DEFAULT 'normal',
    last_at TEXT NOT NULL DEFAULT '',
    last_reading_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(room_id, user_id)
);
CREATE TABLE IF NOT EXISTS max_bind_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    local_day TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(local_day, user_id)
);
CREATE TABLE IF NOT EXISTS max_user_sessions (
    user_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT '',
    room_id INTEGER,
    page INTEGER NOT NULL DEFAULT 0,
    local_day TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS max_reading_corrections (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    reading_id INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    old_observed_at TEXT NOT NULL,
    corrected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_outbox_ui (
    event_id TEXT PRIMARY KEY REFERENCES max_outbox(event_id) ON DELETE CASCADE,
    buttons_json TEXT,
    callback_id TEXT,
    chat_id TEXT,
    origin_mid TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'menu',
    notify INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS notification_batches (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    recipients INTEGER NOT NULL,
    silent INTEGER NOT NULL DEFAULT 0,
    payload_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_recipients (
    batch_id TEXT NOT NULL REFERENCES notification_batches(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    delivered_at TEXT,
    PRIMARY KEY(batch_id, user_id)
);
CREATE TABLE IF NOT EXISTS max_menu_messages (
    user_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    building TEXT NOT NULL DEFAULT '',
    frequency TEXT NOT NULL CHECK(frequency IN ('daily','weekly')),
    send_time TEXT NOT NULL,
    weekday INTEGER NOT NULL DEFAULT 0 CHECK(weekday BETWEEN 0 AND 6),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_dispatches (
    subscription_id INTEGER NOT NULL
        REFERENCES report_subscriptions(id) ON DELETE CASCADE,
    period_key TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY(subscription_id, period_key)
);
CREATE TABLE IF NOT EXISTS report_waiting (
    subscription_id INTEGER NOT NULL REFERENCES report_subscriptions(id) ON DELETE CASCADE,
    period_key TEXT NOT NULL,
    first_day TEXT NOT NULL,
    last_day TEXT NOT NULL,
    PRIMARY KEY(subscription_id,period_key)
);

CREATE TABLE IF NOT EXISTS sensor_migrations(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sensor_firmware(
  id INTEGER PRIMARY KEY, version TEXT NOT NULL UNIQUE, model TEXT NOT NULL,
  sha256 TEXT NOT NULL, image BLOB NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sensor_devices(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, room_id INTEGER NOT NULL REFERENCES rooms(id),
  enabled INTEGER NOT NULL DEFAULT 1, enrollment_hash TEXT, enrollment_until INTEGER NOT NULL,
  hardware_id TEXT UNIQUE, token_hash TEXT, config TEXT NOT NULL,
  created_at TEXT NOT NULL, last_seen INTEGER, firmware TEXT NOT NULL DEFAULT '',
  battery_mv INTEGER, rssi INTEGER, boots INTEGER NOT NULL DEFAULT 0,
  lost INTEGER NOT NULL DEFAULT 0, ack_seq INTEGER NOT NULL DEFAULT 1,
  config_version INTEGER NOT NULL DEFAULT 0, acknowledged_lost INTEGER NOT NULL DEFAULT 0, ota_failures INTEGER NOT NULL DEFAULT 0,
  target_firmware INTEGER REFERENCES sensor_firmware(id), ota_generation INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
  sensor_type TEXT NOT NULL DEFAULT 'sht4x',
  restore_pending INTEGER NOT NULL DEFAULT 0,
  last_complete_sync INTEGER);
CREATE TABLE IF NOT EXISTS sensor_records(
  device_id TEXT NOT NULL REFERENCES sensor_devices(id), seq INTEGER NOT NULL,
  room_id INTEGER NOT NULL REFERENCES rooms(id), measured_at INTEGER, received_at INTEGER NOT NULL,
  boot_id TEXT NOT NULL, uptime_s INTEGER NOT NULL, time_quality TEXT NOT NULL,
  temperature REAL, humidity REAL, battery_mv INTEGER NOT NULL, interval_s INTEGER NOT NULL,
  error TEXT NOT NULL, fingerprint TEXT NOT NULL,
  PRIMARY KEY(device_id,seq));
CREATE INDEX IF NOT EXISTS sensor_records_time ON sensor_records(device_id,measured_at);
CREATE INDEX IF NOT EXISTS sensor_humidity_time ON sensor_records(measured_at,room_id)
  WHERE humidity IS NOT NULL AND error='';
CREATE TABLE IF NOT EXISTS sensor_events(
  id INTEGER PRIMARY KEY, device_id TEXT NOT NULL REFERENCES sensor_devices(id),
  at INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sensor_alert_state(
  device_id TEXT NOT NULL REFERENCES sensor_devices(id), user_id TEXT NOT NULL,
  issue TEXT NOT NULL, event_id TEXT NOT NULL,
  PRIMARY KEY(device_id,user_id,issue));
CREATE TABLE IF NOT EXISTS sensor_mqtt_outbox(
  device_id TEXT PRIMARY KEY REFERENCES sensor_devices(id), generation INTEGER NOT NULL DEFAULT 1,
  attempts INTEGER NOT NULL DEFAULT 0, next_retry INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sensor_mqtt_settings(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
INSERT OR IGNORE INTO sensor_migrations VALUES (1), (2);

CREATE TABLE IF NOT EXISTS sensor_identities(
  fingerprint TEXT PRIMARY KEY, public_key TEXT NOT NULL UNIQUE,
  hardware_id TEXT NOT NULL, sensor_type TEXT NOT NULL, firmware TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected')),
  device_id TEXT UNIQUE REFERENCES sensor_devices(id),
  created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sensor_auth_challenges(
  nonce TEXT PRIMARY KEY, message TEXT NOT NULL, fingerprint TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sensor_auth_limits(
  bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, since INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sensor_installers(
  id INTEGER PRIMARY KEY, version TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
  source_commit TEXT NOT NULL, image BLOB NOT NULL, created_at TEXT NOT NULL);
