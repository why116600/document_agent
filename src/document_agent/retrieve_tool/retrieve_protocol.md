# 检索协议（Retrieval Protocol）

给**其它智能体**调用的一组只读 HTTP 接口：查知识图谱状态、看 Schema、关键词/语义检索、
图遍历、路径查找、读取节点正文、实体识别与消歧。

> 实现位置：`graph_rag/api/routers/retrieval.py`（路由）+ `graph_rag/retrieval.py`（编排）
> + `graph_rag/neo4j_store.py` 的读接口。服务启动后即可调用，接口定义以本文为准，
> 也可以在 `/docs`（Swagger）或 `/openapi.json` 里看到机器可读版本。

---

## 1. 总览

| # | 能力 | 方法 | 路径 | 典型用途 |
| --- | --- | --- | --- | --- |
| 1 | 工作状态 | `GET` | `/retrieval/status` | 调用前自检：图可读？Neo4j 通？LLM 正常？ |
| 2 | Schema 元信息 | `GET` | `/retrieval/schema` | 先看"图里有什么"，再决定检索方式 |
| 3 | 关键词检索 | `GET` / `POST` | `/retrieval/keywords` | 已知关键词，找实体/分块/文档 |
| 4 | 语义检索 | `POST` | `/retrieval/semantic` | 不确定用词，按语义找实体与关系 |
| 5 | 图遍历 | `POST` | `/retrieval/traverse` | 从某节点出发取 k 跳子图 |
| 6 | 路径查找 | `POST` | `/retrieval/paths` | 两个实体之间怎么连起来的 |
| 7 | 读节点正文 | `GET` / `POST` | `/retrieval/nodes/{id}`、`/retrieval/nodes/bodies` | 拿分块原文/文档全文/实体描述 |
| 8 | 实体识别与消歧 | `POST` | `/retrieval/entities/resolve` | 把文本里的"人/公司"对齐到图谱节点 |

所有接口都在**后端 agent 运行期间**可用；它们只读，不会修改图谱。

---

## 2. 通用约定

### 2.1 地址与鉴权

* 默认地址：`http://127.0.0.1:8000`（由 `api_server.py` / 启动脚本决定）。
* 如果配置了 `api.auth_token`（或环境变量 `API_TOKEN`），下面两种请求头任选其一：

  ```http
  X-API-Token: <token>
  Authorization: Bearer <token>
  ```

  未配置 token 时不需要鉴权（本机默认）。`/health`、`/ready`、`/docs` 始终公开；
  `/retrieval/*` **全部**需要 token（如果配置了）。

### 2.2 请求与响应

* 请求体一律 JSON（`Content-Type: application/json`）；GET 接口用 query 参数（列表参数可重复传，
  如 `?kinds=entity&kinds=chunk`）。
* 响应一律 UTF-8 JSON；字段名 `snake_case`。
* 时间：ISO-8601 带时区（如 `2026-10-25T13:00:00+00:00`）。
* 分数：`score` 取值 `0~1`（越小越不相关），仅用于**同一响应内排序**，不同接口之间不可直接比较。
* 路径：响应里的文档路径是**相对服务工作目录**的写法（`/` 分隔），图谱内部仍用绝对路径做身份判断。
* ID：`Document`/`Chunk`/`Entity` 都有 `id`；跨类型唯一，可直接作为 `node_id` 使用。

### 2.3 错误码

| HTTP | 含义 | 典型场景 |
| --- | --- | --- |
| 400 | 参数不合法 / 路径类错误 | 起止节点都为空、Cypher 安全问题（本组接口不暴露 Cypher） |
| 401 | 鉴权失败 | 配置了 token 但请求没带/带错 |
| 404 | 节点/端点不存在 | `traverse`/`paths` 的起点或终点找不到，`nodes/{id}` 不存在 |
| 422 | 请求体校验失败 | 字段缺失、`depth` 超出 1~4、`max_chars` 超出 200~40000 |
| 502 | 上游大模型失败 | 仅发生在需要 LLM 的支路（如实体识别的 `use_llm=true`），且不会中断兜底 |
| 503 | Neo4j 不可用 | 数据库没起/密码不对 |

