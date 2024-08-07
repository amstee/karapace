"""
karapace - Test schema reader

Copyright (c) 2023 Aiven Ltd
See LICENSE for details
"""

from _pytest.logging import LogCaptureFixture
from concurrent.futures import ThreadPoolExecutor
from confluent_kafka import Message
from dataclasses import dataclass
from karapace.config import DEFAULTS
from karapace.in_memory_database import InMemoryDatabase
from karapace.kafka.consumer import KafkaConsumer
from karapace.key_format import KeyFormatter
from karapace.offset_watcher import OffsetWatcher
from karapace.schema_reader import (
    KafkaSchemaReader,
    MAX_MESSAGES_TO_CONSUME_AFTER_STARTUP,
    MAX_MESSAGES_TO_CONSUME_ON_STARTUP,
    OFFSET_EMPTY,
    OFFSET_UNINITIALIZED,
)
from karapace.typing import SchemaId, Version
from tests.base_testcase import BaseTestCase
from unittest.mock import Mock

import confluent_kafka
import json
import logging
import pytest
import random
import time


def test_offset_watcher() -> None:
    watcher = OffsetWatcher()
    timeout = 0.5

    # A largish number of iteration useful to stress the code
    total_number_of_offsets = 100

    # A random sleep is added after every iteration of both the consumer and
    # the producer, the goal is to simulate race conditions were the producer
    # will see an event first,  even though the consumer is more likely of
    # doing so.
    max_sleep = 0.01

    assert timeout > max_sleep, "Bad configuration, test will fail."
    assert max_sleep * total_number_of_offsets < 5, "Bad configuration, test would be too slow."

    consumed_cnt = 0

    def consume() -> None:
        nonlocal consumed_cnt
        for offset in range(total_number_of_offsets):
            assert watcher.wait_for_offset(expected_offset=offset, timeout=timeout), "Event must be produced."
            consumed_cnt += 1
            sleep = random.uniform(0, max_sleep)
            time.sleep(sleep)

    produced_cnt = 0

    def produce() -> None:
        nonlocal produced_cnt
        for offset in range(total_number_of_offsets):
            watcher.offset_seen(new_offset=offset)
            produced_cnt += 1
            sleep = random.uniform(0, max_sleep)
            time.sleep(sleep)

    with ThreadPoolExecutor(max_workers=2) as executor:
        consumer = executor.submit(consume)
        producer = executor.submit(produce)
        assert consumer.result() is None, "Thread should finish without errors"
        assert producer.result() is None, "Thread should finish without errors"

    assert (
        watcher._greatest_offset == 99  # pylint: disable=protected-access
    ), "Expected greatest offset is not one less than total count"
    assert produced_cnt == 100, "Did not produce expected amount of records"
    assert consumed_cnt == 100, "Did not consume expected amount of records"


@dataclass
class ReadinessTestCase(BaseTestCase):
    cur_offset: int
    end_offset: int
    expected: bool


@pytest.mark.parametrize(
    "testcase",
    [
        ReadinessTestCase(
            test_name="Empty schemas topic",
            cur_offset=OFFSET_EMPTY,
            end_offset=0,
            expected=True,
        ),
        ReadinessTestCase(
            test_name="Schema topic with data, beginning offset is 0",
            cur_offset=OFFSET_EMPTY,
            end_offset=100,
            expected=False,
        ),
        ReadinessTestCase(
            test_name="Schema topic with single record",
            cur_offset=OFFSET_EMPTY,
            end_offset=1,
            expected=False,
        ),
        ReadinessTestCase(
            test_name="Beginning offset cannot be resolved.",
            cur_offset=OFFSET_UNINITIALIZED,
            end_offset=0,
            expected=False,
        ),
        ReadinessTestCase(
            test_name="Purged/compacted schemas topic, begin offset n > 0, end offset n+1",
            cur_offset=90,
            end_offset=91,
            expected=True,
        ),
        ReadinessTestCase(
            test_name="Schema topic with single record and replayed",
            cur_offset=0,
            end_offset=0,
            expected=True,
        ),
        ReadinessTestCase(
            test_name="Schema topic with data but compacted or purged, cur offset 10",
            cur_offset=10,
            end_offset=100,
            expected=False,
        ),
        ReadinessTestCase(
            test_name="Schema topic with data, cur offset is highest",
            cur_offset=99,
            end_offset=100,
            expected=True,
        ),
        ReadinessTestCase(
            test_name="Schema topic with data, cur offset is greater than highest",
            cur_offset=101,
            end_offset=100,
            expected=True,
        ),
    ],
)
def test_readiness_check(testcase: ReadinessTestCase) -> None:
    key_formatter_mock = Mock()
    consumer_mock = Mock()
    consumer_mock.consume.return_value = []
    # Return tuple (beginning, end), end offset is the next upcoming record offset
    consumer_mock.get_watermark_offsets.return_value = (0, testcase.end_offset)

    offset_watcher = OffsetWatcher()
    schema_reader = KafkaSchemaReader(
        config=DEFAULTS,
        offset_watcher=offset_watcher,
        key_formatter=key_formatter_mock,
        master_coordinator=None,
        database=InMemoryDatabase(),
    )
    schema_reader.consumer = consumer_mock
    schema_reader.offset = testcase.cur_offset

    schema_reader.handle_messages()
    assert schema_reader.ready is testcase.expected


