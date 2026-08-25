from __future__ import annotations

import bisect
import hashlib
import json
import math
import time
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .defaults import cosine_similarity

logger = logging.getLogger(__name__)


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
    def build(
        self,
        dataset_id: str,
        encoder_fingerprint: str,
        evidence: dict[str, tuple[list[float], list[float]]],
        alpha: float = 1.0,
    ) -> CalibrationProfile:
        dataset_fingerprint = hashlib.sha256(dataset_id.encode()).hexdigest()
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

    def bootstrap(self, repository: Any, encoder: Any, alpha: float = 1.0) -> CalibrationProfile:
        samples = repository.list_all()
        dataset_fingerprint = repository.dataset_fingerprint()
        version = hashlib.sha256(
            f"{dataset_fingerprint}:{encoder.fingerprint}:quantile-laplace-v1".encode()
        ).hexdigest()[:16]
        if len({str(sample.sample_id) for sample in samples}) < 2:
            logger.warning(
                "Insufficient scenarios for calibration bootstrap; using neutral profile"
            )
            return CalibrationProfile(
                version, dataset_fingerprint, encoder.fingerprint, smoothing_alpha=alpha
            )
        vectors = encoder.encode_documents([sample.mocked_query for sample in samples])
        semantic_positive = [cosine_similarity(vector, vector) for vector in vectors]
        semantic_negative = []
        for index, sample in enumerate(samples):
            different = [
                (cosine_similarity(vectors[index], vector), other)
                for vector, other in zip(vectors, samples, strict=True)
                if str(other.sample_id) != str(sample.sample_id)
            ]
            if different:
                semantic_negative.append(max(different, key=lambda item: item[0])[0])
        contexts = [repository.get(str(sample.sample_id)) for sample in samples]
        context_vectors = [
            next(
                position.context.embedding
                for position in scenario.positions
                if position.position == sample.position_id
            )
            for scenario, sample in zip(contexts, samples, strict=True)
            if scenario is not None
        ]
        context_positive = [cosine_similarity(vector, vector) for vector in context_vectors]
        context_negative = []
        for index, sample in enumerate(samples[: len(context_vectors)]):
            different = [
                cosine_similarity(context_vectors[index], vector)
                for vector, other in zip(context_vectors, samples, strict=False)
                if str(other.sample_id) != str(sample.sample_id)
            ]
            if different:
                context_negative.append(max(different))
        distributions = {
            "semantic": fit_distribution(semantic_positive, semantic_negative, alpha=alpha),
            "context": fit_distribution(context_positive, context_negative, alpha=alpha),
        }
        return CalibrationProfile(
            version,
            dataset_fingerprint,
            encoder.fingerprint,
            distributions=distributions,
            smoothing_alpha=alpha,
        )

    def load_or_bootstrap(
        self, repository: Any, encoder: Any, alpha: float = 1.0
    ) -> CalibrationProfile:
        fingerprint = repository.dataset_fingerprint()
        existing = repository.find_compatible_profile(
            fingerprint, encoder.fingerprint, "quantile-laplace-v1"
        )
        if existing is not None:
            return existing
        profile = self.bootstrap(repository, encoder, alpha)
        repository.save_calibration_profile(profile)
        return profile
