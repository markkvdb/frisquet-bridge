"""Load/save the single TOML config + persisted RF state."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from frisquet_bridge.frame import ADDR_BOILER, ADDR_BOILER_ALT
from frisquet_bridge.protocol import default_serial_port


class ConfigError(ValueError):
    """Invalid or missing configuration."""


CONNECT_MODES = {"passive", "read", "full"}
ZONE_MODES = {"disabled", "satellite", "virtual_satellite", "simple_satellite", "central_boiler"}
SATELLITE_IDENTITY_MODES = {"virtual_satellite", "simple_satellite", "central_boiler"}
VIRTUAL_SATELLITE_MODES = {"virtual_satellite", "simple_satellite", "central_boiler"}
LOG_FORMATS = {"console", "json"}
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
MQTT_LANGUAGES = {"en", "fr"}


@dataclass
class SerialConfig:
    port: str = default_serial_port()
    speed: int = 115200


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = "frisquet-bridge"
    base_topic: str = "frisquet"
    language: str = "en"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "console"
    file: str = ""
    file_max_bytes: int = 10_000_000
    file_backup_count: int = 5
    raw_file: str = ""
    raw_max_bytes: int = 50_000_000
    raw_backup_count: int = 5
    raw_lines: bool = False


@dataclass
class DeviceIdentity:
    association_id: int
    request_id: int

    def state_kwargs(self, network_id: bytes, on_change: Callable[[object], None] | None = None) -> dict[str, object]:
        return {
            "network_id": network_id,
            "association_id": self.association_id,
            "request_id": self.request_id,
            "on_change": on_change,
        }


@dataclass
class ConnectConfig:
    mode: str
    identity: DeviceIdentity | None = None


@dataclass
class SondeConfig(DeviceIdentity):
    enabled: bool


@dataclass
class ZoneConfig:
    mode: str
    identity: DeviceIdentity | None = None

    @property
    def enabled(self) -> bool:
        return self.mode != "disabled"

    @property
    def uses_satellite(self) -> bool:
        return self.mode in SATELLITE_IDENTITY_MODES

    @property
    def uses_virtual_satellite(self) -> bool:
        return self.mode in VIRTUAL_SATELLITE_MODES

    @property
    def is_simple_satellite(self) -> bool:
        return self.mode == "simple_satellite"

    @property
    def is_central_boiler(self) -> bool:
        return self.mode == "central_boiler"

    @property
    def is_read_only_satellite(self) -> bool:
        return self.mode == "satellite"


@dataclass
class BridgeConfig:
    """Single-file configuration and persisted protocol state."""

    path: Path
    network_id: bytes
    boiler_addr: int
    sensor_poll_interval_seconds: float = 30.0
    connect: ConnectConfig | None = None
    sonde: SondeConfig | None = None
    zone1: ZoneConfig | None = None
    zone2: ZoneConfig | None = None
    zone3: ZoneConfig | None = None
    serial: SerialConfig = field(default_factory=SerialConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def memory_offset(self) -> int:
        return 0xC8 if self.boiler_addr == ADDR_BOILER_ALT else 0

    @property
    def state_path(self) -> Path:
        """Sidecar file for learned runtime state (e.g. zone schedules)."""
        return self.path.with_name(f"{self.path.stem}.state.json")

    @property
    def connect_reads_enabled(self) -> bool:
        return self.connect is not None and self.connect.identity is not None and self.connect.mode in {"read", "full"}

    @property
    def connect_writes_enabled(self) -> bool:
        return self.connect is not None and self.connect.identity is not None and self.connect.mode == "full"

    @property
    def connect_physical_zone_control_enabled(self) -> bool:
        return self.connect_writes_enabled

    @property
    def boiler_entities_enabled(self) -> bool:
        return self.connect is not None

    def protocol_state_kwargs(self, role: str) -> dict[str, object]:
        return self.identity(role).state_kwargs(self.network_id, self._persist_request_id(role))

    def identity(self, role: str) -> DeviceIdentity:
        if role == "connect":
            if self.connect is None or self.connect.identity is None:
                raise ConfigError("frisquet.connect identity is not configured")
            return self.connect.identity
        if role == "sonde":
            if self.sonde is None:
                raise ConfigError("frisquet.sonde is not configured")
            return self.sonde
        if role in {"satellite_z1", "satellite_z2", "satellite_z3"}:
            zone = self.zone(int(role[-1]))
            if zone is None or zone.identity is None:
                raise ConfigError(f"frisquet.zone{role[-1]} satellite identity is not configured")
            return zone.identity
        raise ConfigError(f"unknown identity role: {role}")

    def zone(self, number: int) -> ZoneConfig | None:
        if number not in (1, 2, 3):
            raise ConfigError(f"unknown zone: {number}")
        return getattr(self, f"zone{number}")

    def zone_enabled(self, number: int) -> bool:
        zone = self.zone(number)
        return zone is not None and zone.enabled

    def _persist_request_id(self, role: str) -> Callable[[object], None]:
        def persist(state: object) -> None:
            from frisquet_bridge.connect.state import ProtocolState
            from frisquet_bridge.state_store import save_protocol_request_id

            if isinstance(state, ProtocolState):
                self.identity(role).request_id = state.request_id
                save_protocol_request_id(self.state_path, role, state.request_id)

        return persist

    def set_identity(self, role: str, *, association_id: int, request_id: int) -> None:
        identity = DeviceIdentity(association_id=association_id, request_id=request_id)
        from frisquet_bridge.state_store import save_protocol_request_id

        if role == "connect":
            mode = self.connect.mode if self.connect else "full"
            self.connect = ConnectConfig(mode=mode, identity=identity)
            save_protocol_request_id(self.state_path, role, request_id)
            return
        if role == "sonde":
            enabled = self.sonde.enabled if self.sonde else True
            self.sonde = SondeConfig(enabled=enabled, **identity.__dict__)
            save_protocol_request_id(self.state_path, role, request_id)
            return
        if role in {"satellite_z1", "satellite_z2", "satellite_z3"}:
            n = int(role[-1])
            current = self.zone(n)
            mode = current.mode if current else "virtual_satellite"
            setattr(self, f"zone{n}", ZoneConfig(mode=mode, identity=identity))
            save_protocol_request_id(self.state_path, role, request_id)
            return
        raise ConfigError(f"unknown identity role: {role}")

    def apply_protocol_request_ids(self, request_ids: dict[str, int]) -> None:
        for role, request_id in request_ids.items():
            try:
                self.identity(role).request_id = request_id
            except ConfigError:
                continue

    def save(self) -> None:
        doc = tomlkit.document()
        frisquet = tomlkit.table()
        frisquet.add("network_id", self.network_id.hex())
        frisquet.add("boiler_addr", f"{self.boiler_addr:02x}")
        frisquet.add("sensor_poll_interval_seconds", self.sensor_poll_interval_seconds)
        if self.connect is not None:
            frisquet.add("connect", _connect_table(self.connect))
        if self.sonde is not None:
            frisquet.add("sonde", _sonde_table(self.sonde))
        for n in (1, 2, 3):
            zone = self.zone(n)
            if zone is not None:
                frisquet.add(f"zone{n}", _zone_table(zone))
        doc.add("frisquet", frisquet)

        serial = tomlkit.table()
        serial.add("port", self.serial.port)
        serial.add("speed", self.serial.speed)
        doc.add("serial", serial)

        mqtt = tomlkit.table()
        mqtt.add("enabled", self.mqtt.enabled)
        mqtt.add("host", self.mqtt.host)
        mqtt.add("port", self.mqtt.port)
        mqtt.add("username", self.mqtt.username)
        mqtt.add("password", self.mqtt.password)
        mqtt.add("client_id", self.mqtt.client_id)
        mqtt.add("base_topic", self.mqtt.base_topic)
        mqtt.add("language", self.mqtt.language)
        doc.add("mqtt", mqtt)

        logging = tomlkit.table()
        logging.add("level", self.logging.level)
        logging.add("format", self.logging.format)
        logging.add("file", self.logging.file)
        logging.add("file_max_bytes", self.logging.file_max_bytes)
        logging.add("file_backup_count", self.logging.file_backup_count)
        logging.add("raw_file", self.logging.raw_file)
        logging.add("raw_max_bytes", self.logging.raw_max_bytes)
        logging.add("raw_backup_count", self.logging.raw_backup_count)
        logging.add("raw_lines", self.logging.raw_lines)
        doc.add("logging", logging)

        self.path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def load(path: str | Path) -> BridgeConfig:
    path = Path(path)
    if not path.exists():
        cfg = BridgeConfig(path=path, network_id=b"\xff\xff\xff\xff", boiler_addr=ADDR_BOILER)
        cfg.save()
        return cfg

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    _validate_keys(data, {"frisquet", "serial", "mqtt", "logging"}, "config")
    frisquet = _required_table(data, "frisquet")
    _validate_keys(
        frisquet,
        {"network_id", "boiler_addr", "sensor_poll_interval_seconds", "connect", "sonde", "zone1", "zone2", "zone3"},
        "frisquet",
    )
    cfg = BridgeConfig(
        path=path,
        network_id=_parse_hex_bytes(_required_str(frisquet, "network_id", "frisquet.network_id"), 4, "frisquet.network_id"),
        boiler_addr=_parse_hex_u8(_required_str(frisquet, "boiler_addr", "frisquet.boiler_addr"), "frisquet.boiler_addr"),
    )
    if "sensor_poll_interval_seconds" in frisquet:
        cfg.sensor_poll_interval_seconds = _positive_float(
            frisquet["sensor_poll_interval_seconds"],
            "frisquet.sensor_poll_interval_seconds",
        )

    if "connect" in frisquet:
        cfg.connect = _load_connect(_required_table(frisquet, "connect"), "frisquet.connect")
    if "sonde" in frisquet:
        cfg.sonde = _load_sonde(_required_table(frisquet, "sonde"), "frisquet.sonde")

    for n in (1, 2, 3):
        key = f"zone{n}"
        if key in frisquet:
            setattr(cfg, key, _load_zone(_required_table(frisquet, key), f"frisquet.{key}", n))
    from frisquet_bridge.state_store import load_protocol_request_ids

    cfg.apply_protocol_request_ids(load_protocol_request_ids(cfg.state_path))
    _validate_zone_topology(cfg)

    serial = _optional_table(data, "serial") or {}
    _validate_keys(serial, {"port", "speed"}, "serial")
    if "port" in serial:
        cfg.serial.port = _required_str(serial, "port", "serial.port")
    if "speed" in serial:
        cfg.serial.speed = int(serial["speed"])

    mqtt = _optional_table(data, "mqtt") or {}
    _validate_keys(mqtt, {"enabled", "host", "port", "username", "password", "client_id", "base_topic", "language"}, "mqtt")
    if mqtt:
        if "enabled" in mqtt:
            cfg.mqtt.enabled = _required_bool(mqtt, "enabled", "mqtt.enabled")
        cfg.mqtt.host = str(mqtt.get("host", cfg.mqtt.host))
        cfg.mqtt.port = int(mqtt.get("port", cfg.mqtt.port))
        cfg.mqtt.username = str(mqtt.get("username", cfg.mqtt.username))
        cfg.mqtt.password = str(mqtt.get("password", cfg.mqtt.password))
        cfg.mqtt.client_id = str(mqtt.get("client_id", cfg.mqtt.client_id))
        cfg.mqtt.base_topic = str(mqtt.get("base_topic", cfg.mqtt.base_topic))
        cfg.mqtt.language = _parse_mqtt_language(str(mqtt.get("language", cfg.mqtt.language)), "mqtt.language")

    logging_config = _optional_table(data, "logging") or {}
    _validate_keys(
        logging_config,
        {
            "level",
            "format",
            "file",
            "file_max_bytes",
            "file_backup_count",
            "raw_file",
            "raw_max_bytes",
            "raw_backup_count",
            "raw_lines",
        },
        "logging",
    )
    if logging_config:
        cfg.logging.level = str(logging_config.get("level", cfg.logging.level)).upper()
        cfg.logging.format = str(logging_config.get("format", cfg.logging.format))
        cfg.logging.file = str(logging_config.get("file", cfg.logging.file))
        cfg.logging.file_max_bytes = int(logging_config.get("file_max_bytes", cfg.logging.file_max_bytes))
        cfg.logging.file_backup_count = int(logging_config.get("file_backup_count", cfg.logging.file_backup_count))
        cfg.logging.raw_file = str(logging_config.get("raw_file", cfg.logging.raw_file))
        cfg.logging.raw_max_bytes = int(logging_config.get("raw_max_bytes", cfg.logging.raw_max_bytes))
        cfg.logging.raw_backup_count = int(logging_config.get("raw_backup_count", cfg.logging.raw_backup_count))
        if "raw_lines" in logging_config:
            cfg.logging.raw_lines = _required_bool(logging_config, "raw_lines", "logging.raw_lines")
        _validate_logging(cfg.logging)

    return cfg


def _connect_table(config: ConnectConfig) -> tomlkit.items.Table:
    table = tomlkit.table()
    table.add("mode", config.mode)
    if config.identity is not None:
        table.add("association_id", f"{config.identity.association_id:02x}")
    return table


def _sonde_table(config: SondeConfig) -> tomlkit.items.Table:
    table = _identity_table(config)
    table.add("enabled", config.enabled)
    return table


def _identity_table(identity: DeviceIdentity) -> tomlkit.items.Table:
    table = tomlkit.table()
    table.add("association_id", f"{identity.association_id:02x}")
    return table


def _zone_table(zone: ZoneConfig) -> tomlkit.items.Table:
    table = tomlkit.table()
    table.add("mode", zone.mode)
    if zone.identity is not None:
        table.add("association_id", f"{zone.identity.association_id:02x}")
    return table


def _load_connect(table: dict[str, object], field_name: str) -> ConnectConfig:
    _validate_keys(table, {"mode", "association_id"}, field_name)
    mode = _parse_connect_mode(_required_str(table, "mode", f"{field_name}.mode"), f"{field_name}.mode")
    identity = _load_identity(table, field_name) if "association_id" in table else None
    if mode in {"read", "full"} and identity is None:
        raise ConfigError(f"missing required field: {field_name}.association_id")
    return ConnectConfig(mode=mode, identity=identity)


def _load_sonde(table: dict[str, object], field_name: str) -> SondeConfig:
    _validate_keys(table, {"enabled", "association_id"}, field_name)
    identity = _load_identity(table, field_name)
    return SondeConfig(
        enabled=_required_bool(table, "enabled", f"{field_name}.enabled"),
        **identity.__dict__,
    )


def _load_identity(table: dict[str, object], field_name: str) -> DeviceIdentity:
    association_id = _parse_hex_u8(
        _required_str(table, "association_id", f"{field_name}.association_id"),
        f"{field_name}.association_id",
    )
    return DeviceIdentity(association_id=association_id, request_id=0)


def _load_zone(table: dict[str, object], field_name: str, zone_number: int) -> ZoneConfig:
    _validate_keys(table, {"mode", "association_id"}, field_name)
    mode = _parse_zone_mode(_required_str(table, "mode", f"{field_name}.mode"), f"{field_name}.mode")
    if mode == "central_boiler" and zone_number != 1:
        raise ConfigError(f"{field_name}.mode: central_boiler is only valid on frisquet.zone1")
    identity = None
    if "association_id" in table or "request_id" in table:
        identity = _load_identity(table, field_name)
    if mode in SATELLITE_IDENTITY_MODES and identity is None:
        raise ConfigError(f"missing required field: {field_name}.association_id")
    return ZoneConfig(mode=mode, identity=identity)


def _validate_zone_topology(cfg: BridgeConfig) -> None:
    if cfg.zone1 is None or not cfg.zone1.is_central_boiler:
        return
    for n in (2, 3):
        zone = cfg.zone(n)
        if zone is not None and zone.enabled:
            raise ConfigError(f"frisquet.zone{n}.mode: central_boiler on zone1 requires zone{n} to be disabled")


def _validate_logging(config: LoggingConfig) -> None:
    if config.level not in LOG_LEVELS:
        choices = ", ".join(sorted(LOG_LEVELS))
        raise ConfigError(f"logging.level: expected one of {choices}, got {config.level!r}")
    if config.format not in LOG_FORMATS:
        choices = ", ".join(sorted(LOG_FORMATS))
        raise ConfigError(f"logging.format: expected one of {choices}, got {config.format!r}")
    if config.file_max_bytes <= 0:
        raise ConfigError("logging.file_max_bytes: expected positive integer")
    if config.file_backup_count < 0:
        raise ConfigError("logging.file_backup_count: expected non-negative integer")
    if config.raw_max_bytes <= 0:
        raise ConfigError("logging.raw_max_bytes: expected positive integer")
    if config.raw_backup_count < 0:
        raise ConfigError("logging.raw_backup_count: expected non-negative integer")


def _validate_keys(table: dict[str, object], allowed: set[str], field_name: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        expected = ", ".join(sorted(allowed))
        raise ConfigError(f"{field_name}: unknown field {unknown[0]!r}; expected one of: {expected}")


def _required_table(parent: dict[str, object], name: str) -> dict[str, object]:
    table = parent.get(name)
    if isinstance(table, dict):
        return table
    raise ConfigError(f"missing required table: {name}")


def _optional_table(parent: dict[str, object], name: str) -> dict[str, object] | None:
    if name not in parent:
        return None
    table = parent[name]
    if isinstance(table, dict):
        return table
    raise ConfigError(f"{name}: expected table")


def _required_str(parent: dict[str, object], key: str, field_name: str) -> str:
    if key not in parent:
        raise ConfigError(f"missing required field: {field_name}")
    value = parent[key]
    if not isinstance(value, str):
        raise ConfigError(f"{field_name}: expected string")
    return value


def _required_bool(parent: dict[str, object], key: str, field_name: str) -> bool:
    if key not in parent:
        raise ConfigError(f"missing required field: {field_name}")
    value = parent[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name}: expected boolean")
    return value


def _positive_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{field_name}: expected positive number")
    parsed = float(value)
    if parsed <= 0:
        raise ConfigError(f"{field_name}: expected positive number")
    return parsed


def _parse_hex_bytes(value: str, length: int, field_name: str) -> bytes:
    cleaned = value.replace(" ", "").lower()
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ConfigError(f"{field_name}: invalid hex") from exc
    if len(raw) != length:
        raise ConfigError(f"{field_name}: expected {length} bytes, got {len(raw)}")
    return raw


def _parse_hex_u8(value: str, field_name: str) -> int:
    return _parse_hex_bytes(value, 1, field_name)[0]


def _parse_connect_mode(value: str, field_name: str) -> str:
    if value not in CONNECT_MODES:
        choices = ", ".join(sorted(CONNECT_MODES))
        raise ConfigError(f"{field_name}: expected one of {choices}, got {value!r}")
    return value


def _parse_zone_mode(value: str, field_name: str) -> str:
    if value not in ZONE_MODES:
        choices = ", ".join(sorted(ZONE_MODES))
        raise ConfigError(f"{field_name}: expected one of {choices}, got {value!r}")
    return value


def _parse_mqtt_language(value: str, field_name: str) -> str:
    normalized = value.strip().casefold()
    if normalized not in MQTT_LANGUAGES:
        choices = ", ".join(sorted(MQTT_LANGUAGES))
        raise ConfigError(f"{field_name}: expected one of {choices}, got {value!r}")
    return normalized
