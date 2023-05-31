"""
Copyright (c) 2023 Aiven Ltd
See LICENSE for details
"""
from __future__ import annotations

from dataclasses import fields
from kafka import KafkaAdminClient, KafkaProducer, TopicPartition
from kafka.admin import NewTopic
from kafka.consumer.fetcher import ConsumerRecord
from kafka.errors import UnknownTopicOrPartitionError
from karapace.backup.api import _consume_records
from karapace.backup.backends.v3.readers import read_metadata
from karapace.backup.backends.v3.schema import Metadata
from karapace.backup.poll_timeout import PollTimeout
from karapace.config import Config, set_config_defaults
from karapace.constants import TOPIC_CREATION_TIMEOUT_MS
from karapace.kafka_utils import kafka_admin_from_config, kafka_consumer_from_config, kafka_producer_from_config
from karapace.version import __version__
from pathlib import Path
from tempfile import mkdtemp
from tests.integration.utils.cluster import RegistryDescription
from tests.integration.utils.kafka_server import KafkaServers
from typing import Iterator, NoReturn

import datetime
import json
import os
import pytest
import secrets
import shutil
import subprocess
import textwrap


@pytest.fixture(scope="function", name="karapace_config")
def config_fixture(
    kafka_servers: KafkaServers,
    registry_cluster: RegistryDescription,
) -> Config:
    return set_config_defaults(
        {
            "bootstrap_uri": kafka_servers.bootstrap_servers,
            "topic_name": registry_cluster.schemas_topic,
        }
    )


@pytest.fixture(scope="function", name="config_file")
def config_file_fixture(
    kafka_servers: KafkaServers,
    registry_cluster: RegistryDescription,
) -> Iterator[Path]:
    str_path = mkdtemp()
    directory_path = Path(str_path)
    file_path = directory_path / "config.json"
    try:
        file_path.write_text(
            json.dumps(
                {
                    "bootstrap_uri": kafka_servers.bootstrap_servers,
                    "topic_name": registry_cluster.schemas_topic,
                },
                indent=2,
            )
        )
        yield file_path
    finally:
        shutil.rmtree(directory_path)


@pytest.fixture(scope="function", name="kafka_admin")
def admin_fixture(karapace_config: Config) -> Iterator[KafkaAdminClient]:
    admin = kafka_admin_from_config(karapace_config)
    try:
        yield admin
    finally:
        admin.close()


@pytest.fixture(scope="function", name="new_topic")
def topic_fixture(kafka_admin: KafkaAdminClient) -> NewTopic:
    new_topic = NewTopic(secrets.token_hex(4), 1, 1)
    kafka_admin.create_topics([new_topic], timeout_ms=TOPIC_CREATION_TIMEOUT_MS)
    try:
        yield new_topic
    finally:
        kafka_admin.delete_topics([new_topic.name], timeout_ms=TOPIC_CREATION_TIMEOUT_MS)


@pytest.fixture(scope="function", name="producer")
def producer_fixture(karapace_config: Config) -> Iterator[KafkaProducer]:
    with kafka_producer_from_config(karapace_config) as producer:
        yield producer


def _raise(exception: Exception) -> NoReturn:
    raise exception


