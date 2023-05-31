"""
Copyright (c) 2023 Aiven Ltd
See LICENSE for details
"""
from __future__ import annotations

from kafka import KafkaConsumer, KafkaProducer
from kafka.admin import NewTopic
from kafka.errors import KafkaError, TopicAlreadyExistsError
from kafka.structs import PartitionMetadata
from karapace import config
from karapace.backup.api import _admin, _consumer, _maybe_create_topic, _producer, BackupVersion
from karapace.backup.errors import PartitionCountError
from karapace.config import Config
from karapace.constants import DEFAULT_SCHEMA_TOPIC, TOPIC_CREATION_TIMEOUT_MS
from types import FunctionType
from typing import Callable, ContextManager
from unittest import mock
from unittest.mock import MagicMock

import pytest

patch_admin_new = mock.patch(
    "karapace.backup.api.KafkaAdminClient.__new__",
    autospec=True,
)


class TestAdmin:
    @patch_admin_new
    def test_auto_closing(self, admin_new: MagicMock) -> None:
        admin_mock = admin_new.return_value
        with _admin(config.DEFAULTS) as admin:
            assert admin is admin_mock
        assert admin_mock.close.call_count == 1

    @mock.patch("time.sleep", autospec=True)
    @patch_admin_new
    def test_retries_on_kafka_error(self, admin_new: MagicMock, sleep_mock: MagicMock) -> None:
        admin_mock = admin_new.return_value
        admin_new.side_effect = [KafkaError("1"), KafkaError("2"), admin_mock]
        with _admin(config.DEFAULTS) as admin:
            assert admin is admin_mock
        assert sleep_mock.call_count == 2  # proof that we waited between retries
        assert admin_mock.close.call_count == 1

    @pytest.mark.parametrize("e", (KeyboardInterrupt, SystemExit, RuntimeError, MemoryError))
    @mock.patch("time.sleep", autospec=True)
    @patch_admin_new
    def test_reraises_unknown_exceptions(
        self,
        admin_new: MagicMock,
        sleep_mock: MagicMock,
        e: type[BaseException],
    ) -> None:
        admin_new.side_effect = e
        with pytest.raises(e), _admin(config.DEFAULTS):
            pass
        assert sleep_mock.call_count == 0  # proof that we did not retry


class TestMaybeCreateTopic:
    @patch_admin_new
    def test_calls_admin_create_topics(self, admin_new: MagicMock) -> None:
        create_topics: MagicMock = admin_new.return_value.create_topics
        _maybe_create_topic(config.DEFAULTS, DEFAULT_SCHEMA_TOPIC, BackupVersion.V1)

        create_topics.assert_called_once_with(mock.ANY, timeout_ms=TOPIC_CREATION_TIMEOUT_MS)
        ((new_topic,),) = create_topics.call_args.args
        assert isinstance(new_topic, NewTopic)
        assert new_topic.name == DEFAULT_SCHEMA_TOPIC
        assert new_topic.num_partitions == 1
        assert new_topic.replication_factor == config.DEFAULTS["replication_factor"]
        assert new_topic.topic_configs == {"cleanup.policy": "compact"}

    @patch_admin_new
    def test_gracefully_handles_topic_already_exists_error(self, admin_new: MagicMock) -> None:
        create_topics: MagicMock = admin_new.return_value.create_topics
        create_topics.side_effect = TopicAlreadyExistsError()
        _maybe_create_topic(config.DEFAULTS, DEFAULT_SCHEMA_TOPIC, BackupVersion.V2)
        create_topics.assert_called_once()

    @patch_admin_new
    def test_retries_for_kafka_errors(self, admin_new: MagicMock) -> None:
        create_topics: MagicMock = admin_new.return_value.create_topics
        create_topics.side_effect = [KafkaError("1"), KafkaError("2"), None]

        with mock.patch("time.sleep", autospec=True):
            _maybe_create_topic(config.DEFAULTS, DEFAULT_SCHEMA_TOPIC, BackupVersion.V2)

        assert create_topics.call_count == 3

    @pytest.mark.parametrize("version", (BackupVersion.V1, BackupVersion.V2))
    @patch_admin_new
    def test_noop_for_custom_name_on_legacy_versions(
        self,
        admin_new: MagicMock,
        version: BackupVersion,
    ) -> None:
        create_topics: MagicMock = admin_new.return_value.create_topics
        assert "custom-name" != DEFAULT_SCHEMA_TOPIC
        _maybe_create_topic(config.DEFAULTS, "custom-name", version)
        create_topics.assert_not_called()

    @patch_admin_new
    def test_allows_custom_name_on_v3(
        self,
        admin_new: MagicMock,
    ) -> None:
        create_topics: MagicMock = admin_new.return_value.create_topics
        topic_name = "custom-name"
        assert topic_name != DEFAULT_SCHEMA_TOPIC
        _maybe_create_topic(config.DEFAULTS, "custom-name", BackupVersion.V3)

        create_topics.assert_called_once_with(mock.ANY, timeout_ms=TOPIC_CREATION_TIMEOUT_MS)
        ((new_topic,),) = create_topics.call_args.args
        assert isinstance(new_topic, NewTopic)
        assert new_topic.name == topic_name
        assert new_topic.num_partitions == 1
        assert new_topic.replication_factor == config.DEFAULTS["replication_factor"]
        assert new_topic.topic_configs == {"cleanup.policy": "compact"}


class TestClients:
    @staticmethod
    def _partition_metadata(c: int = 1) -> set[PartitionMetadata]:
        return {PartitionMetadata("topic", i, 0, tuple(), tuple(), None) for i in range(0, c)}

    @pytest.mark.parametrize(
        "ctx_mng,client_class,partitions_method",
        (
            (_consumer, KafkaConsumer, KafkaConsumer.partitions_for_topic),
            (_producer, KafkaProducer, KafkaProducer.partitions_for),
        ),
    )
    def test_auto_closing(
        self,
        ctx_mng: Callable[[Config, str], ContextManager[KafkaConsumer | KafkaProducer]],
        client_class: type[KafkaConsumer | KafkaProducer],
        partitions_method: FunctionType,
    ) -> None:
        with mock.patch(f"{client_class.__module__}.{client_class.__qualname__}.__new__", autospec=True) as client_ctor:
            client_mock = client_ctor.return_value
            getattr(client_mock, partitions_method.__name__).return_value = self._partition_metadata()
            with ctx_mng(config.DEFAULTS, "topic") as client:
                assert client is client_mock
            assert client_mock.close.call_count == 1

    @pytest.mark.parametrize("partition_count", (0, 2))
    @pytest.mark.parametrize(
        "ctx_mng,client_class,partitions_method",
        (
            (_consumer, KafkaConsumer, KafkaConsumer.partitions_for_topic),
            (_producer, KafkaProducer, KafkaProducer.partitions_for),
        ),
    )
    def test_raises_partition_count_error_for_unexpected_count(
        self,
        ctx_mng: Callable[[Config, str], KafkaConsumer | KafkaProducer],
        client_class: type[KafkaConsumer | KafkaProducer],
        partitions_method: FunctionType,
        partition_count: int,
    ) -> None:
        with mock.patch(f"{client_class.__module__}.{client_class.__qualname__}.__new__", autospec=True) as client_ctor:
            client_mock = client_ctor.return_value
            getattr(client_mock, partitions_method.__name__).return_value = self._partition_metadata(partition_count)
            with pytest.raises(PartitionCountError):
                with ctx_mng(config.DEFAULTS, "topic") as client:
                    assert client == client_mock
            assert client_mock.close.call_count == 1