错误响应体：

```json
{ "detail": "起点不存在：查无此人（可用 /retrieval/keywords 或 /retrieval/semantic 先找节点）" }
```

### 2.4 规模上限（服务端强制）

| 项 | 默认 | 上限 | 说明 |
| --- | --- | --- | --- |
| `limit`（关键词/语义） | 10 | 50 | 每个通道/每类结果条数 |
| `depth`（遍历） | 2 | 4 | 跳数越大结果爆炸越快，建议先 1~2 |
| `node_limit`（遍历） | 100 | 500 | 子图节点数上限 |
| `edge_limit`（遍历） | 300 | 2000 | 子图边数上限 |
| `max_depth`（路径） | 4 | 8 | 超过 8 跳的"关系"通常已没有解释力 |
| `limit`（路径条数） | 3 | 10 | 只返回最短路径 |
| `max_chars`（正文） | 4000 | 40000 | 超出会截断并置 `truncated=true` |
| `ids`（批量正文） | — | 50 | 单次最多 50 个节点 |

超出上限时接口返回 422，不会静默截断参数；**结果**规模触顶时用响应里的 `truncated=true` 表示。

---

## 3. 工作状态 `GET /retrieval/status`

调用前自检：**图是否可读、Neo4j 是否连通、大模型是否正常**。三项全绿才建议继续检索。

### 参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `probe_llm` | bool | `true` | 是否真的调一次大模型做连通性探测；`false` 时只报告"配置是否齐全" |
| `refresh` | bool | `false` | 忽略 30 秒探测缓存，强制重新探测 |

> `probe_llm=true` 会消耗一次很小的模型调用；高频轮询时建议传 `false`，或复用 `refresh=false` 的缓存。

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `ok` | 三个组件的与：任一异常即 `false` |
| `version` | 服务版本 |
| `checked_at` | 检查时间 |
| `components.graph` | `ok` / 文档/分块/实体/关系/提及计数 / `empty` / `error` |
| `components.neo4j` | `ok` / `uri` / `database` / `latency_ms` / `server_version` / `edition` / `error` |
| `components.llm` | `ok` / `configured` / `probed` / `model` / `latency_ms` / `reply` / `error` |

### 示例

```bash
curl -s 'http://127.0.0.1:8000/retrieval/status?probe_llm=false'
```

```json
{
  "ok": true,
  "version": "0.1.0",
  "checked_at": "2026-10-25T13:00:00+00:00",
  "components": {
    "graph": {"ok": true, "documents": 12, "chunks": 240, "entities": 380,
              "relations": 210, "mentions": 900, "empty": false, "error": ""},
    "neo4j": {"ok": true, "uri": "bolt://localhost:7687", "database": "neo4j",
              "latency_ms": 1.8, "server_version": "5.26.0", "edition": "community", "error": ""},
    "llm": {"ok": true, "configured": true, "probed": false,
            "model": "starlight/DeepSeek-V4", "latency_ms": null, "reply": "", "error": ""}
  }
}
```

> `probe_llm=false` 时 `llm.ok=true` 只代表"密钥/地址/模型名都配了"，不代表模型真能回话；
> 要确认可用性请用 `probe_llm=true`。

---

## 4. Schema 元信息 `GET /retrieval/schema`

返回图谱里"有什么"：标签、关系类型、属性、索引、约束，以及静态图谱模型与检索能力。

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `labels` | `[{label, count}]`，节点标签及数量 |
| `relationship_types` | `[{type, count}]`，关系类型及数量 |
| `property_keys` | 全图属性键列表 |
| `indexes` / `constraints` | 索引与约束（名称、类型、作用对象、属性、状态） |
| `node_properties` / `relationship_properties` | 属性类型信息（Neo4j 5 的过程；老版本可能为空） |
| `entity_types` | 配置里的实体类型（`ingest.entity_types`） |
| `relation_hint` | 配置里的关系提示（`ingest.relation_hint`） |
| `graph_model` | 静态模型：Document/Chunk/Entity 的属性、四类关系及其语义 |
| `search` | `fulltext_indexes` / `vector_indexes` / `embedding_enabled` / `embedding_model` |

