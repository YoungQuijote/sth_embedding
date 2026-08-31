from __future__ import annotations

import hashlib
import ipaddress
import math
import logging
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Mapping

from .domain import ContextMessage, FeatureRelation, FeatureSet, RequestAffinityInfo

logger = logging.getLogger(__name__)


class DefaultQueryParser:
    """Parse a string or a common JSON request shape without business assumptions."""

    def parse(self, body: Any) -> str:
        if isinstance(body, str):
            return body
        if isinstance(body, Mapping):
            for key in ("query", "question", "content", "prompt", "input"):
                value = body.get(key)
                if isinstance(value, str):
                    return value
        raise ValueError("request body must be a string or contain a textual query field")


class HashingEncoder:
    """Dependency-free deterministic token hashing encoder suitable for tests and bootstrap."""

    name = "hashing-token-encoder"
    version = "1"
    fingerprint = "sha256-token-256-v1"

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = re.findall(r"[\w]+|[^\w\s]", text.casefold(), re.UNICODE)
        # Character trigrams preserve useful overlap for languages without whitespace.
        compact = re.sub(r"\s+", "", text.casefold())
        tokens.extend(compact[i : i + 3] for i in range(max(0, len(compact) - 2)))
        for token, count in Counter(tokens).items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * count
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def encode_query(self, text: str) -> list[float]:
        return self.encode(text)

    def encode_document(self, text: str) -> list[float]:
        return self.encode(text)

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.encode_document(text) for text in texts]


class SentenceTransformerEncoder:
    """Local SentenceTransformer adapter using asymmetric retrieval methods when available."""

    def __init__(self, model_path: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_path = str(Path(model_path).resolve())
        self.model = SentenceTransformer(self.model_path)
        self.name = "sentence-transformer"
        self.version = getattr(__import__("sentence_transformers"), "__version__", "unknown")
        self.fingerprint = hashlib.sha256(f"{self.model_path}:{self.version}".encode()).hexdigest()

    @staticmethod
    def _list(vector: Any) -> list[float]:
        return vector.tolist() if hasattr(vector, "tolist") else list(vector)

    def encode_query(self, text: str) -> list[float]:
        method = getattr(self.model, "encode_query", self.model.encode)
        return self._list(method(text, normalize_embeddings=True))

    def encode_document(self, text: str) -> list[float]:
        method = getattr(self.model, "encode_document", self.model.encode)
        return self._list(method(text, normalize_embeddings=True))

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        method = getattr(self.model, "encode_document", self.model.encode)
        vectors = method(list(texts), normalize_embeddings=True)
        return [self._list(vector) for vector in vectors]


def create_default_encoder(model_path: str | None) -> HashingEncoder | SentenceTransformerEncoder:
    if model_path is None:
        logger.warning("SENTENCE_TRANSFORMER_ENCODER is not configured; using HashingEncoder")
        return HashingEncoder()
    if not Path(model_path).is_dir():
        logger.warning(
            "SentenceTransformer path does not exist: %s; using HashingEncoder", model_path
        )
        return HashingEncoder()
    try:
        return SentenceTransformerEncoder(model_path)
    except Exception:
        logger.exception(
            "Unable to load SentenceTransformer from %s; using HashingEncoder", model_path
        )
        return HashingEncoder()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions differ")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


class EmptyFeatureExtractor:
    def extract(self, query: str) -> FeatureSet[dict[str, Any], dict[str, Any]]:
        return FeatureSet({}, {})


class DefaultFeatureComparator:
    """Only conflicting values of explicit, shared hard keys eliminate a candidate."""

    def compare(
        self, query: FeatureSet[Any, Any], candidate: FeatureSet[Any, Any]
    ) -> tuple[FeatureRelation, float]:
        query_hard = query.hard_features if isinstance(query.hard_features, Mapping) else {}
        candidate_hard = (
            candidate.hard_features if isinstance(candidate.hard_features, Mapping) else {}
        )
        shared = set(query_hard) & set(candidate_hard)
        if any(query_hard[key] != candidate_hard[key] for key in shared):
            return FeatureRelation.CONFLICT, -1.0
        if shared:
            return FeatureRelation.MATCH, 1.0
        return FeatureRelation.UNKNOWN, 0.0


class JoiningContextFusionProvider:
    def fuse(self, inputs: Sequence[ContextMessage]) -> str:
        return "\n\n".join(
            f"{'Question' if message.role == 'question' else 'Answer'}:\n{message.content.strip()}"
            for message in inputs
            if message.content.strip()
        )


class DefaultAffinityExtractor:
    def extract(self, headers: Mapping[str, str]) -> RequestAffinityInfo:
        normalized = {key.casefold(): value for key, value in headers.items()}
        forwarded = normalized.get("x-forwarded-for", "").split(",", 1)[0].strip()
        request_ip = forwarded or normalized.get("x-real-ip")
        authorization = normalized.get("authorization")
        auth_hash = hashlib.sha256(authorization.encode()).hexdigest() if authorization else None
        return RequestAffinityInfo(
            request_ip=request_ip,
            user_agent=normalized.get("user-agent"),
            auth_identity_hash=auth_hash,
            trace_header=normalized.get("traceparent") or normalized.get("x-trace-id"),
        )


def ipv4_affinity(query: RequestAffinityInfo, registered: list[RequestAffinityInfo]) -> float:
    if not query.request_ip:
        return 0.0
    try:
        query_ip = ipaddress.IPv4Address(query.request_ip)
    except ipaddress.AddressValueError:
        return 0.0
    best = 0.0
    for info in registered:
        if not info.request_ip:
            continue
        try:
            candidate_ip = ipaddress.IPv4Address(info.request_ip)
        except ipaddress.AddressValueError:
            continue
        xor = int(query_ip) ^ int(candidate_ip)
        prefix = 32 if xor == 0 else 32 - xor.bit_length()
        best = max(
            best,
            1.0
            if prefix == 32
            else 0.75
            if prefix >= 24
            else 0.5
            if prefix >= 16
            else 0.25
            if prefix >= 8
            else 0.0,
        )
    return best
