"""
Copyright (c) 2022 Aiven Ltd
See LICENSE for details
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ZKConfig:
    client_port: int
    admin_port: int
    path: str  # can't be a Path instance because it needs to be JSON serializable

    @staticmethod
    def from_dict(data: dict) -> "ZKConfig":
        return ZKConfig(
            data["client_port"],
            data["admin_port"],
            data["path"],
        )


@dataclass(frozen=True)
class KafkaDescription:
    version: str
    install_dir: Path
    download_url: str
    protocol_version: str


@dataclass
class KafkaConfig:
    datadir: str
    kafka_keystore_password: str
    kafka_port: int
    zookeeper_port: int

    @staticmethod
    def from_dict(data: dict) -> "KafkaConfig":
        return KafkaConfig(
            data["datadir"],
            data["kafka_keystore_password"],
            data["kafka_port"],
            data["zookeeper_port"],
        )
