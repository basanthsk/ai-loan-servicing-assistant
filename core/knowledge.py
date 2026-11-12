"""Policy knowledge base: retrieval over policy clauses and FAQ documents (RAG).

Documents are split into passages that each carry a citable id:

- every clause in data/policy/*.yaml (e.g. LT-6.1), one passage each
- every `## [ID] Title` section of the Markdown files in data/kb/

Passages are embedded with a Hugging Face model run locally by fastembed (ONNX,
no API key; BAAI/bge-small-en-v1.5 by default) and stored in a persistent Chroma
collection per domain pack. The index is rebuilt automatically when any
document or the model changes (a fingerprint is stored with the collection).

Retrieval is for questions the executable rules do not cover ("how do I change
my EMI date?", "who do I complain to?"). Customer-specific figures still come
from the systems and calculators, never from retrieved text.

This module is platform code: a new domain pack gets retrieval by adding
`pack: <name>` documents, without code changes.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.settings import Settings, get_settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "data" / "policy"
KB_DIR = ROOT / "data" / "kb"

Embedder = Callable[[list[str], bool], list[list[float]]]  # (texts, is_query) -> vectors


@dataclass
class Passage:
    id: str
    pack: str
    title: str
    text: str
    document: str
    source: str = ""
    url: str = ""

    def embed_text(self) -> str:
        return f"{self.title}. {self.text}"

    def metadata(self) -> dict:
        return {
            "pack": self.pack,
            "title": self.title,
            "document": self.document,
            "source": self.source,
            "url": self.url,
        }


# --- Loading and chunking --------------------------------------------------------------

_SECTION = re.compile(r"^##\s+\[(?P<id>[A-Z0-9.\-]+)\]\s+(?P<title>.+?)\s*$", re.M)
_SOURCE_LINE = re.compile(r"^Source:\s*(?P<src>.+)\Z", re.M | re.S)


def _front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, head, body = text.split("---", 2)
        return yaml.safe_load(head) or {}, body
    return {}, text


def load_markdown(path: Path) -> list[Passage]:
    meta, body = _front_matter(path.read_text(encoding="utf-8"))
    pack = meta.get("pack")
    if not pack:
        raise ValueError(f"{path.name}: front matter needs 'pack'")
    matches = list(_SECTION.finditer(body))
    passages = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[m.end() : end].strip()
        source = meta.get("source", "")
        src = _SOURCE_LINE.search(text)
        if src:  # a section may name its own source on a last "Source:" line
            source = " ".join(src.group("src").split())
            text = text[: src.start()].strip()
        passages.append(
            Passage(
                id=m.group("id"),
                pack=pack,
                title=m.group("title"),
                text=" ".join(text.split()),
                document=path.stem,
                source=source,
            )
        )
    return passages


def load_clauses(path: Path) -> list[Passage]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    pack = doc.get("pack")
    if not pack:
        raise ValueError(f"{path.name}: needs a top-level 'pack'")
    return [
        Passage(
            id=c["id"],
            pack=pack,
            title=c["title"],
            text=" ".join(c["text"].split()),
            document=path.stem,
            source=c.get("source", ""),
            url=c.get("url", ""),
        )
        for c in doc["clauses"]
    ]


def load_passages(policy_dir: Path = POLICY_DIR, kb_dir: Path = KB_DIR) -> list[Passage]:
    passages = [p for f in sorted(policy_dir.glob("*.yaml")) for p in load_clauses(f)]
    passages += [p for f in sorted(kb_dir.glob("*.md")) for p in load_markdown(f)]
    ids = [p.id for p in passages]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate knowledge base ids: {sorted(dupes)}")
    return passages


# --- Embedders ---------------------------------------------------------------------------


def fastembed_embedder(model: str, cache_dir: str) -> Embedder:
    """Hugging Face model run locally through ONNX. Downloaded once (about 65 MB)."""
    from fastembed import TextEmbedding

    m = TextEmbedding(model, cache_dir=cache_dir)

    def embed(texts: list[str], is_query: bool) -> list[list[float]]:
        # bge models expect a query instruction prefix; query_embed adds it.
        vectors = m.query_embed(texts) if is_query else m.embed(texts)
        return [v.tolist() for v in vectors]

    return embed


def hash_embedder(dim: int = 512) -> Embedder:
    """Offline bag-of-words embedder for tests: no download, deterministic."""
    import math

    def vec(text: str) -> list[float]:
        v = [0.0] * dim
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            if len(w) > 2:
                v[int(hashlib.md5(w[:6].encode()).hexdigest(), 16) % dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    return lambda texts, is_query: [vec(t) for t in texts]


def make_embedder(settings: Settings) -> tuple[Embedder, str]:
    if settings.kb_embedder == "hash":
        return hash_embedder(), "hash-512"
    if settings.kb_embedder == "fastembed":
        return (
            fastembed_embedder(settings.kb_embed_model, settings.kb_model_dir),
            settings.kb_embed_model,
        )
    raise ValueError(f"Unknown KB_EMBEDDER {settings.kb_embedder!r}")


# --- Knowledge base ----------------------------------------------------------------------


@dataclass
class Hit:
    id: str
    title: str
    text: str
    source: str
    document: str
    score: float
    url: str = ""

    def as_dict(self) -> dict:
        d = {
            "id": self.id,
            "title": self.title,
            "text": self.text,
            "source": self.source,
            "document": self.document,
            "score": round(self.score, 3),
        }
        if self.url:
            d["url"] = self.url
        return d


_CLAUSE_ID = re.compile(r"\b([A-Z]{2,3}(?:-[A-Z])?-\d+(?:\.\d+)?)\b")


class KnowledgeBase:
    """Chroma collection per pack, filled from the documents on first use."""

    def __init__(
        self,
        embedder: Embedder,
        model_name: str,
        persist_dir: str | Path,
        passages: Iterable[Passage] | None = None,
        min_score: float = 0.6,
        max_gap: float = 0.12,
    ):
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self.embed = embedder
        self.model_name = model_name
        self.min_score = min_score
        self.max_gap = max_gap  # drop hits much weaker than the best one
        self.passages = list(passages if passages is not None else load_passages())
        self.by_id = {p.id: p for p in self.passages}
        self.client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        self.collections = {}
        self.index_ms = 0.0
        self._build()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> KnowledgeBase:
        s = settings or get_settings()
        embedder, name = make_embedder(s)
        # Keyword-hash similarities run much lower than a real model's.
        min_score = 0.2 if s.kb_embedder == "hash" else s.kb_min_score
        return cls(embedder, name, s.kb_chroma_dir, min_score=min_score)

    def packs(self) -> list[str]:
        return sorted({p.pack for p in self.passages})

    def _fingerprint(self, passages: list[Passage]) -> str:
        h = hashlib.sha256(self.model_name.encode())
        for p in passages:
            h.update(repr((p.id, p.title, p.text, p.source)).encode())
        return h.hexdigest()[:16]

    def _build(self) -> None:
        t0 = time.perf_counter()
        for pack in self.packs():
            items = [p for p in self.passages if p.pack == pack]
            name = f"kb_{pack}"
            fp = self._fingerprint(items)
            col = self.client.get_or_create_collection(
                name, metadata={"hnsw:space": "cosine", "fingerprint": fp}
            )
            if col.metadata.get("fingerprint") != fp or col.count() != len(items):
                log.info("Indexing %d passages for %s", len(items), pack)
                self.client.delete_collection(name)
                col = self.client.create_collection(
                    name, metadata={"hnsw:space": "cosine", "fingerprint": fp}
                )
                col.add(
                    ids=[p.id for p in items],
                    documents=[p.text for p in items],
                    metadatas=[p.metadata() for p in items],
                    embeddings=self.embed([p.embed_text() for p in items], False),
                )
            self.collections[pack] = col
        self.index_ms = (time.perf_counter() - t0) * 1000

    def search(self, pack: str, query: str, k: int = 4) -> list[Hit]:
        """Most relevant passages for a question, best first. A clause id named in
        the query (e.g. "what does LT-6.1 say") is returned first."""
        col = self.collections.get(pack)
        if col is None:
            raise KeyError(f"no knowledge base for pack {pack!r}")
        hits: list[Hit] = []
        for cid in _CLAUSE_ID.findall(query):
            p = self.by_id.get(cid)
            if p and p.pack == pack:
                hits.append(self._hit(p, 1.0))
        res = col.query(
            query_embeddings=self.embed([query], True),
            n_results=min(k, col.count()),
        )
        best = None
        for pid, dist in zip(res["ids"][0], res["distances"][0], strict=True):
            score = 1.0 - dist
            best = score if best is None else best
            if score < self.min_score or score < best - self.max_gap:
                break
            if all(h.id != pid for h in hits):
                hits.append(self._hit(self.by_id[pid], score))
        return hits[:k]

    def _hit(self, p: Passage, score: float) -> Hit:
        return Hit(p.id, p.title, p.text, p.source, p.document, score, p.url)


_default: KnowledgeBase | None = None
_lock = threading.Lock()


def get_knowledge_base() -> KnowledgeBase:
    """Process-wide knowledge base, built on first use."""
    global _default
    with _lock:
        if _default is None:
            _default = KnowledgeBase.from_settings()
        return _default
