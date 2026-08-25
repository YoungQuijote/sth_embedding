from __future__ import annotations

import bisect
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field


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


def fit_distribution(positive: list[float], negative: list[float], bin_count: int = 10, alpha: float = 1.0) -> CalibratedDistribution:
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
    positive_mass = [(count + alpha) / (len(positive) + alpha * bucket_count) for count in positive_counts]
    negative_mass = [(count + alpha) / (len(negative) + alpha * bucket_count) for count in negative_counts]
    return CalibratedDistribution(boundaries, positive_mass, negative_mass)


class CalibrationBootstrapper:
    def build(self, dataset_id: str, encoder_fingerprint: str, evidence: dict[str, tuple[list[float], list[float]]], alpha: float = 1.0) -> CalibrationProfile:
        dataset_fingerprint = hashlib.sha256(dataset_id.encode()).hexdigest()
        version = hashlib.sha256(f"{dataset_fingerprint}:{encoder_fingerprint}:quantile-laplace-v1".encode()).hexdigest()[:16]
        return CalibrationProfile(version, dataset_fingerprint, encoder_fingerprint, distributions={name: fit_distribution(pos, neg, alpha=alpha) for name, (pos, neg) in evidence.items()}, smoothing_alpha=alpha)