### 示例

```bash
curl -s http://127.0.0.1:8000/retrieval/schema | head -c 800
```

```json
{
  "labels": [{"label": "Chunk", "count": 240}, {"label": "Document", "count": 12}, {"label": "Entity", "count": 380}],
  "relationship_types": [{"type": "HAS_CHUNK", "count": 240}, {"type": "MENTIONS", "count": 900},
                          {"type": "NEXT", "count": 228}, {"type": "RELATES", "count": 210}],
  "property_keys": ["alias_text", "aliases", "char_count", "chunk_count", "description", "..."],
  "indexes": [{"name": "entity_fulltext", "type": "FULLTEXT", "labels_or_types": ["Entity"],
               "properties": ["name", "alias_text"], "state": "ONLINE"}],
  "entity_types": ["人物", "组织", "地点", "产品", "事件"],
  "relation_hint": "关系类型用中文动词短语，例如：创立、任职于、投资、位于",
  "graph_model": {
    "nodes": {"Document": {"properties": ["id", "path", "title", "..."]}, "Chunk": {}, "Entity": {}},
    "relationships": {
      "HAS_CHUNK": {"pattern": "(Document)-[:HAS_CHUNK]->(Chunk)", "description": "文档包含分块"},
      "MENTIONS": {"pattern": "(Chunk)-[:MENTIONS {count}]->(Entity)", "description": "分块提到了实体"},
      "RELATES": {"pattern": "(Entity)-[:RELATES {type, description, weight, evidence}]->(Entity)"}
    }
  },
  "search": {"fulltext_indexes": ["entity_fulltext", "chunk_fulltext"],
             "vector_indexes": ["chunk_vector"], "embedding_enabled": true,
             "embedding_model": "text-embedding-3-small"}
}
```

---

## 5. 关键词检索

`GET /retrieval/keywords`（便于 curl/浏览器）与 `POST /retrieval/keywords` 参数等价。

### 请求参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `query` / `q` | str | 必填 | 查询串；按空格与中英文标点切词，切出的词分别做匹配 |
| `kinds` | list[str] | 三路都查 | `entity` / `chunk` / `document` |
| `limit` | int | 10 | **每个通道**的返回上限（1~50） |
| `document_ids` | list[str] | 无 | 只在这些文档的分块里检索（可选） |

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `terms` | 实际使用的关键词（去重、小写） |
| `entities[]` | `id` / `name` / `type` / `description` / `mentions` / `score` |
| `chunks[]` | `id` / `text`（截断到 600 字）/ `snippet`（命中位置摘要）/ `document_id` / `document_title` / `chunk_index` / `score` |
| `documents[]` | `id` / `title` / `path` / `chunk_count` / `score` |
| `elapsed_ms` | 服务端耗时 |

> 实体/分块优先走 Neo4j 全文索引（`entity_fulltext` / `chunk_fulltext`），索引缺失或中文分词
> 不理想时自动退回 `CONTAINS` 包含匹配，因此**低版本或索引未建**也能用，只是分数含义不同。

### 示例

```bash
# GET
curl -s 'http://127.0.0.1:8000/retrieval/keywords?q=张三%20任职&limit=5'

# POST：只要实体和分块
curl -s -X POST http://127.0.0.1:8000/retrieval/keywords \
  -H 'Content-Type: application/json' \
  -d '{"query": "张三 任职", "kinds": ["entity", "chunk"], "limit": 5}'
```

