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
from typing import Any

from .defaults import cosine_similarity
from .domain import CalibrationSample

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


class CalibrationBootstrapper:
    algorithm_version = "scenario-max-loo-v1"

    def __init__(
        self,
        hard_negative_k: int = 3,
        easy_negative_k: int = 3,
        random_seed: int = 42,
    ) -> None:
        self.hard_negative_k = hard_negative_k
        self.easy_negative_k = easy_negative_k
        self.random_seed = random_seed

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

    def bootstrap(
        self, samples: Sequence[CalibrationSample], encoder: Any, alpha: float = 1.0
    ) -> CalibrationProfile:
        dataset_fingerprint = self.corpus_fingerprint(samples)
        version = hashlib.sha256(
            f"{dataset_fingerprint}:{encoder.fingerprint}:{self.algorithm_version}".encode()
        ).hexdigest()[:16]
        semantic_positive, semantic_negative = self.semantic_scores(samples, encoder)
        if not semantic_negative:
            logger.warning(
                "Insufficient calibration corpus for semantic negatives; using neutral profile"
            )
            return CalibrationProfile(
                version,
                dataset_fingerprint,
                encoder.fingerprint,
                algorithm_version=self.algorithm_version,
                smoothing_alpha=alpha,
            )
        # Context remains uncalibrated until runtime-like probes are explicitly supplied.
        distributions = {
            "semantic": fit_distribution(semantic_positive, semantic_negative, alpha=alpha)
        }
        return CalibrationProfile(
            version,
            dataset_fingerprint,
            encoder.fingerprint,
            algorithm_version=self.algorithm_version,
            distributions=distributions,
            smoothing_alpha=alpha,
        )

    def load_or_bootstrap(
        self, repository: Any, encoder: Any, corpus_provider: Any, alpha: float = 1.0
    ) -> CalibrationProfile:
        samples = list(corpus_provider.load())
        fingerprint = self.corpus_fingerprint(samples)
        existing = repository.find_compatible_profile(
            fingerprint, encoder.fingerprint, self.algorithm_version
        )
        if existing is not None:
            return existing
        profile = self.bootstrap(samples, encoder, alpha)
        repository.save_calibration_profile(profile)
        return profile


def neutral_profile(encoder_fingerprint: str) -> CalibrationProfile:
    version = hashlib.sha256(f"neutral:{encoder_fingerprint}".encode()).hexdigest()[:16]
    return CalibrationProfile(version, "no-calibration-corpus", encoder_fingerprint)
