from __future__ import annotations

import bisect
import hashlib
import json
import math
import random
import time
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .bm25 import DefaultTokenizer, EndpointBM25Index
from .defaults import cosine_similarity
from .domain import CalibrationSample, FeatureRelation, MockSample

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StaticCalibrationCorpusProvider:
    samples: Sequence[CalibrationSample]

    def load(self) -> Sequence[CalibrationSample]:
        return self.samples


@dataclass(slots=True)
class CalibratedDistribution:
    bins: list[float] = field(default_factory=list)
    positive: list[float] = field(default_factory=list)
    negative: list[float] = field(default_factory=list)

    def llr(self, score: float) -> float:
        if not self.positive or not self.negative:
            return 0.0
        index = min(bisect.bisect_left(self.bins, score), len(self.positive) - 1)
        return math.log(self.positive[index] / self.negative[index])


@dataclass(slots=True)
class CalibrationProfile:
    version: str
    dataset_fingerprint: str
    encoder_fingerprint: str
    algorithm_version: str = "quantile-laplace-v1"
    distributions: dict[str, CalibratedDistribution] = field(default_factory=dict)
    smoothing_alpha: float = 1.0
    created_at: float = field(default_factory=time.time)
    compatibility_fingerprint: str = ""
    evidence_status: dict[str, str] = field(default_factory=dict)

    def llr(self, evidence: str, score: float) -> float:
        distribution = self.distributions.get(evidence)
        return distribution.llr(score) if distribution else 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "CalibrationProfile":
        payload = json.loads(raw)
        payload["distributions"] = {
            name: CalibratedDistribution(**value)
            for name, value in payload.get("distributions", {}).items()
        }
        return cls(**payload)


def fit_distribution(
    positive: list[float], negative: list[float], bin_count: int = 10, alpha: float = 1.0
) -> CalibratedDistribution:
    """Fit empirical quantile bins and Laplace-smoothed class-conditional masses."""
    if not positive or not negative:
        return CalibratedDistribution()
    values = sorted(positive + negative)
    actual_bins = min(bin_count, len(values))
    boundaries = []
    for index in range(1, actual_bins):
        boundary = values[min(len(values) - 1, math.ceil(index * len(values) / actual_bins) - 1)]
        if not boundaries or boundary > boundaries[-1]:
            boundaries.append(boundary)
    bucket_count = len(boundaries) + 1
    positive_counts = [0] * bucket_count
    negative_counts = [0] * bucket_count
    for value in positive:
        positive_counts[bisect.bisect_left(boundaries, value)] += 1
    for value in negative:
        negative_counts[bisect.bisect_left(boundaries, value)] += 1
    positive_mass = [
        (count + alpha) / (len(positive) + alpha * bucket_count) for count in positive_counts
    ]
    negative_mass = [
        (count + alpha) / (len(negative) + alpha * bucket_count) for count in negative_counts
    ]
    return CalibratedDistribution(boundaries, positive_mass, negative_mass)


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    enabled_evidences: frozenset[str] = frozenset({"semantic"})
    minimum_positive: int = 1
    minimum_negative: int = 1
    algorithm_version: str = "multi-evidence-v1"

    def __post_init__(self) -> None:
        supported = {"semantic", "context", "bm25", "feature"}
        unknown = self.enabled_evidences - supported
        if unknown:
            raise ValueError(f"unsupported calibration evidences: {sorted(unknown)}")
        if self.minimum_positive < 1 or self.minimum_negative < 1:
            raise ValueError("calibration minimum sample counts must be positive")


class EvidenceCalibrator(Protocol):
    name: str
    version: str

    def collect(
        self, samples: Sequence[CalibrationSample], **kwargs: Any
    ) -> tuple[list[float], list[float]]: ...


class SemanticEvidenceCalibrator:
    name = "semantic"
    version = "scenario-max-loo-v1"

    def __init__(self, bootstrapper: "CalibrationBootstrapper") -> None:
        self.bootstrapper = bootstrapper

    def collect(self, samples: Sequence[CalibrationSample], **kwargs: Any):
        return self.bootstrapper.semantic_scores(samples, kwargs["encoder"])


