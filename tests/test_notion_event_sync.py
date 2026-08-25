from pipeline.notion_sync import NotionSync


class FakePages:
    def __init__(self):
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return {"id": kwargs["page_id"]}


class FakeBlocksChildren:
    def __init__(self, owner):
        self.owner = owner
        self.appends = []

    def append(self, **kwargs):
        self.appends.append(kwargs)
        created = []
        for child in kwargs["children"]:
            stored = {**child, "id": f"block-{self.owner.next_id}"}
            self.owner.next_id += 1
            self.owner.stored.append(stored)
            created.append(stored)
        return {"results": created}

    def list(self, **kwargs):
        start = int(kwargs.get("start_cursor") or 0)
        page_size = int(kwargs.get("page_size") or 100)
        results = list(self.owner.stored[start:start + page_size])
        next_offset = start + len(results)
        has_more = next_offset < len(self.owner.stored)
        return {
            "results": results,
            "has_more": has_more,
            "next_cursor": str(next_offset) if has_more else None,
        }


class FakeBlocks:
    def __init__(self):
        self.stored = []
        self.next_id = 1
        self.children = FakeBlocksChildren(self)

    def delete(self, block_id=None, **kwargs):
        block_id = block_id or kwargs.get("block_id")
        self.stored = [block for block in self.stored if block["id"] != block_id]
        return {"id": block_id, "archived": True}


class FakeClient:
    def __init__(self):
        self.pages = FakePages()
        self.blocks = FakeBlocks()


def test_event_page_create_retry_recovers_existing_page_by_canonical_url():
    class RecoverPages(FakePages):
        def create(self, **kwargs):
            raise AssertionError("must not create a duplicate page after URL recovery")

    class RecoverDataSources:
        def query(self, **kwargs):
            assert kwargs["filter"] == {
                "property": "网址", "url": {"equals": "https://existing.test"}
            }
            return {"results": [{"id": "existing-page"}]}

    client = FakeClient()
    client.pages = RecoverPages()
    client.data_sources = RecoverDataSources()
    sync = NotionSync("unused", "database", client=client)
    sync._schema_cache = {}
    sync._data_source_id = "data-source"
    article = type(
        "Article", (), {"id": 1, "title": "Existing", "url": "https://existing.test"}
    )()

    synced, errors, page_map = sync.sync_articles(
        [article], recover_existing_article_ids={1}
    )

    assert synced == 1
    assert errors == []
    assert page_map == {1: "existing-page"}


def test_visible_event_title_keeps_single_member_title_and_prefixes_multi_member_event():
    assert NotionSync.visible_event_title("Original", 1) == "Original"
    assert NotionSync.visible_event_title("Original", 3) == "【事件·3】Original"


def test_render_event_info_links_every_member_and_marks_winner():
    members = [
        {"id": 1, "title": "First", "url": "https://one.test", "source": "Feed A", "score": 4},
        {"id": 2, "title": "Second", "url": "https://two.test", "source": "Feed B", "score": None},
    ]

    rendered = NotionSync.render_event_info(members, winner_id=2)

    assert rendered["overflow"] is False
    assert rendered["rich_text"][0]["text"]["content"] == "事件成员（2）\n"
    linked = rendered["rich_text"][1:]
    assert [item["text"]["link"]["url"] for item in linked] == ["https://one.test", "https://two.test"]
    assert "First｜Feed A｜4" in linked[0]["text"]["content"]
    assert "⭐ Second｜Feed B｜待评分" in linked[1]["text"]["content"]


def test_render_event_info_accepts_storage_rows_and_formats_average_score():
    rendered = NotionSync.render_event_info(
        [{
            "article_id": 7,
            "title": "Stored",
            "url": "https://stored.test",
            "source": "Feed",
            "score_total": 25,
            "score_count": 6,
        }],
        winner_id=7,
    )

    assert "⭐ Stored｜Feed｜4.17" in rendered["rich_text"][1]["text"]["content"]