```json
{
  "query": "张三 任职",
  "terms": ["张三", "任职"],
  "entities": [{"id": "ent-8f2a", "name": "张三", "type": "人物",
                "description": "星河科技创始人", "mentions": 6, "score": 4.2}],
  "chunks": [{"id": "chunk-1a2b", "text": "张三于 2015 年创立了星河科技……",
              "snippet": "…张三于 2015 年创立了星河科技…", "document_id": "doc-9c",
              "document_title": "公司简介", "chunk_index": 0, "score": 3.1}],
  "documents": [{"id": "doc-9c", "title": "公司简介", "path": "examples/demo_company.md",
                 "chunk_count": 8, "score": 1.0}],
  "elapsed_ms": 12.4
}
```

---

## 6. 语义检索 `POST /retrieval/semantic`

按语义找**实体**与**关系**，不要求关键词字面重合。

### 请求参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `query` | str | 必填 | 自然语言即可，例如"谁创建了这家公司" |
| `entity_limit` | int | 10 | 实体结果上限（1~50） |
| `relation_limit` | int | 10 | 关系结果上限（1~50） |
| `min_score` | float | 0.0 | 低于该分数的结果不返回（0~1） |
| `entity_types` | list[str] | 无 | 只在这些实体类型里检索 |
| `refresh` | bool | false | 忽略 20 秒实体目录缓存，重新读库 |

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `mode` | `embedding`（用了向量精排）或 `lexical`（字面相似度兜底） |
| `embedding_enabled` | 服务端是否开启了向量化（`embedding.enabled`） |
| `entities[]` | `id` / `name` / `type` / `description` / `aliases` / `mentions` / `score` / `match` |
| `relations[]` | `id`（`source_id\|type\|target_id`）/ `source_id` / `source_name` / `target_id` / `target_name` / `type` / `description` / `weight` / `score` / `match` |
| `elapsed_ms` | 服务端耗时 |

> **降级行为**：开启向量时，先用字面相似度粗排 80 个候选，再用向量余弦精排
> （`score = 0.35 × 字面 + 0.65 × 余弦`）；向量不可用（未配置/调用失败）时自动退回
> `mode=lexical`，接口不报错。`match` 取 `embedding` / `lexical` 表示该条分数来源。

### 示例

```bash
curl -s -X POST http://127.0.0.1:8000/retrieval/semantic \
  -H 'Content-Type: application/json' \
  -d '{"query": "谁创办了这家公司", "entity_limit": 5, "relation_limit": 5, "min_score": 0.2}'
```

```json
{
  "query": "谁创办了这家公司",
  "mode": "lexical",
  "embedding_enabled": false,
  "entities": [{"id": "ent-8f2a", "name": "张三", "type": "人物",
                "description": "星河科技创始人", "aliases": ["老张"], "mentions": 6,
                "score": 0.71, "match": "lexical"}],
  "relations": [{"id": "ent-8f2a|创立|ent-3c1d", "source_id": "ent-8f2a",
                 "source_name": "张三", "target_id": "ent-3c1d", "target_name": "星河科技",
                 "type": "创立", "description": "张三创立了星河科技", "weight": 0.9,
                 "score": 0.93, "match": "lexical"}],
  "elapsed_ms": 8.1
}
```

---

## 7. 图遍历 `POST /retrieval/traverse`

从起点做**有界 BFS**，返回子图（节点 + 边）。适合"把这个实体的关系网拉出来看看"。

### 请求参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `start` | str | 必填 | 起点：节点 ID，或实体名/别名（`auto` 先按 ID 再按名称解析） |
| `start_kind` | str | `auto` | `auto` / `entity` / `document` / `chunk` |
| `depth` | int | 2 | 跳数 1~4 |
| `relation_types` | list[str] | 无 | 只走这些关系类型（如 `["RELATES"]`） |
| `direction` | str | `both` | `out`（出边）/ `in`（入边）/ `both` |
| `node_limit` | int | 100 | 节点上限 1~500 |
| `edge_limit` | int | 300 | 边上限 1~2000 |

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `start` | 实际使用的起点节点 ID（`auto` 解析后） |
| `nodes[]` | `id` / `kind`（`document`/`chunk`/`entity`/`other`）/ `labels` / `name` / `type` / `properties` |
| `edges[]` | `id`（Neo4j 关系元素 ID）/ `type`（`HAS_CHUNK`/`NEXT`/`MENTIONS`/`RELATES`）/ `source` / `target` / `properties`（`count`、`type`、`description`、`weight`…） |
| `truncated` | 是否因 `node_limit`/`edge_limit` 被截断 |
| `elapsed_ms` | 服务端耗时 |

