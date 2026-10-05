"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    target = normalize(name or "")
    if not target:
        return None
    by_normalized = {}
    for original in known:
        by_normalized.setdefault(normalize(original), original)
    if target in by_normalized:
        return by_normalized[target]
    close = difflib.get_close_matches(target, list(by_normalized), n=1, cutoff=0.8)
    return by_normalized[close[0]] if close else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Ontology v2 (own design, see report/ONTOLOGY.md section 7)
#
#   (:Source {doc_id, title, url})-[:REPORTS]->(:Case {id, name, summary, date, doc_id})
#   (:Person {name, aliases})-[:INVOLVED_IN {role, stage, sentence, charge}]->(:Case)
#   (:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(:Article)-[:HAS_CLAUSE]->(:Clause {severity, ...})
#   (:Case)-[:INVOLVES {amount, amount_g}]->(:Substance)          # canonical names only
#   (:Clause)-[:HAS_THRESHOLD]->(:Threshold {min_g, max_g})-[:FOR]->(:Substance)
# ----------------------------------------------------------------------------------------------

OTHER_SUBSTANCE = "chất ma túy khác"       # the law's own catch-all ("Các chất ma túy khác ...")
SUBSTANCES_V2 = SUBSTANCES + ["Etomidate", OTHER_SUBSTANCE]
SUBSTANCE_ALIASES = {
    "heroin": "Heroine", "thuốc lắc": "MDMA", "ecstasy": "MDMA", "ma túy đá": "Methamphetamine",
    "ketamin": "Ketamine", "cỏ": "cần sa", "ma túy": OTHER_SUBSTANCE, "chất ma túy": OTHER_SUBSTANCE,
    "ma túy tổng hợp": OTHER_SUBSTANCE, "ma tuý": OTHER_SUBSTANCE,
}
STAGES = ["bắt giữ", "khởi tố", "truy tố", "xét xử sơ thẩm", "xét xử phúc thẩm", "không rõ"]
POINT_START = re.compile(r"^([a-zđ])\)\s", re.MULTILINE)
THRESHOLD = re.compile(
    r"([\d.,]+) (gam|kilôgam)(?: đến dưới ([\d.,]+) (gam|kilôgam))?( trở lên)?")

def canonical_substance(name: str) -> str | None:
    """Map a mention ('ketamine', 'thuốc lắc', 'ma túy') onto one canonical Substance name."""
    key = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not key or key in ("không rõ", "không xác định"):
        return None
    if key in SUBSTANCE_ALIASES:
        return SUBSTANCE_ALIASES[key]
    return link_entity(key, SUBSTANCES_V2, normalize=lambda s: re.sub(r"\s+", " ", s.strip().lower()))

def _number(text: str) -> float:
    text = text.strip(".,")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        return float(text.replace(".", ""))
    return float(text.replace(",", "."))

def _grams(value: str, unit: str) -> float:
    return _number(value) * (1000 if unit == "kilôgam" else 1)

def penalty_severity(penalty: str) -> int:
    """Rank a penalty so 'the heaviest clause' is a number: death > life > years of prison > 0."""
    if "tử hình" in penalty:
        return 1000
    if "chung thân" in penalty:
        return 100
    years = [int(y) for y in re.findall(r"(\d+) năm", penalty)] if "tù" in penalty else []
    return max(years, default=0)

def parse_thresholds(clause: dict) -> list[dict]:
    """Per-point quantity thresholds of a clause, e.g. 'MDMA ... có khối lượng 100 gam trở lên'."""
    text = clause["text"]
    starts = list(POINT_START.finditer(text))
    out = []
    for i, start in enumerate(starts):
        point = text[start.start(): starts[i + 1].start() if i + 1 < len(starts) else len(text)].strip()
        if "khối lượng" not in point:
            continue
        found = THRESHOLD.search(point.split("khối lượng", 1)[1])
        if not found:
            continue
        low, low_unit, high, high_unit, at_least = found.groups()
        substances = find_substances(point)
        if "chất ma túy khác" in point.lower():
            substances = [OTHER_SUBSTANCE]
        if not substances:
            continue
        out.append({
            "id": f"{clause['id']} điểm {start.group(1)}", "point": start.group(1),
            "min_g": _grams(low, low_unit), "max_g": _grams(high, high_unit) if high else None,
            "text": point, "substances": substances,
        })
    return out

def parse_law_article_v2(doc: Document) -> dict[str, Any]:
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause["severity"] = penalty_severity(clause["penalty"])
        clause["thresholds"] = parse_thresholds(clause)
    return article