class ContextEvidenceCalibrator:
    name = "context"
    version = "runtime-probes-required-v1"

    def collect(self, samples: Sequence[CalibrationSample], **kwargs: Any):
        # Preset contexts cannot faithfully simulate runtime lane history.
        return [], []


class BM25EvidenceCalibrator:
    name = "bm25"
    version = "scenario-max-loo-v1"

    def __init__(self, bootstrapper: "CalibrationBootstrapper") -> None:
        self.bootstrapper = bootstrapper

    def collect(self, samples: Sequence[CalibrationSample], **kwargs: Any):
        tokenizer = kwargs.get("tokenizer") or DefaultTokenizer()
        k1 = kwargs.get("bm25_k1", 1.5)
        b = kwargs.get("bm25_b", 0.75)
        grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
        by_endpoint: dict[str, list[int]] = defaultdict(list)
        mock_samples = []
        for index, sample in enumerate(samples):
            grouped[(sample.endpoint_id, sample.scenario_id)].append(index)
            by_endpoint[sample.endpoint_id].append(index)
            mock_samples.append(
                MockSample(
                    sample.endpoint_id,
                    sample.mocked_query,
                    sample.mocked_answer,
                    sample.scenario_id,
                    sample.round_id,
                    sample.position_id,
                    sample.features,
                    f"calibration-{index}",
                )
            )
        positive: list[float] = []
        negative: list[float] = []
        rng = random.Random(self.bootstrapper.random_seed)
        for endpoint_id, indexes in by_endpoint.items():
            index = EndpointBM25Index(tokenizer, k1, b)
            index.refresh_if_changed(mock_samples[item] for item in indexes)
            for query_index in indexes:
                query_sample = samples[query_index]
                scores = {
                    candidate.sample_hash: score
                    for candidate, score in index.recall(query_sample.mocked_query, len(indexes))
                    if candidate.sample_hash != f"calibration-{query_index}"
                }
                true_indexes = [
                    item
                    for item in grouped[(endpoint_id, query_sample.scenario_id)]
                    if item != query_index and f"calibration-{item}" in scores
                ]
                if true_indexes:
                    positive.append(max(scores[f"calibration-{item}"] for item in true_indexes))
                incorrect = []
                for (candidate_endpoint, scenario_id), scenario_indexes in grouped.items():
                    if candidate_endpoint != endpoint_id or scenario_id == query_sample.scenario_id:
                        continue
                    values = [
                        scores[f"calibration-{item}"]
                        for item in scenario_indexes
                        if f"calibration-{item}" in scores
                    ]
                    if values:
                        incorrect.append(max(values))
                incorrect.sort(reverse=True)
                negative.extend(incorrect[: self.bootstrapper.hard_negative_k])
                remaining = incorrect[self.bootstrapper.hard_negative_k :]
                negative.extend(
                    rng.sample(
                        remaining,
                        min(self.bootstrapper.easy_negative_k, len(remaining)),
                    )
                    if remaining
                    else []
                )
        return positive, negative


class FeatureEvidenceCalibrator:
    name = "feature"
    version = "labelled-scenario-pairs-v1"

    def collect(self, samples: Sequence[CalibrationSample], **kwargs: Any):
        comparator = kwargs.get("feature_comparator")
        if comparator is None:
            return [], []
        positive: list[float] = []
        negative: list[float] = []
        for query in samples:
            for candidate in samples:
                if query is candidate or query.endpoint_id != candidate.endpoint_id:
                    continue
                relation, score = comparator.compare(query.features, candidate.features)
                if relation in {FeatureRelation.CONFLICT, FeatureRelation.UNKNOWN}:
                    continue
                target = positive if query.scenario_id == candidate.scenario_id else negative
                target.append(score)
        # No distinction means no reliable distribution.
        if positive and negative and set(positive) == set(negative):
            return [], []
        return positive, negative


