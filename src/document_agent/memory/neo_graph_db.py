import re
import numpy as np
from neo4j import GraphDatabase
from sentence_transformers import SentenceTransformer


class MemgraphNodeManager:
    """
    Memgraph 节点管理类
    - 支持带嵌入向量的节点 CRUD
    - 支持精确匹配 / 正则匹配的复合查询条件
    - 支持前驱/后继节点查询
    - 支持向量相似度检索
    - 所有查询结果可按向量距离排序
    """

    def __init__(self, uri="bolt://localhost:7687", auth=("", ""),
                 model_name="paraphrase-multilingual-MiniLM-L12-v2",
                 database="memgraph"):
        self.driver = GraphDatabase.driver(uri, auth=auth)
        self.model = SentenceTransformer(model_name)
        self.vector_dim = self.model.get_embedding_dimension()
        self.database = database

    def close(self):
        self.driver.close()

    # =====================================================
    # 工具方法
    # =====================================================
    def embed(self, text: str) -> list:
        """文本 -> 嵌入向量"""
        return self.model.encode(text).tolist()

    def _build_where(self, label: str, conditions: dict, only_root: bool = False):
        """
        根据条件字典构造 WHERE 子句。
        规则：
        - 值为 str 且以 'regex:' 开头 -> 正则匹配 (=~)
        - 值为 str -> 精确匹配 (=)
        - 其他类型（int/float/bool）-> 精确匹配 (=)
        - 值为 dict 形式 {'$regex': '...'} 也可支持（更显式）
        返回: (where_clause:str, params:dict)
        """
        if not conditions:
            return "", {}

        clauses = []
        params = {}

        for i, (field, value) in enumerate(conditions.items()):
            if field.endswith("_embedding"):
                # 向量字段：使用 = 精确比较（不常用，但保留兼容）
                key = f"p{i}"
                clauses.append(f"n.{field} = ${key}")
                params[key] = value
                continue

            # 显式 dict 写法：{'$regex': 'xxx'}
            if isinstance(value, dict):
                if "$regex" in value:
                    key = f"p{i}"
                    clauses.append(f"n.{field} =~ ${key}")
                    params[key] = value["$regex"]
                elif "$eq" in value:
                    key = f"p{i}"
                    clauses.append(f"n.{field} = ${key}")
                    params[key] = value["$eq"]
                else:
                    raise ValueError(f"不支持的查询操作符: {value}")
                continue

            # 字符串隐式写法
            if isinstance(value, str) and value.startswith("regex:"):
                key = f"p{i}"
                clauses.append(f"n.{field} =~ ${key}")
                params[key] = value[len("regex:"):]
            else:
                key = f"p{i}"
                clauses.append(f"n.{field} = ${key}")
                params[key] = value
                
        if only_root:
            clauses.append("NOT EXISTS { ()-->(n) }")

        where_clause = "WHERE " + " AND ".join(clauses)
        return where_clause, params

    def _distance(self, vec_a, vec_b) -> float:
        """余弦距离 = 1 - 余弦相似度"""
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        denom = (np.linalg.norm(a) * np.linalg.norm(b))
        if denom == 0:
            return 1.0
        return float(1.0 - np.dot(a, b) / denom)

    # =====================================================
    # 索引 / 约束
    # =====================================================
    def create_unique_constraint(self, label: str, property_name: str = "id"):
        with self.driver.session(database=self.database) as session:
            session.run(
                f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{property_name} IS UNIQUE"
            )
            print(f"✅ 唯一约束: {label}.{property_name}")

    def create_vector_index(self, index_name: str, label: str,
                            property_name: str = "embedding",
                            capacity: int = 10000, metric: str = "cos"):
        with self.driver.session(database=self.database) as session:
            session.run(f"""
                CREATE VECTOR INDEX {index_name}
                ON :{label}({property_name})
                WITH CONFIG {{
                    "dimension": {self.vector_dim},
                    "capacity": {capacity},
                    "metric": "{metric}"
                }}
            """)
            print(f"✅ 向量索引: {index_name}")

    def drop_vector_index(self, index_name: str):
        with self.driver.session(database=self.database) as session:
            session.run(f"DROP VECTOR INDEX {index_name}")

    # =====================================================
    # 1. 插入节点
    # =====================================================
    def insert_node(self, label: str, data: dict,
                    text_field: str = None,
                    embedding_field: str = "embedding") -> dict:
        """
        插入节点
        :param label: 节点标签
        :param data: 节点属性字典
        :param text_field: 用于生成嵌入向量的文本字段名（如 'bio'）
        :param embedding_field: 向量属性名，默认 'embedding'
        """
        data = dict(data)

        if text_field and text_field in data and data[text_field]:
            data[embedding_field] = self.embed(data[text_field])

        keys = ", ".join([f"{k}: ${k}" for k in data.keys()])
        query = f"CREATE (n:{label} {{{keys}}}) RETURN n"

        with self.driver.session(database=self.database) as session:
            result = session.run(query, **data)
            record = result.single()
            return dict(record["n"].items()) if record else None

    # =====================================================
    # 2. 查找节点
    # =====================================================
    def find_nodes(self, label: str, conditions: dict = None,
                   order_by_vector: bool = False,
                   query_text: str = None,
                   embedding_field: str = "embedding",
                   limit: int = None) -> list:
        """
        按条件查找节点
        :param label: 标签
        :param conditions: 查询条件字典（AND 组合）
        :param order_by_vector: 是否按向量距离排序
        :param query_text: 排序用的查询文本（若为 None 则用 conditions 中的文本字段拼接）
        :param embedding_field: 向量属性名
        :param limit: 返回条数限制
        """
        where_clause, params = self._build_where(label, conditions or {})

        query = f"""
            MATCH (n:{label})
            {where_clause}
            RETURN n
        """

        with self.driver.session(database=self.database) as session:
            result = session.run(query, **params)
            nodes = [dict(record["n"].items()) for record in result]

        # 若需要按向量距离排序
        if order_by_vector:
            nodes = self._sort_by_vector(nodes, query_text,
                                         conditions, embedding_field)

        if limit:
            nodes = nodes[:limit]
        return nodes

    def _sort_by_vector(self, nodes: list, query_text: str,
                        conditions: dict, embedding_field: str) -> list:
        """内部方法：按与目标向量的余弦距离升序排序"""
        if not nodes:
            return nodes

        # 确定目标向量
        if query_text is None:
            # 用 conditions 中所有字符串字段拼成查询文本
            parts = []
            for v in (conditions or {}).values():
                if isinstance(v, str):
                    parts.append(v)
            query_text = " ".join(parts)

        if not query_text:
            return nodes

        target_vec = self.embed(query_text)

        # 计算每个节点与目标向量的距离
        scored = []
        for n in nodes:
            vec = n.get(embedding_field)
            if vec:
                dist = self._distance(target_vec, vec)
            else:
                dist = float("inf")
            scored.append((dist, n))

        scored.sort(key=lambda x: x[0])
        # 把距离也附到结果上，方便查看
        result = []
        for dist, n in scored:
            item = dict(n)
            item["_distance"] = dist
            result.append(item)
        return result

    # =====================================================
    # 3. 删除节点（连同其关联边）
    # =====================================================
    def delete_nodes(self, label: str, conditions: dict) -> int:
        """
        按条件删除节点，DETACH DELETE 同时删除关联边
        返回删除的节点数量
        """
        where_clause, params = self._build_where(label, conditions or {})

        query = f"""
            MATCH (n:{label})
            {where_clause}
            WITH n, count(n) AS cnt
            DETACH DELETE n
            RETURN count(n) AS deleted
        """

        with self.driver.session(database=self.database) as session:
            # 先数一下待删除的节点数（DETACH DELETE 后无法再返回 count）
            count_query = f"""
                MATCH (n:{label})
                {where_clause}
                RETURN count(n) AS cnt
            """
            cnt = session.run(count_query, **params).single()["cnt"]

            if cnt > 0:
                session.run(f"""
                    MATCH (n:{label})
                    {where_clause}
                    DETACH DELETE n
                """, **params)

            return cnt

    # =====================================================
    # 4. 修改节点
    # =====================================================
    def update_nodes(self, label: str, match_conditions: dict,
                     update_data: dict,
                     text_field: str = None,
                     embedding_field: str = "embedding") -> list:
        """
        修改节点
        :param match_conditions: 查找条件字典
        :param update_data: 要修改的内容字典（只更新这里出现的字段）
        :param text_field: 若更新内容中该字段变化，则重新生成嵌入向量
        :return: 更新后的节点列表
        """
        where_clause, params = self._build_where(label, match_conditions or {})
        update_data = dict(update_data)

        # 若更新了文本字段，同步刷新嵌入向量
        if text_field and text_field in update_data and update_data[text_field]:
            update_data[embedding_field] = self.embed(update_data[text_field])

        if not update_data:
            return []

        # 构造 SET 子句
        set_clauses = []
        for i, (k, v) in enumerate(update_data.items()):
            key = f"u{i}"
            set_clauses.append(f"n.{k} = ${key}")
            params[key] = v

        set_clause = "SET " + ", ".join(set_clauses)

        query = f"""
            MATCH (n:{label})
            {where_clause}
            {set_clause}
            RETURN n
        """

        with self.driver.session(database=self.database) as session:
            result = session.run(query, **params)
            return [dict(record["n"].items()) for record in result]

    # =====================================================
    # 5. 查询前驱 / 后继节点
    # =====================================================
    def get_neighbors(self, label: str, conditions: dict,
                      edge_type: str = None,
                      direction: str = "both",
                      depth: int = 1,
                      order_by_vector: bool = False,
                      query_text: str = None,
                      embedding_field: str = "embedding") -> list:
        """
        查询某个节点的所有前驱/后继节点
        :param direction: "out" 后继, "in" 前驱, "both" 两者
        :param edge_type: 关系类型，None 表示任意类型
        :param depth: 遍历深度，1 表示仅直接邻居
        :param order_by_vector: 是否按向量距离排序
        :return: 邻居节点列表（去重）
        """
        where_clause, params = self._build_where(label, conditions or {})

        rel = f":{edge_type}" if edge_type else ""

        if direction == "out":
            pattern = f"-[:{edge_type}*1..{depth}]->" if edge_type else f"-[*1..{depth}]->"
        elif direction == "in":
            pattern = f"<-[:{edge_type}*1..{depth}]-" if edge_type else f"<-[*1..{depth}]-"
        else:
            pattern = f"-[:{edge_type}*1..{depth}]-" if edge_type else f"-[*1..{depth}]-"

        query = f"""
            MATCH (n:{label}){pattern}(m)
            {where_clause}
            RETURN DISTINCT m
        """

        with self.driver.session(database=self.database) as session:
            result = session.run(query, **params)
            neighbors = [dict(record["m"].items()) for record in result]

        if order_by_vector:
            neighbors = self._sort_by_vector(
                neighbors, query_text, conditions, embedding_field
            )

        return neighbors

    # =====================================================
    # 6. 按嵌入向量检索节点
    # =====================================================
    def search_by_vector(self, label: str, query_text: str,
                         index_name: str = None,
                         top_k: int = 10,
                         embedding_field: str = "embedding",
                         extra_conditions: dict = None,
                         order_by_distance: bool = True,
                         only_root: bool = False) -> list:
        """
        向量相似度检索
        优先使用向量索引（如果提供 index_name），否则在全量节点上计算距离。
        :param extra_conditions: 额外的属性过滤条件（AND）
        :param order_by_distance: 是否按距离排序（默认 True）
        """
        query_vector = self.embed(query_text)

        # ---- 使用向量索引 ----
        if index_name:
            with self.driver.session(database=self.database) as session:
                result = session.run("""
                    CALL vector_search.search($index_name, $top_k, $query_vector)
                    YIELD node, similarity
                    RETURN node, similarity
                """, index_name=index_name, top_k=top_k,
                     query_vector=query_vector)
                items = []
                for record in result:
                    node = dict(record["node"].items())
                    node["_similarity"] = record["similarity"]
                    node["_distance"] = 1.0 - record["similarity"]  # cos 距离
                    items.append(node)

            # 过滤额外条件
            if extra_conditions:
                items = [n for n in items
                         if self._match_conditions(n, extra_conditions)]

            if order_by_distance:
                items.sort(key=lambda x: x.get("_distance", float("inf")))
            return items[:top_k]

        # ---- 无索引：全量拉取后本地计算 ----
        where_clause, params = self._build_where(label, extra_conditions or {},only_root=only_root)
        query = f"""
            MATCH (n:{label})
            {where_clause}
            RETURN n
        """
        with self.driver.session(database=self.database) as session:
            result = session.run(query, **params)
            nodes = [dict(record["n"].items()) for record in result]

        scored = []
        for n in nodes:
            vec = n.get(embedding_field)
            if not vec:
                continue
            dist = self._distance(query_vector, vec)
            item = dict(n)
            item["_distance"] = dist
            item["_similarity"] = 1.0 - dist
            scored.append((dist, item))

        scored.sort(key=lambda x: x[0])
        return [item for _, item in scored[:top_k]]

    def _match_conditions(self, node: dict, conditions: dict) -> bool:
        """本地校验节点是否满足条件（支持正则）"""
        for k, v in conditions.items():
            if k not in node:
                return False
            nv = node[k]
            if isinstance(v, str) and v.startswith("regex:"):
                if not re.search(v[len("regex:"):], str(nv)):
                    return False
            elif isinstance(v, dict) and "$regex" in v:
                if not re.search(v["$regex"], str(nv)):
                    return False
            else:
                if nv != v:
                    return False
        return True

    # =====================================================
    # 辅助：创建边（便于测试前驱/后继）
    # =====================================================
    def create_edge(self, from_label: str, from_conditions: dict,
                to_label: str, to_conditions: dict,
                rel_type: str, properties: dict = None) -> int:
        """在两个节点之间创建有向边，返回创建条数"""

        def build_where(var: str, conditions: dict, prefix: str):
            """构造 WHERE 子句，变量名由调用方传入（a 或 b）"""
            if not conditions:
                return "", {}
            clauses, params = [], {}
            for i, (field, value) in enumerate(conditions.items()):
                key = f"{prefix}p{i}"
                if isinstance(value, str) and value.startswith("regex:"):
                    clauses.append(f"{var}.{field} =~ ${key}")
                    params[key] = value[len("regex:"):]
                elif isinstance(value, dict) and "$regex" in value:
                    clauses.append(f"{var}.{field} =~ ${key}")
                    params[key] = value["$regex"]
                elif isinstance(value, dict) and "$eq" in value:
                    clauses.append(f"{var}.{field} = ${key}")
                    params[key] = value["$eq"]
                else:
                    clauses.append(f"{var}.{field} = ${key}")
                    params[key] = value
            return "WHERE " + " AND ".join(clauses), params

        where_a, params_a = build_where("a", from_conditions or {}, "a")
        where_b, params_b = build_where("b", to_conditions or {}, "b")

        props = properties or {}
        params = {**params_a, **params_b}

        # 关系属性
        prop_clause = ""
        if props:
            prop_clause = "{" + ", ".join([f"{k}: $r_{k}" for k in props.keys()]) + "}"
            for k, v in props.items():
                params[f"r_{k}"] = v

        query = f"""
            MATCH (a:{from_label})
            {where_a}
            MATCH (b:{to_label})
            {where_b}
            CREATE (a)-[r:{rel_type} {prop_clause}]->(b)
            RETURN count(r) AS cnt
        """

        with self.driver.session(database=self.database) as session:
            result = session.run(query, **params)
            return result.single()["cnt"]