"""Mapper: discretizer -> CountVectorizer -> sklearn LatentDirichletAllocation (batch variational Bayes).

Deviation from the paper (recorded in the lock): the paper trains LDA with collapsed Gibbs sampling (1024 sweeps);
sklearn implements online/batch variational Bayes. We use ``learning_method="batch"``, alpha(doc_topic_prior)=0.1,
beta(topic_word_prior)=0.01, fixed ``random_state``. Vocabulary and LDA are fitted on the TRAIN split only.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
from sklearn.decomposition import LatentDirichletAllocation
from sklearn.feature_extraction.text import CountVectorizer

from .features import TOKEN_PATTERN, AttributeDiscretizer

DOC_TOPIC_PRIOR = 0.1
TOPIC_WORD_PRIOR = 0.01
MAX_ITER = 100


class Mapper:
    def __init__(self, n_topics: int, seed: int):
        self.n_topics, self.seed = n_topics, seed
        self.discretizer = AttributeDiscretizer()
        self.vectorizer: CountVectorizer | None = None
        self.lda: LatentDirichletAllocation | None = None
        self.fit_row_count = 0

    def fit(self, train_features: list[dict]) -> "Mapper":
        if not train_features:
            raise ValueError("cannot fit the Mapper on zero training rows")
        self.discretizer.fit(train_features)
        docs = [self.discretizer.document(f) for f in train_features]
        self.vectorizer = CountVectorizer(lowercase=False, token_pattern=TOKEN_PATTERN)
        matrix = self.vectorizer.fit_transform(docs)          # vocabulary comes from TRAIN documents only
        if matrix.shape[1] == 0:
            raise ValueError("empty vocabulary: every training document is empty")
        self.lda = LatentDirichletAllocation(
            n_components=self.n_topics, doc_topic_prior=DOC_TOPIC_PRIOR, topic_word_prior=TOPIC_WORD_PRIOR,
            learning_method="batch", max_iter=MAX_ITER, random_state=self.seed, n_jobs=1)
        self.lda.fit(matrix)
        self.fit_row_count = len(train_features)
        return self

    def transform(self, rows_features: list[dict]) -> np.ndarray:
        docs = [self.discretizer.document(f) for f in rows_features]
        return self.lda.transform(self.vectorizer.transform(docs))

    def perplexity(self, rows_features: list[dict]) -> float:
        docs = [self.discretizer.document(f) for f in rows_features]
        return float(self.lda.perplexity(self.vectorizer.transform(docs)))

    @property
    def vocabulary(self) -> list[str]:
        return sorted(self.vectorizer.vocabulary_, key=self.vectorizer.vocabulary_.get)

    def vocabulary_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.vocabulary).encode()).hexdigest()

    def content_digest(self) -> str:
        h = hashlib.sha256()
        h.update(json.dumps(self.discretizer.state(), sort_keys=True).encode())
        h.update(json.dumps(self.vocabulary).encode())
        h.update(np.ascontiguousarray(self.lda.components_, dtype="<f8").tobytes())
        return h.hexdigest()