class CalibrationBootstrapper:
    algorithm_version = "scenario-max-loo-v1"

    def __init__(
        self,
        hard_negative_k: int = 3,
        easy_negative_k: int = 3,
        random_seed: int = 42,
        calibrators: dict[str, EvidenceCalibrator] | None = None,
    ) -> None:
        self.hard_negative_k = hard_negative_k
        self.easy_negative_k = easy_negative_k
        self.random_seed = random_seed
        self.calibrators = {
            "semantic": SemanticEvidenceCalibrator(self),
            "context": ContextEvidenceCalibrator(),
            "bm25": BM25EvidenceCalibrator(self),
            "feature": FeatureEvidenceCalibrator(),
            **(calibrators or {}),
        }

    def fit_profile(
        self,
        dataset_fingerprint: str,
        encoder_fingerprint: str,
        evidence: dict[str, tuple[list[float], list[float]]],
        alpha: float = 1.0,
    ) -> CalibrationProfile:
        """Fit pre-labelled evidence; fingerprint must describe the actual corpus."""
        version = hashlib.sha256(
            f"{dataset_fingerprint}:{encoder_fingerprint}:quantile-laplace-v1".encode()
        ).hexdigest()[:16]
        return CalibrationProfile(
            version,
            dataset_fingerprint,
            encoder_fingerprint,
            distributions={
                name: fit_distribution(pos, neg, alpha=alpha)
                for name, (pos, neg) in evidence.items()
            },
            smoothing_alpha=alpha,
        )

    @staticmethod
    def corpus_fingerprint(samples: Sequence[CalibrationSample]) -> str:
        records = [
            {
                "endpoint_id": sample.endpoint_id,
                "scenario_id": sample.scenario_id,
                "round_id": str(sample.round_id),
                "position_id": sample.position_id,
                "mocked_query": sample.mocked_query,
                "mocked_answer": sample.mocked_answer,
                "features": {
                    "hard": sample.features.hard_features,
                    "soft": sample.features.soft_features,
                },
            }
            for sample in samples
        ]
        records.sort(
            key=lambda item: (
                item["endpoint_id"],
                item["scenario_id"],
                item["round_id"],
                item["position_id"],
            )
        )
        raw = json.dumps(
            records, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def semantic_scores(
        self, samples: Sequence[CalibrationSample], encoder: Any
    ) -> tuple[list[float], list[float]]:
        """Simulate sample-to-scenario retrieval using leave-one-out scenario maxima."""
        vectors = encoder.encode_documents([sample.mocked_query for sample in samples])
        grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
        for index, sample in enumerate(samples):
            grouped[(sample.endpoint_id, sample.scenario_id)].append(index)
        positive: list[float] = []
        negative: list[float] = []
        rng = random.Random(self.random_seed)
        for query_index, query_sample in enumerate(samples):
            true_key = (query_sample.endpoint_id, query_sample.scenario_id)
            same_scenario = [index for index in grouped[true_key] if index != query_index]
            if same_scenario:
                positive.append(
                    max(
                        cosine_similarity(vectors[query_index], vectors[index])
                        for index in same_scenario
                    )
                )
            incorrect_scores = []
            for (endpoint_id, scenario_id), indexes in grouped.items():
                if (
                    endpoint_id != query_sample.endpoint_id
                    or scenario_id == query_sample.scenario_id
                ):
                    continue
                incorrect_scores.append(
                    max(
                        cosine_similarity(vectors[query_index], vectors[index]) for index in indexes
                    )
                )
            incorrect_scores.sort(reverse=True)
            negative.extend(incorrect_scores[: self.hard_negative_k])
            remaining = incorrect_scores[self.hard_negative_k :]
            if remaining:
                negative.extend(rng.sample(remaining, min(self.easy_negative_k, len(remaining))))
        # One global exact-match anchor, not one self-similarity per sample.
        positive.append(1.0)
        return positive, negative

    def _compatibility(
        self,
        config: CalibrationConfig,
        encoder: Any,
        tokenizer: Any | None,
        feature_comparator: Any | None,
        bm25_k1: float,
        bm25_b: float,
    ) -> tuple[str, str]:
        if config == CalibrationConfig():
            return self.algorithm_version, "semantic-only-v1"
        details = {
            "config": asdict(config),
            "calibrators": {
                name: self.calibrators[name].version for name in sorted(config.enabled_evidences)
            },
            "encoder": encoder.fingerprint,
            "tokenizer": getattr(tokenizer, "fingerprint", None),
            "bm25": [bm25_k1, bm25_b],
            "feature_comparator": getattr(feature_comparator, "fingerprint", None),
        }
        raw = json.dumps(details, sort_keys=True, default=sorted)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        return f"{config.algorithm_version}:{fingerprint[:16]}", fingerprint

    def bootstrap(
        self,
        samples: Sequence[CalibrationSample],
        encoder: Any,
        alpha: float = 1.0,
        *,
        config: CalibrationConfig | None = None,
        tokenizer: Any | None = None,
        feature_comparator: Any | None = None,
        bm25_k1: float = 1.5,
        bm25_b: float = 0.75,
    ) -> CalibrationProfile:
        config = config or CalibrationConfig()
        dataset_fingerprint = self.corpus_fingerprint(samples)
        algorithm_version, compatibility = self._compatibility(
            config, encoder, tokenizer, feature_comparator, bm25_k1, bm25_b
        )
        version = hashlib.sha256(
            f"{dataset_fingerprint}:{encoder.fingerprint}:{algorithm_version}".encode()
        ).hexdigest()[:16]
        distributions = {}
        status = {}
        for evidence_name in sorted(config.enabled_evidences):
            positive, negative = self.calibrators[evidence_name].collect(
                samples,
                encoder=encoder,
                tokenizer=tokenizer,
                feature_comparator=feature_comparator,
                bm25_k1=bm25_k1,
                bm25_b=bm25_b,
            )
            if len(positive) < config.minimum_positive or len(negative) < config.minimum_negative:
                status[evidence_name] = (
                    f"unavailable: positive={len(positive)}, negative={len(negative)}"
                )
                logger.warning("Insufficient %s calibration data; using neutral LLR", evidence_name)
                continue
            distributions[evidence_name] = fit_distribution(positive, negative, alpha=alpha)
            status[evidence_name] = f"available: positive={len(positive)}, negative={len(negative)}"
        return CalibrationProfile(
            version,
            dataset_fingerprint,
            encoder.fingerprint,
            algorithm_version=algorithm_version,
            distributions=distributions,
            smoothing_alpha=alpha,
            compatibility_fingerprint=compatibility,
            evidence_status=status,
        )

    def load_or_bootstrap(
        self,
        repository: Any,
        encoder: Any,
        corpus_provider: Any,
        alpha: float = 1.0,
        **kwargs: Any,
    ) -> CalibrationProfile:
        samples = list(corpus_provider.load())
        fingerprint = self.corpus_fingerprint(samples)
        config = kwargs.get("config") or CalibrationConfig()
        algorithm_version, _ = self._compatibility(
            config,
            encoder,
            kwargs.get("tokenizer"),
            kwargs.get("feature_comparator"),
            kwargs.get("bm25_k1", 1.5),
            kwargs.get("bm25_b", 0.75),
        )
        existing = repository.find_compatible_profile(
            fingerprint, encoder.fingerprint, algorithm_version
        )
        if existing is not None:
            return existing
        profile = self.bootstrap(samples, encoder, alpha, **kwargs)
        repository.save_calibration_profile(profile)
        return profile


def neutral_profile(encoder_fingerprint: str) -> CalibrationProfile:
    version = hashlib.sha256(f"neutral:{encoder_fingerprint}".encode()).hexdigest()[:16]
    return CalibrationProfile(version, "no-calibration-corpus", encoder_fingerprint)