def test_exceptional_event_rendering_reports_overflow_and_keeps_every_link_for_fallback():
    members = [
        {"id": index, "title": f"Member {index}", "url": f"https://example.test/{index}", "source": "Feed"}
        for index in range(105)
    ]

    rendered = NotionSync.render_event_info(members)

    assert rendered["overflow"] is True
    assert len(rendered["rich_text"]) == 100
    assert len(rendered["fallback_rich_text"]) == 105
    assert [item["text"]["link"]["url"] for item in rendered["fallback_rich_text"]] == [
        member["url"] for member in members
    ]
    assert len(rendered["fallback_blocks"]) == 105
    assert [
        block["paragraph"]["rich_text"][0]["text"]["link"]["url"]
        for block in rendered["fallback_blocks"]
    ] == [member["url"] for member in members]


def test_oversized_member_line_is_deterministically_chunked_for_notion_limits():
    member = {
        "id": 1,
        "title": "T" * 4500,
        "url": "https://long.test",
        "source": "Feed",
        "score": 5,
    }

    rendered = NotionSync.render_event_info([member])

    linked = rendered["rich_text"][1:]
    assert len(linked) == 3
    assert all(len(item["text"]["content"]) <= 2000 for item in linked)
    assert all(item["text"]["link"]["url"] == member["url"] for item in linked)
    assert "".join(item["text"]["content"] for item in linked) == f"{'T' * 4500}｜Feed｜5\n"


def test_update_event_page_atomically_updates_title_info_and_explicitly_resets_reading():
    client = FakeClient()
    sync = NotionSync("unused", "database", client=client)
    sync._schema_cache = {
        sync.FIELD_TITLE: {"type": "title"},
        sync.FIELD_EVENT_INFO: {"type": "rich_text"},
        sync.FIELD_READ_STATUS: {"type": "checkbox"},
    }
    members = [{"id": 1, "title": "One", "url": "https://one.test", "source": "Feed"}]

    result = sync.update_event_page("page-1", "One", members, winner_id=1, reset_reading=True)

    assert result["success"] is True
    assert result["fallback_blocks"] == []
    properties = client.pages.updates[0]["properties"]
    assert properties[sync.FIELD_TITLE]["title"][0]["text"]["content"] == "One"
    assert properties[sync.FIELD_EVENT_INFO]["rich_text"][1]["text"]["link"]["url"] == "https://one.test"
    assert properties[sync.FIELD_READ_STATUS] == {"checkbox": False}


def test_update_event_page_does_not_reset_reading_for_later_scoring_update():
    client = FakeClient()
    sync = NotionSync("unused", "database", client=client)
    sync._schema_cache = {
        sync.FIELD_TITLE: {"type": "title"},
        sync.FIELD_EVENT_INFO: {"type": "rich_text"},
    }

    result = sync.update_event_page("page-1", "One", [], reset_reading=False)

    assert result["success"] is True
    assert sync.FIELD_READ_STATUS not in client.pages.updates[0]["properties"]


def test_update_event_page_prefixes_multi_member_title_and_writes_overflow_fallback():
    client = FakeClient()
    sync = NotionSync("unused", "database", client=client)
    sync._schema_cache = {
        sync.FIELD_TITLE: {"type": "title"},
        sync.FIELD_EVENT_INFO: {"type": "rich_text"},
        sync.FIELD_READ_STATUS: {"type": "checkbox"},
    }
    members = [
        {"article_id": index, "title": f"Member {index}", "url": f"https://e.test/{index}"}
        for index in range(105)
    ]

    result = sync.update_event_page("page-1", "Winner", members, winner_id=0, reset_reading=True)

    assert result["success"] is True
    title = client.pages.updates[0]["properties"][sync.FIELD_TITLE]["title"][0]["text"]["content"]
    assert title == "【事件·105】Winner"
    appended = [
        block["paragraph"]["rich_text"][0]["text"]["link"]["url"]
        for call in client.blocks.children.appends
        for block in call["children"]
    ]
    assert appended == [member["url"] for member in members]
    assert all(len(call["children"]) <= 100 for call in client.blocks.children.appends)


def test_overflow_fallback_blocks_are_replaced_idempotently_on_retry():
    client = FakeClient()
    sync = NotionSync("unused", "database", client=client)
    sync._schema_cache = {
        sync.FIELD_TITLE: {"type": "title"},
        sync.FIELD_EVENT_INFO: {"type": "rich_text"},
    }
    members = [
        {"id": index, "title": f"Member {index}", "url": f"https://example.test/{index}"}
        for index in range(105)
    ]

    assert sync.update_event_page("page", "Winner", members, winner_id=0)["success"] is True
    assert sync.update_event_page("page", "Winner", members, winner_id=0)["success"] is True

    assert len(client.blocks.stored) == 105
    assert all(len(call["children"]) <= 100 for call in client.blocks.children.appends)