def test_roundtrip_from_kafka_state(
    tmp_path: Path,
    new_topic: NewTopic,
    producer: KafkaProducer,
    config_file: Path,
    admin_client: KafkaAdminClient,
    karapace_config: Config,
) -> None:
    # Populate the test topic.
    producer.send(
        new_topic.name,
        key=b"bar",
        value=b"foo",
        partition=0,
        timestamp_ms=1683474641,
    ).add_errback(_raise)
    producer.send(
        new_topic.name,
        key=b"foo",
        value=b"bar",
        partition=0,
        headers=[
            ("some-header", b"some header value"),
            ("other-header", b"some other header value"),
        ],
        timestamp_ms=1683474657,
    ).add_errback(_raise)
    producer.flush()

    # Execute backup creation.
    subprocess.run(
        [
            "karapace_schema_backup",
            "get",
            "--use-format-v3",
            "--config",
            str(config_file),
            "--topic",
            new_topic.name,
            "--location",
            str(tmp_path),
        ],
        capture_output=True,
        check=True,
    )

    # Verify exactly the expected file structure in the target path, and no residues
    # from temporary files.
    (backup_directory,) = tmp_path.iterdir()
    assert backup_directory.name == f"topic-{new_topic.name}"
    assert sorted(path.name for path in backup_directory.iterdir()) == [
        f"{new_topic.name}.metadata",
        f"{new_topic.name}:0.data",
    ]
    (metadata_path,) = backup_directory.glob("*.metadata")

    # Delete the source topic.
    admin_client.delete_topics([new_topic.name], timeout_ms=10_000)

    # todo: assert new topic uuid != old topic uuid?
    # Execute backup restoration.
    subprocess.run(
        [
            "karapace_schema_backup",
            "restore",
            "--config",
            str(config_file),
            "--topic",
            new_topic.name,
            "--location",
            str(metadata_path),
        ],
        capture_output=True,
        check=True,
    )

    # Verify restored topic.
    with kafka_consumer_from_config(karapace_config, new_topic.name) as consumer:
        (partition,) = consumer.partitions_for_topic(new_topic.name)
        first_record, second_record = _consume_records(
            consumer=consumer,
            topic_partition=TopicPartition(new_topic.name, partition),
            poll_timeout=PollTimeout.default(),
        )

    # First record.
    assert isinstance(first_record, ConsumerRecord)
    assert first_record.topic == new_topic.name
    assert first_record.partition == partition
    # Note: This might be unreliable due to not using idempotent producer, i.e. we have
    # no guarantee against duplicates currently.
    assert first_record.offset == 0
    assert first_record.timestamp == 1683474641
    assert first_record.timestamp_type == 0
    assert first_record.key == b"bar"
    assert first_record.value == b"foo"
    assert first_record.headers == []

    # Second record.
    assert isinstance(second_record, ConsumerRecord)
    assert second_record.topic == new_topic.name
    assert second_record.partition == partition
    assert second_record.offset == 1
    assert second_record.timestamp == 1683474657
    assert second_record.timestamp_type == 0
    assert second_record.key == b"foo"
    assert second_record.value == b"bar"
    assert second_record.headers == [
        ("some-header", b"some header value"),
        ("other-header", b"some other header value"),
    ]


def test_roundtrip_from_file(
    tmp_path: Path,
    config_file: Path,
    admin_client: KafkaAdminClient,
) -> None:
    topic_name = "6595c9c2"
    backup_directory = Path(__file__).parent.parent.resolve() / "test_data" / "backup_v3_single_partition"
    metadata_path = backup_directory / f"{topic_name}.metadata"
    with metadata_path.open("rb") as buffer:
        metadata = read_metadata(buffer)
    (data_file,) = metadata_path.parent.glob("*.data")

    # Make sure topic doesn't exist beforehand.
    try:
        admin_client.delete_topics([topic_name])
    except UnknownTopicOrPartitionError:
        print("No previously existing topic.")
    else:
        print("Deleted topic from previous run.")

    # Execute backup restoration.
    subprocess.run(
        [
            "karapace_schema_backup",
            "restore",
            "--config",
            str(config_file),
            "--topic",
            topic_name,
            "--location",
            str(metadata_path),
        ],
        capture_output=True,
        check=True,
    )

    # Execute backup creation.
    backup_start_time = datetime.datetime.now(datetime.timezone.utc)
    subprocess.run(
        [
            "karapace_schema_backup",
            "get",
            "--use-format-v3",
            "--config",
            str(config_file),
            "--topic",
            topic_name,
            "--location",
            str(tmp_path),
        ],
        capture_output=True,
        check=True,
    )
    backup_end_time = datetime.datetime.now(datetime.timezone.utc)

    # Verify exactly the expected file directory structure, no other files in target
    # path. This is important so that assert temporary files are properly cleaned up.
    (backup_directory,) = tmp_path.iterdir()
    assert backup_directory.name == f"topic-{topic_name}"
    assert sorted(path.name for path in backup_directory.iterdir()) == [
        f"{topic_name}.metadata",
        f"{topic_name}:0.data",
    ]

    # Parse metadata from file.
    (new_metadata_path,) = backup_directory.glob("*.metadata")
    with new_metadata_path.open("rb") as buffer:
        new_metadata = read_metadata(buffer)

    # Verify start and end timestamps are within expected range.
    assert backup_start_time < new_metadata.started_at
    assert new_metadata.started_at < new_metadata.finished_at
    assert new_metadata.finished_at < backup_end_time

    # Verify new version matches current version of Karapace.
    assert new_metadata.tool_version == __version__

    # Verify all fields other than timings and version match exactly.
    for field in fields(Metadata):
        if field.name in {"started_at", "finished_at", "tool_version"}:
            continue
        assert getattr(metadata, field.name) == getattr(new_metadata, field.name)

    # Verify data files are identical.
    (new_data_file,) = new_metadata_path.parent.glob("*.data")
    assert data_file.read_bytes() == new_data_file.read_bytes()