> 起点找不到返回 **404**；建议先用关键词/语义检索拿到 `id`，再传 `start`（比按名字解析更稳）。

### 示例

```bash
curl -s -X POST http://127.0.0.1:8000/retrieval/traverse \
  -H 'Content-Type: application/json' \
  -d '{"start": "张三", "depth": 2, "relation_types": ["RELATES", "MENTIONS"], "node_limit": 50}'
```

```json
{
  "start": "ent-8f2a",
  "depth": 2,
  "direction": "both",
  "nodes": [
    {"id": "ent-8f2a", "kind": "entity", "labels": ["Entity"], "name": "张三", "type": "人物",
     "properties": {"description": "星河科技创始人", "mention_count": 6, "aliases": ["老张"]}},
    {"id": "ent-3c1d", "kind": "entity", "labels": ["Entity"], "name": "星河科技", "type": "组织",
     "properties": {"description": "一家科技公司", "mention_count": 4, "aliases": []}},
    {"id": "chunk-1a2b", "kind": "chunk", "labels": ["Chunk"], "name": "chunk-1a2b", "type": "",
     "properties": {"index": 0, "document_id": "doc-9c", "document_title": "公司简介"}}
  ],
  "edges": [
    {"id": "4:abc:12", "type": "RELATES", "source": "ent-8f2a", "target": "ent-3c1d",
     "properties": {"type": "创立", "description": "张三创立了星河科技", "weight": 0.9}},
    {"id": "4:abc:31", "type": "MENTIONS", "source": "chunk-1a2b", "target": "ent-8f2a",
     "properties": {"count": 2}}
  ],
  "truncated": false,
  "elapsed_ms": 6.7
}
```

---

## 8. 路径查找 `POST /retrieval/paths`

查两个节点之间的**最短路径**（可限定关系类型/方向/最大跳数），回答"这两个人/公司是怎么连上的"。

### 请求参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `source` / `target` | str | 必填 | 端点：节点 ID 或实体名 |
| `max_depth` | int | 4 | 最大跳数 1~8 |
| `relation_types` | list[str] | 无 | 只允许这些关系类型 |
| `direction` | str | `both` | `out` / `in` / `both` |
| `limit` | int | 3 | 最多返回几条最短路径（1~10） |

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `found` | 是否找到路径（`source == target` 时视为未找到） |
| `source` / `target` | 解析后的端点 ID |
| `paths[]` | `length`（跳数）/ `nodes[]`（`id`/`kind`/`labels`/`name`/`type`）/ `edges[]`（`type`/`source`/`target`/`properties`） |
| `elapsed_ms` | 服务端耗时 |

> 实现用 `allShortestPaths`：只返回**最短**的若干条，不会返回更长的绕路。
> 端点不存在返回 **404**。

### 示例

```bash
curl -s -X POST http://127.0.0.1:8000/retrieval/paths \
  -H 'Content-Type: application/json' \
  -d '{"source": "张三", "target": "星河科技", "max_depth": 4}'
```

```json
{
  "found": true,
  "source": "ent-8f2a",
  "target": "ent-3c1d",
  "paths": [{
    "length": 1,
    "nodes": [
      {"id": "ent-8f2a", "kind": "entity", "labels": ["Entity"], "name": "张三", "type": "人物"},
      {"id": "ent-3c1d", "kind": "entity", "labels": ["Entity"], "name": "星河科技", "type": "组织"}
    ],
    "edges": [{"type": "RELATES", "source": "ent-8f2a", "target": "ent-3c1d",
               "properties": {"type": "创立", "description": "张三创立了星河科技", "weight": 0.9}}]
  }],
  "elapsed_ms": 5.2
}
```

