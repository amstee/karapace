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
    topic_name = "2db42756"
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
            Path(__file__).parent.parent.resolve() / "test_data" / "backup_v3_single_partition" / "2db42756.metadata"
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
            "tool_version": "3.4.6-65-g9259060",
            "started_at": "2023-05-08T09:31:56.238000+00:00",
            "finished_at": "2023-05-08T09:31:56.571000+00:00",
            "topic_name": "2db42756",
            "topic_id": None,
            "partition_count": 1,
            "checksum_algorithm": "xxhash3_64_be",
            "data_files": [
                {
                    "filename": "2db42756:0.data",
                    "partition": 0,
                    "checksum_hex": "f414f504a8e49313",
                    "record_count": 2,
                },
            ],
        }

    def test_can_inspect_v3_with_future_checksum_algorithm(self) -> None:
        metadata_path = (
            Path(__file__).parent.parent.resolve() / "test_data" / "backup_v3_future_algorithm" / "a-topic.metadata"
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
        assert (
            cp.stderr.decode()
            == "Warning! This file has an unknown checksum algorithm and cannot be restored with this version of Karapace.\n"
        )
        assert json.loads(cp.stdout) == {
            "version": 3,
            "tool_name": "karapace",
            "tool_version": "3.4.6-67-g26d38c0",
            "started_at": "2023-05-23T13:19:34.843000+00:00",
            "finished_at": "2023-05-23T13:19:34.843000+00:00",
            "topic_name": "a-topic",
            "topic_id": None,
            "partition_count": 1,
            "checksum_algorithm": "unknown",
            "data_files": [
                {
                    "filename": "a-topic:123.data",
                    "partition": 123,
                    "checksum_hex": "dc0e738f1e856010",
                    "record_count": 1,
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