def no_color_env() -> dict[str, str]:
    env = os.environ.copy()
    try:
        del env["FORCE_COLOR"]
    except KeyError:
        pass
    return {**env, "COLUMNS": "100"}


class TestInspect:
    def test_can_inspect_v3(self) -> None:
        metadata_path = (
            Path(__file__).parent.parent.resolve() / "test_data" / "backup_v3_single_partition" / "6595c9c2.metadata"
        )

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "inspect",
                "--location",
                str(metadata_path),
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 0
        assert cp.stderr == b""
        assert json.loads(cp.stdout) == {
            "version": 3,
            "tool_name": "karapace",
            "tool_version": "3.4.6-67-g26d38c0",
            "started_at": "2023-05-12T14:24:45.932000+00:00",
            "finished_at": "2023-05-12T14:24:46.286000+00:00",
            "topic_name": "6595c9c2",
            "topic_id": None,
            "partition_count": 1,
            "checksum_algorithm": "xxhash3_64_be",
            "data_files": [
                {
                    "filename": "6595c9c2:0.data",
                    "partition": 0,
                    "checksum_hex": "f414f504a8e49313",
                    "record_count": 2,
                    "start_offset": 0,
                    "end_offset": 1,
                },
            ],
        }

    def test_can_inspect_v3_with_future_checksum_algorithm(self) -> None:
        metadata_path = (
            Path(__file__).parent.parent.resolve() / "test_data" / "backup_v3_future_algorithm" / "a5f7a413.metadata"
        )

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "inspect",
                "--location",
                str(metadata_path),
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 0
        assert cp.stderr.decode() == (
            "Warning! This file has an unknown checksum algorithm and cannot be restored with this version of \nKarapace.\n"
        )
        assert json.loads(cp.stdout) == {
            "version": 3,
            "tool_name": "karapace",
            "tool_version": "3.4.6-67-g26d38c0",
            "started_at": "2023-05-30T14:44:24.841000+00:00",
            "finished_at": "2023-05-30T14:44:25.168000+00:00",
            "topic_name": "a5f7a413",
            "topic_id": None,
            "partition_count": 1,
            "checksum_algorithm": "unknown",
            "data_files": [
                {
                    "filename": "a5f7a413:0.data",
                    "partition": 0,
                    "checksum_hex": "f414f504a8e49313",
                    "record_count": 2,
                    "start_offset": 0,
                    "end_offset": 1,
                },
            ],
        }

    def test_can_inspect_v2(self) -> None:
        backup_path = Path(__file__).parent.parent.resolve() / "test_data" / "test_restore_v2.log"

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "inspect",
                "--location",
                str(backup_path),
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 0
        assert cp.stderr == b""
        assert json.loads(cp.stdout) == {"version": 2}

    def test_can_inspect_v1(self) -> None:
        backup_path = Path(__file__).parent.parent.resolve() / "test_data" / "test_restore_v1.log"

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "inspect",
                "--location",
                str(backup_path),
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 0
        assert cp.stderr == b""
        assert json.loads(cp.stdout) == {"version": 1}