---

## 9. 读取节点正文

### 9.1 单个节点 `GET /retrieval/nodes/{node_id}`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `max_chars` | int | 4000 | 正文字符上限 200~40000，超出截断并置 `truncated` |
| `include_related` | bool | true | 是否返回关联片段（实体→提及它的分块；文档→前 3 个分块） |

**不同节点的"正文"含义**：

| 节点类型 | `body.text` 内容 |
| --- | --- |
| `chunk` | 该分块的原文 |
| `document` | 按 `index` 顺序拼接的全部分块正文（`\n\n` 分隔） |
| `entity` | 实体描述（`description`） |
| `other` | 空字符串 |

响应字段：`node`（同遍历的节点摘要，`properties` 含 `text` 之外的元信息）、
`body`（`kind`/`text`/`char_count`/`truncated`）、`related[]`。
节点不存在返回 **404**。

```bash
curl -s 'http://127.0.0.1:8000/retrieval/nodes/chunk-1a2b?max_chars=2000'
```

```json
{
  "node": {"id": "chunk-1a2b", "kind": "chunk", "labels": ["Chunk"],
           "name": "chunk-1a2b", "type": "",
           "properties": {"index": 0, "document_id": "doc-9c", "document_title": "公司简介"}},
  "body": {"kind": "chunk", "text": "张三于 2015 年创立了星河科技……",
           "char_count": 42, "truncated": false},
  "related": []
}
```

### 9.2 批量读取 `POST /retrieval/nodes/bodies`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ids` | list[str] | 必填 | 1~50 个节点 ID |
| `max_chars` | int | 4000 | 每个节点的正文上限 |

```bash
curl -s -X POST http://127.0.0.1:8000/retrieval/nodes/bodies \
  -H 'Content-Type: application/json' \
  -d '{"ids": ["chunk-1a2b", "chunk-2b3c"], "max_chars": 1500}'
```

响应：`nodes[]`（同单个接口的完整结构）与 `missing[]`（不存在的 ID，逐条失败不影响其它条）。

---

## 10. 实体识别与消歧 `POST /retrieval/entities/resolve`

把一段文本里的实体提及识别出来，并对齐到图谱里的实体节点（给出候选与分数）。

### 请求参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `text` | str | 必填 | 待分析文本（≤ 20000 字符） |
| `use_llm` | bool | true | 是否先用大模型抽取；失败/未配置时自动退回词典匹配 |
| `top_k` | int | 3 | 每个提及最多几个候选（1~10） |
| `min_score` | float | 0.55 | 判定"已消歧"的最低分（0~1） |
| `entity_types` | list[str] | 无 | 只在这些实体类型里匹配 |

### 响应字段

| 字段 | 说明 |
| --- | --- |
| `extractor` | `llm`（大模型抽到了）/ `dictionary`（退回词典匹配）/ `hybrid` |
| `mentions[]` | `text` / `start` / `end`（原文偏移，左闭右开）/ `type` / `resolved` / `candidates[]` |
| `resolved` | 最高分候选且分数 ≥ `min_score`，否则 `null` |
| `candidates[]` | `id` / `name` / `type` / `score` / `match`（`exact`/`alias`/`contains`/`fuzzy`/`embedding`） |
| `elapsed_ms` | 服务端耗时 |

> 词典匹配用的是图谱里的 `Entity.name` 与 `aliases`（列表取最长优先、span 不重叠），
> 所以"老张"这类别名也能命中；大模型只负责补充"没进图谱的新提及"，最终仍以图谱为准做消歧。

### 示例

```bash
curl -s -X POST http://127.0.0.1:8000/retrieval/entities/resolve \
  -H 'Content-Type: application/json' \
  -d '{"text": "老张在星河科技负责知识图谱平台", "min_score": 0.5}'
```

