from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .contracts import Tokenizer
from .domain import BM25RecallCandidate, MockSample


class DefaultTokenizer:
    """Dependency-free tokenizer with word, CJK-character, and punctuation tokens."""

    fingerprint = "default-regex-cjk-char-v1"

    def tokenize(self, text: str) -> list[str]:
        return re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]|[^\w\s]", text.casefold(), re.UNICODE)


@dataclass(slots=True)
class BM25Document:
    sample: MockSample
    term_frequency: Counter[str]
    length: int


class EndpointBM25Index:
    """Thread-safe endpoint-local BM25 corpus rebuilt only when its signature changes."""

    def __init__(self, tokenizer: Tokenizer, k1: float = 1.5, b: float = 0.75) -> None:
        if k1 <= 0:
            raise ValueError("bm25 k1 must be positive")
        if not 0 <= b <= 1:
            raise ValueError("bm25 b must be between 0 and 1")
        self.tokenizer = tokenizer
        self.k1 = k1
        self.b = b
        self.documents: list[BM25Document] = []
        self.idf: dict[str, float] = {}
        self.average_length = 0.0
        self._signature = ""
        self._lock = threading.RLock()

    def _corpus_signature(self, samples: list[MockSample]) -> str:
        payload = "|".join(
            [self.tokenizer.fingerprint, str(self.k1), str(self.b)]
            + [sample.sample_hash for sample in samples]
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._signature

    def refresh_if_changed(self, samples: Iterable[MockSample]) -> None:
        values = list(samples)
        signature = self._corpus_signature(values)
        with self._lock:
            if signature == self._signature:
                return
            documents = []
            document_frequency: Counter[str] = Counter()
            for sample in values:
                tokens = self.tokenizer.tokenize(sample.mocked_query)
                frequency = Counter(tokens)
                documents.append(BM25Document(sample, frequency, len(tokens)))
                document_frequency.update(frequency.keys())
            count = len(documents)
            self.documents = documents
            self.average_length = (
                sum(document.length for document in documents) / count if count else 0.0
            )
            self.idf = {
                token: math.log(1.0 + (count - frequency + 0.5) / (frequency + 0.5))
                for token, frequency in document_frequency.items()
            }
            self._signature = signature

    def score_tokens(self, query_tokens: list[str], document: BM25Document) -> float:
        if not document.length or not self.average_length:
            return 0.0
        score = 0.0
        for token in set(query_tokens):
            frequency = document.term_frequency.get(token, 0)
            if not frequency:
                continue
            denominator = frequency + self.k1 * (
                1.0 - self.b + self.b * document.length / self.average_length
            )
            score += self.idf.get(token, 0.0) * frequency * (self.k1 + 1.0) / denominator
        return score

    def recall(self, query: str, k: int) -> list[tuple[MockSample, float]]:
        tokens = self.tokenizer.tokenize(query)
        with self._lock:
            scored = [
                (document.sample, self.score_tokens(tokens, document))
                for document in self.documents
            ]
        return sorted(
            (item for item in scored if item[1] > 0.0),
            key=lambda item: (-item[1], item[0].sample_hash),
        )[:k]


class JiebaTokenizer:
    """Optional jieba adapter with dictionary content included in its fingerprint."""

    def __init__(self, user_dictionaries: Iterable[str | Path] = ()) -> None:
        import jieba

        self._jieba = jieba
        digest = hashlib.sha256(b"jieba-tokenizer-v1")
        for dictionary in user_dictionaries:
            path = Path(dictionary)
            data = path.read_bytes()
            digest.update(str(path.resolve()).encode())
            digest.update(data)
            jieba.load_userdict(str(path))
        self.fingerprint = digest.hexdigest()

    def tokenize(self, text: str) -> list[str]:
        return [token.strip().casefold() for token in self._jieba.cut(text) if token.strip()]


class BM25Recaller:
    def __init__(self, tokenizer: Tokenizer, k1: float = 1.5, b: float = 0.75) -> None:
        self.tokenizer = tokenizer
        self.k1 = k1
        self.b = b
        self.indexes: dict[str, EndpointBM25Index] = {}
        self._lock = threading.RLock()

    def recall(
        self, query: str, samples: Iterable[MockSample], k: int
    ) -> list[BM25RecallCandidate]:
        values = list(samples)
        endpoint_id = values[0].endpoint_id if values else ""
        with self._lock:
            index = self.indexes.get(endpoint_id)
            if index is None or index.tokenizer.fingerprint != self.tokenizer.fingerprint:
                index = EndpointBM25Index(self.tokenizer, self.k1, self.b)
                self.indexes[endpoint_id] = index
        index.refresh_if_changed(values)
        return [
            BM25RecallCandidate(
                sample.sample_hash,
                str(sample.sample_id),
                sample.position_id,
                score,
                rank,
                sample,
            )
            for rank, (sample, score) in enumerate(index.recall(query, k), start=1)
        ]