class TestVerify:
    def test_can_verify_file_integrity(self) -> None:
        metadata_path = (
            Path(__file__).parent.parent.resolve() / "test_data" / "backup_v3_single_partition" / "6595c9c2.metadata"
        )

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "verify",
                f"--location={metadata_path!s}",
                "--level=file",
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 0
        assert cp.stderr == b""
        assert cp.stdout.decode() == textwrap.dedent(
            """\
            Integrity of 6595c9c2:0.data is intact.
            ✅ Verified 1 data files in backup OK.
            """
        )

    def test_can_verify_record_integrity(self) -> None:
        metadata_path = (
            Path(__file__).parent.parent.resolve() / "test_data" / "backup_v3_single_partition" / "6595c9c2.metadata"
        )

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "verify",
                f"--location={metadata_path!s}",
                "--level=record",
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 0
        assert cp.stderr == b""
        assert cp.stdout.decode() == textwrap.dedent(
            """\
            Integrity of 6595c9c2:0.data is intact.
            ✅ Verified 1 data files in backup OK.
            """
        )

    def test_can_verify_file_integrity_from_large_topic(
        self,
        tmp_path: Path,
        new_topic: NewTopic,
        producer: KafkaProducer,
        config_file: Path,
    ) -> None:
        # Populate the test topic.
        for _ in range(100):
            producer.send(
                new_topic.name,
                key=1000 * b"a",
                value=1000 * b"b",
                partition=0,
            ).add_errback(_raise)
        producer.flush()

        # Execute backup creation.
        subprocess.run(
            [
                "karapace_schema_backup",
                "get",
                "--use-format-v3",
                f"--config={config_file!s}",
                f"--topic={new_topic.name!s}",
                f"--location={tmp_path!s}",
            ],
            capture_output=True,
            check=True,
        )
        metadata_path = tmp_path / f"topic-{new_topic.name}" / f"{new_topic.name}.metadata"

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "verify",
                f"--location={metadata_path!s}",
                "--level=file",
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 0
        assert cp.stderr == b""
        assert cp.stdout.decode() == textwrap.dedent(
            f"""\
            Integrity of {new_topic.name}:0.data is intact.
            ✅ Verified 1 data files in backup OK.
            """
        )

    def test_can_verify_record_integrity_from_large_topic(
        self,
        tmp_path: Path,
        new_topic: NewTopic,
        producer: KafkaProducer,
        config_file: Path,
    ) -> None:
        # Populate the test topic.
        for _ in range(100):
            producer.send(
                new_topic.name,
                key=1000 * b"a",
                value=1000 * b"b",
                partition=0,
            ).add_errback(_raise)
        producer.flush()

        # Execute backup creation.
        subprocess.run(
            [
                "karapace_schema_backup",
                "get",
                "--use-format-v3",
                f"--config={config_file!s}",
                f"--topic={new_topic.name}",
                f"--location={tmp_path!s}",
            ],
            capture_output=True,
            check=True,
        )
        metadata_path = tmp_path / f"topic-{new_topic.name}" / f"{new_topic.name}.metadata"

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "verify",
                f"--location={metadata_path}",
                "--level=record",
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 0
        assert cp.stderr == b""
        assert cp.stdout.decode() == textwrap.dedent(
            f"""\
            Integrity of {new_topic.name}:0.data is intact.
            ✅ Verified 1 data files in backup OK.
            """
        )

    def test_can_refute_file_integrity(self) -> None:
        metadata_path = (
            Path(__file__).parent.parent.resolve()
            / "test_data"
            / "backup_v3_corrupt_last_record_bit_flipped_no_checkpoints"
            / "a-topic.metadata"
        )

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "verify",
                f"--location={metadata_path!s}",
                "--level=file",
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 1
        assert cp.stderr == b""
        assert cp.stdout.decode() == textwrap.dedent(
            """\
            Integrity of a-topic:123.data is not intact!
            💥 Failed to verify integrity of some data files.
            """
        )

    def test_can_refute_record_integrity(self) -> None:
        metadata_path = (
            Path(__file__).parent.parent.resolve()
            / "test_data"
            / "backup_v3_corrupt_last_record_bit_flipped_no_checkpoints"
            / "a-topic.metadata"
        )

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "verify",
                f"--location={metadata_path!s}",
                "--level=record",
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 1
        assert cp.stderr == b""
        assert cp.stdout.decode() == textwrap.dedent(
            """\
            Integrity of a-topic:123.data is not intact!
                InvalidChecksum: Found checksum mismatch after reading full data file.
            💥 Failed to verify integrity of some data files.
            """
        )

    @pytest.mark.parametrize(
        ("test_file", "error_message"),
        (
            (
                "test_restore_v1.log",
                "Only backups using format V3 can be verified, found V1.\n",
            ),
            (
                "test_restore_v2.log",
                "Only backups using format V3 can be verified, found V2.\n",
            ),
        ),
    )
    def test_gives_non_successful_exit_code_for_legacy_backup_format(
        self,
        test_file: str,
        error_message: str,
    ) -> None:
        backup_path = Path(__file__).parent.parent.resolve() / "test_data" / test_file

        cp = subprocess.run(
            [
                "karapace_schema_backup",
                "verify",
                f"--location={backup_path}",
                "--level=file",
            ],
            capture_output=True,
            check=False,
            env=no_color_env(),
        )

        assert cp.returncode == 1
        assert cp.stderr.decode() == error_message
        assert cp.stdout == b""