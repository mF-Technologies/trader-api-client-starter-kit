from trader_api_examples.position_sync import SsePositionUpdateParser


def test_sse_parser_extracts_position_references_and_last_event_id() -> None:
    parser = SsePositionUpdateParser()

    assert parser.feed_line("id: 42") is None
    assert parser.feed_line("event: PositionUpdate") is None
    assert parser.feed_line('data: {"deleted":[5178],"modified":[{"orderNo":5179}]}') is None
    notification = parser.feed_line("")

    assert notification is not None
    assert notification.event_id == "42"
    assert notification.affected_refs == frozenset({"5178", "5179"})
    assert parser.last_event_id == "42"


def test_sse_parser_accepts_nested_data_and_unknown_position_refs() -> None:
    parser = SsePositionUpdateParser()

    parser.feed_line('data: {"data":{"added":[{"dealRef":5180}]}}')
    notification = parser.finish()

    assert notification is not None
    assert notification.affected_refs == frozenset({"5180"})