def test_num_max_messages_to_consume_moved_to_one_after_ready() -> None:
    key_formatter_mock = Mock()
    consumer_mock = Mock()
    consumer_mock.consume.return_value = []
    # Return tuple (beginning, end), end offset is the next upcoming record offset
    consumer_mock.get_watermark_offsets.return_value = (0, 1)

    offset_watcher = OffsetWatcher()
    schema_reader = KafkaSchemaReader(
        config=DEFAULTS,
        offset_watcher=offset_watcher,
        key_formatter=key_formatter_mock,
        master_coordinator=None,
        database=InMemoryDatabase(),
    )
    schema_reader.consumer = consumer_mock
    schema_reader.offset = 0
    assert schema_reader.max_messages_to_process == MAX_MESSAGES_TO_CONSUME_ON_STARTUP

    schema_reader.handle_messages()
    assert schema_reader.ready is True
    assert schema_reader.max_messages_to_process == MAX_MESSAGES_TO_CONSUME_AFTER_STARTUP


def test_schema_reader_can_end_to_ready_state_if_last_message_is_invalid_in_schemas_topic() -> None:
    key_formatter_mock = Mock(spec=KeyFormatter)
    consumer_mock = Mock(spec=KafkaConsumer)

    schema_str = json.dumps(
        {"name": "init", "type": "record", "fields": [{"name": "inner", "type": ["string", "int"]}]}
    ).encode()

    ok1_message = Mock(spec=Message)
    ok1_message.key.return_value = b'{"keytype":"SCHEMA","subject1":"test","version":1,"magic":1}'
    ok1_message.error.return_value = None
    ok1_message.value.return_value = schema_str
    ok1_message.offset.return_value = 1
    invalid_key_message = Mock(spec=Message)
    invalid_key_message.key.return_value = "invalid-key"
    invalid_key_message.error.return_value = None
    invalid_key_message.value.return_value = schema_str
    invalid_key_message.offset.return_value = 2
    invalid_value_message = Mock(spec=Message)
    invalid_value_message.key.return_value = b'{"keytype":"SCHEMA","subject3":"test","version":1,"magic":1}'
    invalid_value_message.error.return_value = None
    invalid_value_message.value.return_value = "invalid-value"
    invalid_value_message.offset.return_value = 3

    consumer_mock.consume.side_effect = [ok1_message], [invalid_key_message], [invalid_value_message], []
    # Return tuple (beginning, end), end offset is the next upcoming record offset
    consumer_mock.get_watermark_offsets.return_value = (0, 4)

    offset_watcher = OffsetWatcher()
    schema_reader = KafkaSchemaReader(
        config=DEFAULTS,
        offset_watcher=offset_watcher,
        key_formatter=key_formatter_mock,
        master_coordinator=None,
        database=InMemoryDatabase(),
    )
    schema_reader.consumer = consumer_mock
    schema_reader.offset = 0
    assert schema_reader.max_messages_to_process == MAX_MESSAGES_TO_CONSUME_ON_STARTUP

    schema_reader.handle_messages()
    assert schema_reader.offset == 1
    assert schema_reader.ready is False
    schema_reader.handle_messages()
    assert schema_reader.offset == 2
    assert schema_reader.ready is False
    schema_reader.handle_messages()
    assert schema_reader.offset == 3
    assert schema_reader.ready is False
    schema_reader.handle_messages()  # call last time to call _is_ready()
    assert schema_reader.offset == 3
    assert schema_reader.ready is True
    assert schema_reader.max_messages_to_process == MAX_MESSAGES_TO_CONSUME_AFTER_STARTUP


def test_soft_deleted_schema_storing() -> None:
    """This tests a case when _schemas has been compacted and only
    the soft deleted version of the schema is present.
    """
    key_formatter_mock = Mock(spec=KeyFormatter)
    consumer_mock = Mock(spec=KafkaConsumer)
    soft_deleted_schema_record = Mock(spec=confluent_kafka.Message)
    soft_deleted_schema_record.error.return_value = None
    soft_deleted_schema_record.key.return_value = json.dumps(
        {
            "keytype": "SCHEMA",
            "subject": "soft-delete-test",
            "version": 1,
            "magic": 0,
        }
    )
    soft_deleted_schema_record.value.return_value = json.dumps(
        {
            "deleted": True,
            "id": 1,
            "schema": '"int"',
            "subject": "test-soft-delete-test",
            "version": 1,
        }
    )

    consumer_mock.consume.return_value = [soft_deleted_schema_record]
    # Return tuple (beginning, end), end offset is the next upcoming record offset
    consumer_mock.get_watermark_offsets.return_value = (0, 1)

    offset_watcher = OffsetWatcher()
    schema_reader = KafkaSchemaReader(
        config=DEFAULTS,
        offset_watcher=offset_watcher,
        key_formatter=key_formatter_mock,
        master_coordinator=None,
        database=InMemoryDatabase(),
    )
    schema_reader.consumer = consumer_mock
    schema_reader.offset = 0

    schema_reader.handle_messages()

    soft_deleted_stored_schema = schema_reader.database.find_schema(schema_id=SchemaId(1))
    assert soft_deleted_stored_schema is not None


def test_handle_msg_delete_subject_logs(caplog: LogCaptureFixture) -> None:
    database_mock = Mock(spec=InMemoryDatabase)
    database_mock.find_subject.return_value = True
    database_mock.find_subject_schemas.return_value = {
        Version(1): "SchemaVersion"
    }  # `SchemaVersion` is an actual object, simplified for test
    schema_reader = KafkaSchemaReader(
        config=DEFAULTS,
        offset_watcher=OffsetWatcher(),
        key_formatter=KeyFormatter(),
        master_coordinator=None,
        database=database_mock,
    )

    with caplog.at_level(logging.WARNING, logger="karapace.schema_reader"):
        schema_reader._handle_msg_schema_hard_delete(  # pylint: disable=protected-access
            key={"subject": "test-subject", "version": 2}
        )
        for log in caplog.records:
            assert log.name == "karapace.schema_reader"
            assert log.levelname == "WARNING"
            assert log.message == "Hard delete: version: Version(2) for subject: 'test-subject' did not exist, should have"