NEWS_EXTRACTION_PROMPT_V2 = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, BẮT BUỘC chọn từ DANH SÁCH CHẤT (thuốc lắc = MDMA; ma túy đá = Methamphetamine; chất không có trong danh sách hoặc nói chung chung = chất ma túy khác)",
                   "amount": "khối lượng đúng như bài viết, chuỗi rỗng nếu không có",
                   "amount_g": "khối lượng quy ra GAM, chỉ là một con số (9,6kg = 9600; 0,686g = 0.686), null nếu không rõ"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "stage": "giai đoạn tố tụng của người này trong bài, chọn một trong: {stages}",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Một bài có thể có nhiều vụ việc riêng biệt; mỗi vụ là một phần tử của "cases".
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases_v2(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    prompt = NEWS_EXTRACTION_PROMPT_V2.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES_V2), stages=" | ".join(STAGES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        merged: dict[str, dict] = {}
        for item in case.get("substances", []):
            name = canonical_substance(item.get("name", ""))
            if not name:
                continue
            try:
                grams = float(item["amount_g"]) if item.get("amount_g") not in (None, "") else None
            except (TypeError, ValueError):
                grams = None
            current = merged.setdefault(name, {"name": name, "amount": "", "amount_g": None})
            current["amount"] = current["amount"] or item.get("amount", "")
            if grams is not None:
                current["amount_g"] = max(grams, current["amount_g"] or 0)
        case["substances"] = list(merged.values())
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
            if person.get("stage") not in STAGES:
                person["stage"] = "không rõ"
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- ontology v2: writes

    def constraints_v2(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Threshold", "id"), ("Crime", "name"),
                           ("Case", "id"), ("Substance", "name"), ("Person", "name"), ("Location", "name"),
                           ("Source", "doc_id")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article_v2(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text,
                  cl.severity = clause.severity, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )
        thresholds = [dict(t, clause_id=c["id"]) for c in article["clauses"] for t in c["thresholds"]]
        if thresholds:
            self.run(
                """
                UNWIND $thresholds AS t
                MATCH (cl:Clause {id: t.clause_id})
                MERGE (th:Threshold {id: t.id})
                  SET th.point = t.point, th.min_g = t.min_g, th.max_g = t.max_g, th.text = t.text, th.doc_id = $doc_id
                MERGE (cl)-[:HAS_THRESHOLD]->(th)
                FOREACH (s IN t.substances | MERGE (sub:Substance {name: s}) MERGE (th)-[:FOR]->(sub))
                """,
                thresholds=thresholds, doc_id=article["doc_id"],
            )

    def add_news_case_v2(self, case: dict, index: int, doc: Document) -> None:
        self.run(
            """
            MERGE (src:Source {doc_id: $doc_id}) SET src.title = $title, src.url = $url
            MERGE (k:Case {id: $case_id})
              SET k.name = $name, k.summary = $summary, k.date = $date, k.doc_id = $doc_id
            MERGE (src)-[:REPORTS]->(k)
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount, r.amount_g = s.amount_g)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(person.aliases, []) + [a IN coalesce(p.aliases, [])
                                      WHERE NOT a IN coalesce(person.aliases, [])]
                MERGE (person)-[r:INVOLVED_IN]->(k)
                SET r.role = p.role, r.stage = p.stage, r.charge = p.charge, r.sentence = p.sentence)
            """,
            case_id=f"{doc.id}#{index}", name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""), url=doc.metadata.get("source_url", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts: seeds + 1 hop, then the legal basis of each case reached (ontology v2).

        For a case the legal basis is: clause 1 (base frame) + the clauses whose quantity Threshold matches
        the case's amount_g for each substance (falls back to clauses that MENTION the substance) + the
        heaviest clause when the question asks for a maximum. Questions naming a substance also get
        every case that involves it (aggregation).
        """
        seed_ids, facts = self.seed_facts(question, doc_ids, skip_labels=("Threshold", "Source", "Clause"))
        facts = list(facts)
        seen: set[str] = set()
        wants_max = bool(re.search(r"tối đa|cao nhất|nặng nhất|lớn nhất", question.lower()))

        def add_clause(row: dict, why: str = "") -> None:
            if row["clause_id"] in seen:
                return
            seen.add(row["clause_id"])
            facts.append(f"[{row['article_id']} - {row['title']}] khoản {row['number']}{why}: {row['text']}")

        clause_cols = "cl.id AS clause_id, a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text"

        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.id AS key, k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        for case in cases:
            facts.append(f"Vụ việc '{case['name']}': {case['summary']}")

        for case in cases:
            # a person named in the question narrows the case to that person's own charge
            own = [r["charge"] for r in self.run(
                """MATCH (p:Person)-[r:INVOLVED_IN]->(k:Case {id: $key})
                   WHERE elementId(p) IN $ids AND coalesce(r.charge, '') <> '' RETURN DISTINCT r.charge AS charge""",
                key=case["key"], ids=seed_ids)]
            articles = self.run(
                """MATCH (k:Case {id: $key})-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)
                   WHERE size($own) = 0 OR c.name IN $own RETURN DISTINCT a.id AS id""",
                key=case["key"], own=own)
            amounts = self.run(
                "MATCH (k:Case {id: $key})-[r:INVOLVES]->(s:Substance) RETURN s.name AS name, r.amount_g AS grams",
                key=case["key"])
            for article in articles:
                for row in self.run(
                        f"MATCH (a:Article {{id: $aid}})-[:HAS_CLAUSE]->(cl:Clause) WHERE cl.number = 1 RETURN {clause_cols}",
                        aid=article["id"]):
                    add_clause(row)
                thresholds = self.run(
                    f"""
                    MATCH (a:Article {{id: $aid}})-[:HAS_CLAUSE]->(cl:Clause)-[:HAS_THRESHOLD]->(t:Threshold)-[:FOR]->(s:Substance)
                    RETURN s.name AS substance, t.min_g AS lo, t.max_g AS hi, {clause_cols}
                    """,
                    aid=article["id"])
                covered = {t["substance"] for t in thresholds}
                for item in amounts:
                    name = item["name"] if item["name"] in covered else OTHER_SUBSTANCE
                    grams = item["grams"]
                    hits = [t for t in thresholds if t["substance"] == name and grams is not None
                            and t["lo"] <= grams and (t["hi"] is None or grams < t["hi"])]
                    for t in hits:
                        upper = "+" if t["hi"] is None else "-" + format(t["hi"], "g")
                        add_clause(t, f" (khối lượng {grams:g} g, ngưỡng {t['lo']:g}{upper} g)")
                    if not hits:   # no usable amount: clauses of this article that mention the substance
                        for row in self.run(
                                f"""MATCH (a:Article {{id: $aid}})-[:HAS_CLAUSE]->(cl:Clause)-[:MENTIONS]->(:Substance {{name: $s}})
                                    RETURN {clause_cols}""", aid=article["id"], s=item["name"]):
                            add_clause(row)
                if wants_max:
                    for row in self.run(
                            f"""MATCH (a:Article {{id: $aid}})-[:HAS_CLAUSE]->(cl:Clause)
                                RETURN {clause_cols} ORDER BY cl.severity DESC LIMIT 1""", aid=article["id"]):
                        add_clause(row, " (khung nặng nhất)")

        # articles named in the question
        subs = [canonical_substance(x) or x for x in find_substances(question)]
        for num in sorted(set(re.findall(r"[Đđ]iều (\d+)", question))):
            for row in self.run(
                    f"""
                    MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                    WHERE a.id STARTS WITH 'Điều ' + $num + ' '
                      AND (cl.number = 1 OR EXISTS {{ MATCH (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $subs }})
                    RETURN DISTINCT {clause_cols} ORDER BY number""", num=num, subs=subs):
                add_clause(row)

        # aggregation: every case that involves a substance named in the question
        for name in subs:
            for row in self.run(
                    """
                    MATCH (k:Case)-[r:INVOLVES]->(:Substance {name: $s})
                    OPTIONAL MATCH (p:Person)-[:INVOLVED_IN]->(k)
                    RETURN k.name AS name, r.amount AS amount, collect(DISTINCT p.name)[..4] AS people
                    """, s=name):
                facts.append(f"Vụ việc có {name}: '{row['name']}' (khối lượng: {row['amount'] or 'không rõ'};"
                             f" người liên quan: {', '.join(row['people']) or 'không rõ'})")
        return facts

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.constraints_v2()
    articles = [parse_law_article_v2(d) for d in law_docs]
    for article in articles:
        graph.add_law_article_v2(article)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for doc in news_docs:
        for index, case in enumerate(extract_news_cases_v2(doc, lambda p: llm_fn(p, json_mode=True), crimes)):
            graph.add_news_case_v2(case, index, doc)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(c["metadata"]["doc_id"] for c in chunks if c["metadata"].get("doc_id")))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {f}" for f in facts),
            chunks="\n".join(f"[{i}] {c['content']}" for i, c in enumerate(chunks, 1)),
            question=question,
        )
        return self.llm_fn(prompt)
