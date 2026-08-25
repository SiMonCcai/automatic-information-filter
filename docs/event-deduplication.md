# Event-level deduplication / 事件级去重

[中文](#中文) · [English](#english)

## 中文

普通去重解决“同一条内容是否处理过”，事件级去重解决“不同来源是否在报道同一件事”。两者应当分开：

- **条目去重**：按来源 ID、URL 或内容指纹跳过完全重复的输入。
- **事件去重**：把标题不同、但语义上属于同一事件的条目归入一个事件簇。

### 推荐流程

```text
新条目
  → 条目指纹去重
  → 标题向量
  → 在最近事件窗口中做余弦匹配
  ├─ 未命中：创建新事件和稳定 event_id
  └─ 命中：作为新成员加入已有事件，不创建第二条展示记录
```

第一版只需要标题向量，不必读取全文，也不必加入实体抽取、编辑距离或大模型复核。向量服务应当可替换；阈值和时间窗口必须按自己的数据标定。一个偏保守的起点是最近 7 天、余弦相似度 `0.96`。

向量生成失败、标题为空、向量维度错误或包含非有限数值时，建议 **fail open**：把该条目视为独立事件，避免误删资讯。

### 一个事件只对应一个稳定输出

事件簇应保存一个稳定的 canonical ID。新成员命中后：

1. 持久化原始条目、标题、来源、链接、相似度和加入时间；
2. 复用事件原来的输出 ID，不向下游创建第二条记录；
3. 更新事件成员数和全部来源链接；
4. 使用幂等 operation ID 更新输出端，失败后可以安全重试。

外部展示只是事件的视图，事件、成员、评分和代表替换历史应保存在自己的状态库中。不要依赖解析输出页面恢复状态。

### 先评分，再决定是否做昂贵处理

如果后续还有评分、摘要、分类或其他昂贵处理，可以使用两阶段策略：

1. 所有事件成员只执行低成本评分；
2. 当前没有代表时，由首个事件成员建立初始代表；
3. 后来的候选只有在总分超过当前代表的配置门槛后，才执行摘要、分类等昂贵任务；
4. 低于门槛的候选进入终态，不要永久留在 pending；
5. 候选的全部处理和外部更新成功后，再原子提交代表替换。

这能保留所有来源，同时避免对明显较差的重复内容重复消耗模型和输出资源。

### 需要持久化的最小状态

```text
events
  event_id, canonical_output_id, current_representative_id,
  representative_score, member_count, revision

event_members
  event_id, item_id, title, source, url, similarity,
  score_status, score, candidate_status, created_at

event_replacements
  event_id, old_representative_id, new_representative_id,
  operation_id, created_at
```

还应持久化展示 revision、任务 claim 和重试状态。数据库更新使用事务或 compare-and-swap，防止并发 worker 重复替换代表。

### 与本框架结合

`Automatic Information Filter` 的核心保持轻量，不绑定任何 embedding 模型或向量数据库。事件去重可以作为自定义有状态 Processor，或作为 Source 与 Sink 之间的独立服务：

```python
class EventClusterProcessor(Processor):
    def process(self, item: InformationItem) -> InformationItem | None:
        match = self.event_store.match_recent(
            vector=self.embedder.embed(item.title),
            window_days=7,
            threshold=0.96,
        )
        event = self.event_store.attach_or_create(item, match)
        item.annotations["event_id"] = event.id
        item.annotations["canonical_id"] = event.canonical_id
        item.annotations["event_member_count"] = event.member_count
        return item
```

生产实现还需要事务、并发 claim、幂等输出和崩溃恢复；上面的代码只说明适配器边界。

---

## English

Item deduplication answers “have I processed this exact item?” Event-level deduplication answers “are different sources reporting the same event?” Keep them separate:

- **Item deduplication:** skip exact repeats by source ID, URL, or content fingerprint.
- **Event deduplication:** cluster differently worded items that describe the same event.

### Recommended flow

```text
new item
  → item fingerprint deduplication
  → title embedding
  → cosine match against a recent event window
  ├─ no match: create a new event with a stable event_id
  └─ match: attach as a member and reuse the existing output record
```

A first version can use title embeddings only. It does not need full-text retrieval, entity extraction, edit distance, or an LLM judge. Keep the embedding provider replaceable and calibrate the threshold and time window on your own corpus. A conservative starting point is a 7-day window with cosine similarity `0.96`.

If embedding fails, the title is empty, the vector has the wrong dimension, or values are non-finite, **fail open** and create an independent event. Missing an occasional merge is safer than silently dropping unrelated information.

### One stable output per event

Each cluster owns a stable canonical ID. When a new member matches:

1. persist the raw item, title, source, URL, similarity, and attachment time;
2. reuse the existing output ID instead of creating another downstream record;
3. refresh the member count and all source links;
4. use an idempotent operation ID so an interrupted output update can be retried safely.

The downstream page or record is a projection. Keep authoritative event membership, scores, and representative replacement history in your own state store rather than reconstructing them by parsing the sink.

### Score first, enrich selectively

When the pipeline also performs scoring, summarization, classification, or other expensive work, use two phases:

1. run low-cost scoring for every member;
2. let the first event member establish the initial representative;
3. run expensive enrichment only when a later candidate beats the representative by a configured score margin;
4. move losing candidates to a terminal state instead of leaving them pending forever;
5. commit the representative swap atomically only after enrichment and the external update both succeed.

This preserves every source while avoiding repeated expensive work for weaker duplicate reports.

### Minimal durable state

```text
events
  event_id, canonical_output_id, current_representative_id,
  representative_score, member_count, revision

event_members
  event_id, item_id, title, source, url, similarity,
  score_status, score, candidate_status, created_at

event_replacements
  event_id, old_representative_id, new_representative_id,
  operation_id, created_at
```

Persist presentation revisions, queue claims, and retry state as well. Use transactions or compare-and-swap updates to prevent concurrent workers from replacing the representative twice.

### Using it with this framework

The `Automatic Information Filter` core remains lightweight and does not require a specific embedding model or vector database. Implement event clustering as a custom stateful Processor or as a service between a Source and a Sink:

```python
class EventClusterProcessor(Processor):
    def process(self, item: InformationItem) -> InformationItem | None:
        match = self.event_store.match_recent(
            vector=self.embedder.embed(item.title),
            window_days=7,
            threshold=0.96,
        )
        event = self.event_store.attach_or_create(item, match)
        item.annotations["event_id"] = event.id
        item.annotations["canonical_id"] = event.canonical_id
        item.annotations["event_member_count"] = event.member_count
        return item
```

A production implementation still needs transactions, concurrent claims, idempotent sinks, and crash recovery. The snippet only illustrates the adapter boundary.