```json
{
  "text_length": 16,
  "extractor": "dictionary",
  "mentions": [
    {"text": "老张", "start": 0, "end": 2, "type": "人物",
     "resolved": {"id": "ent-8f2a", "name": "张三", "type": "人物", "score": 1.0, "match": "alias"},
     "candidates": [{"id": "ent-8f2a", "name": "张三", "type": "人物", "score": 1.0, "match": "alias"}]},
    {"text": "星河科技", "start": 3, "end": 7, "type": "组织",
     "resolved": {"id": "ent-3c1d", "name": "星河科技", "type": "组织", "score": 1.0, "match": "exact"},
     "candidates": [{"id": "ent-3c1d", "name": "星河科技", "type": "组织", "score": 1.0, "match": "exact"}]}
  ],
  "elapsed_ms": 9.6
}
```

---

## 11. 典型工作流

一个外部智能体完成"查资料 → 看原文"的最小闭环：

```text
1. GET  /retrieval/status          三绿再继续（失败就看 components.*.error）
2. GET  /retrieval/schema          确认标签/关系/索引，决定用哪种检索
3. POST /retrieval/semantic        不确定用词时先按语义找实体
   GET  /retrieval/keywords        已知关键词时更便宜
4. POST /retrieval/traverse        从命中的实体取 1~2 跳子图，看关系网
   POST /retrieval/paths           或查两个实体之间怎么连
5. GET  /retrieval/nodes/{id}      读回证据原文（分块/文档）
6. POST /retrieval/entities/resolve 把用户新文本里的说法对齐到图谱节点
```

Python 示例（只用标准库）：

```python
import json
import urllib.request

BASE = "http://127.0.0.1:8000"
TOKEN = ""  # 配置了 api.auth_token 时填上

def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if TOKEN:
        request.add_header("X-API-Token", TOKEN)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())

status = call("GET", "/retrieval/status?probe_llm=false")
assert status["ok"], status["components"]

entities = call("POST", "/retrieval/semantic", {"query": "谁创办了这家公司"})["entities"]
if entities:
    subgraph = call("POST", "/retrieval/traverse", {"start": entities[0]["id"], "depth": 2})
    for node in subgraph["nodes"]:
        if node["kind"] == "chunk":
            body = call("GET", f"/retrieval/nodes/{node['id']}?max_chars=1500")
            print(body["body"]["text"][:200])
```

---

## 12. 与 `/ask` 的区别

| | `/ask` | `/retrieval/*` |
| --- | --- | --- |
| 面向 | 最终用户提问 | 其它智能体/程序取原料 |
| 返回 | 自然语言答案 + 引用 + 过程 | 结构化实体/关系/子图/正文/候选 |
| 会调大模型 | 是（分析、生成 Cypher、写答案、自检） | 只有实体识别默认调用，可关（`use_llm=false`） |
| 适合 | 直接展示给用户 | 二次加工、规划、多源融合 |

两者共用同一个图谱与同一套只读查询。

---

## 13. 限制与性能提示

* **只读**：本组接口不会写图谱；写操作只发生在 `/ingest*`。
* **遍历/路径成本**：`depth` 与 `node_limit` 越大越慢；建议深度 ≤ 2，超大图用关键词/语义先缩小范围。
* **语义检索**：开启向量时首次会为候选实体算向量并缓存（进程内 LRU）；重复查询很快。
  未开启向量时 `mode=lexical`，对同义改写不敏感，属于"能用的降级"。
* **实体识别**：`use_llm=true` 才调模型；批量处理长文本建议先切段，避免超过 `text` 长度上限。
* **并发**：检索接口可并发；导入任务仍然串行（会写同一张图）。
* **鉴权**：对外暴露时务必配置 `api.auth_token`，并限制 `api.allowed_path_roots`（导入侧）。

---

## 14. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-10-25 | 首版：8 类能力 / 9 个端点（状态、Schema、关键词 GET+POST、语义、遍历、路径、节点正文 GET+批量、实体消歧） |