def test_update_event_page_schema_mismatch_fails_without_partial_update():
    client = FakeClient()
    sync = NotionSync("unused", "database", client=client)
    sync._schema_cache = {
        sync.FIELD_TITLE: {"type": "title"},
        sync.FIELD_EVENT_INFO: {"type": "url"},
        sync.FIELD_READ_STATUS: {"type": "checkbox"},
    }

    result = sync.update_event_page("page-1", "One", [], reset_reading=True)

    assert result == {
        "success": False,
        "error": "Notion schema mismatch: 事件信息 expected rich_text, got url",
        "invalid_fields": ["事件信息"],
    }
    assert client.pages.updates == []


def test_representative_replacement_payload_contains_article_scores_meta_and_event_info():
    sync = NotionSync("unused", "database", client=FakeClient())
    representative = {
        "title": "Winner",
        "url": "https://winner.test",
        "author": "Writer",
        "content_text": "Body",
    }
    members = [
        {"id": 7, "title": "Winner", "url": "https://winner.test", "source": "Feed", "score": 5},
        {"id": 8, "title": "Other", "url": "https://other.test", "source": "Feed", "score": 3},
    ]
    ai_values = {
        "实用性": "5", "客观性": "4", "是否营销内容": "3",
        "有趣性": "4", "独特性": "5", "信息密度": "4",
        "分类": "AI", "摘要": "Summary", "金句": "Quote",
    }

    payload = sync.build_representative_replacement_payload(
        representative, members, ai_values, winner_id=7
    )

    assert payload[sync.FIELD_TITLE]["title"][0]["text"]["content"] == "【事件·2】Winner"
    assert payload[sync.FIELD_URL] == {"url": "https://winner.test"}
    assert payload[sync.FIELD_AUTHOR]["rich_text"][0]["text"]["content"] == "Writer"
    assert payload[sync.FIELD_CONTENT]["rich_text"][0]["text"]["content"] == "Body"
    assert payload["实用性"]["rich_text"][0]["text"]["content"] == "5"
    assert payload["分类"]["rich_text"][0]["text"]["content"] == "AI"
    assert payload["摘要"]["rich_text"][0]["text"]["content"] == "Summary"
    assert payload["金句"]["rich_text"][0]["text"]["content"] == "Quote"
    assert payload["客观性"]["rich_text"][0]["text"]["content"] == "4"
    assert payload[sync.FIELD_EVENT_INFO]["rich_text"][1]["text"]["link"]["url"] == "https://winner.test"



def test_apply_representative_requires_complete_ai_snapshot_and_uses_one_update():
    client = FakeClient()
    sync = NotionSync("unused", "database", client=client)
    required = [
        sync.FIELD_TITLE, sync.FIELD_URL, sync.FIELD_AUTHOR, sync.FIELD_CONTENT,
        sync.FIELD_EVENT_INFO, "实用性", "客观性", "是否营销内容",
        "有趣性", "独特性", "信息密度", "分类", "摘要", "金句",
    ]
    sync._schema_cache = {
        name: {"type": "title" if name == sync.FIELD_TITLE else "url" if name == sync.FIELD_URL else "rich_text"}
        for name in required
    }
    article = {"title": "Winner", "url": "https://winner", "author": "A", "content_text": "Body"}
    members = [{"id": 1, "title": "Winner", "url": "https://winner", "source": "feed"}]

    failed = sync.apply_representative("page", article, members, {"实用性": "5"}, winner_id=1)
    assert failed["success"] is False
    assert client.pages.updates == []

    values = {name: "4" for name in ["实用性", "客观性", "是否营销内容", "有趣性", "独特性", "信息密度"]}
    values.update({"分类": "AI", "摘要": "Summary", "金句": "Quote"})
    result = sync.apply_representative("page", article, members, values, winner_id=1)
    assert result["success"] is True
    assert len(client.pages.updates) == 1
