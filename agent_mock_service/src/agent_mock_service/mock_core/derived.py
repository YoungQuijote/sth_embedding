"""Versioned document embeddings and feature views; inference outside SQL transactions."""

from __future__ import annotations

import json
from dataclasses import replace

from .domain import FeatureSet
from .materialization import component_fingerprint, valid_vector


class DerivedStore:
    def __init__(self, repository, resources=None):
        self.repository = repository
        self.resources = resources

    def document_vectors(self, samples, encoder, metrics=None):
        fp = component_fingerprint(encoder)
        vectors, missing = {}, []
        with self.repository._read():
            for sample in samples:
                row = self.repository._connection.execute(
                    "SELECT * FROM sample_embeddings WHERE sample_hash=? AND encoder=?",
                    (sample.sample_hash, fp),
                ).fetchone()
                vector = (
                    valid_vector(
                        row["vector"], row["dimension"], getattr(encoder, "dimensions", None)
                    )
                    if row and row["format_version"] == 1
                    else None
                )
                if vector is None:
                    missing.append(sample)
                else:
                    vectors[sample.sample_hash] = vector
        if missing:
            if metrics is not None:
                metrics["sample_document_encode"] = metrics.get("sample_document_encode", 0) + len(
                    missing
                )
            texts = [s.mocked_query for s in missing]
            values = (
                self.resources.call_component(encoder, encoder.encode_documents, texts)
                if self.resources
                else encoder.encode_documents(texts)
            )
            with self.repository._write():
                if encoder.fingerprint != fp:
                    return {}
                for sample, vector in zip(missing, values, strict=True):
                    current = self.repository.get_by_hash(sample.sample_hash)
                    if current is None:
                        continue
                    if (
                        valid_vector(
                            json.dumps(vector), len(vector), getattr(encoder, "dimensions", None)
                        )
                        is None
                    ):
                        raise ValueError("invalid sample embedding")
                    self.repository._connection.execute(
                        "INSERT OR REPLACE INTO sample_embeddings(sample_hash,encoder,vector,dimension,format_version) VALUES(?,?,?,?,1)",
                        (sample.sample_hash, fp, json.dumps(vector), len(vector)),
                    )
                    vectors[sample.sample_hash] = vector
        return vectors

    def feature_views(self, samples, extractor):
        fp = component_fingerprint(extractor)
        result = []
        for sample in samples:
            with self.repository._read():
                row = self.repository._connection.execute(
                    "SELECT features FROM sample_features WHERE sample_hash=? AND extractor=?",
                    (sample.sample_hash, fp),
                ).fetchone()
            if row:
                payload = json.loads(row[0])
                features = FeatureSet(payload["hard"], payload["soft"])
            else:
                features = (
                    self.resources.call_component(extractor, extractor.extract, sample.mocked_query)
                    if self.resources
                    else extractor.extract(sample.mocked_query)
                )
                with self.repository._write():
                    if extractor.fingerprint != fp:
                        raise ValueError("feature extractor changed during extraction; retry")
                    if self.repository.get_by_hash(sample.sample_hash) is None:
                        continue
                    self.repository._save_feature_locked(sample, fp, features)
                    payload = json.loads(
                        self.repository._connection.execute(
                            "SELECT features FROM sample_features WHERE sample_hash=? AND extractor=?",
                            (sample.sample_hash, fp),
                        ).fetchone()[0]
                    )
                    features = FeatureSet(payload["hard"], payload["soft"])
            result.append(replace(sample, features=features))
        return result
