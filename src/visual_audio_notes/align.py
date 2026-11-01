from __future__ import annotations

from dataclasses import asdict
import time
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize as normalize_rows

from .core import Chunk, Unit, normalize


def lexical_scores(units: list[Unit], chunks: list[Chunk]) -> np.ndarray:
    texts = [normalize(u.text).lower() for u in units]
    queries = [normalize(c.text).lower() for c in chunks]
    # Character n-grams also work when Chinese has no spaces.
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5),
                                 sublinear_tf=True, max_features=60000)
    try:
        documents = vectorizer.fit_transform(texts)
    except ValueError:
        return np.zeros((len(chunks), len(units)), dtype=np.float32)
    return (normalize_rows(vectorizer.transform(queries)) @
            normalize_rows(documents).T).toarray().astype(np.float32)


def passages(text: str, size: int = 450, overlap: int = 80) -> list[str]:
    value = normalize(text)
    if not value:
        return [""]
    return [value[start:start + size]
            for start in range(0, len(value), size - overlap)]


def semantic_scores(units: list[Unit], chunks: list[Chunk], model_path: str) -> np.ndarray:
    import torch
    from sentence_transformers import SentenceTransformer
    torch.set_num_threads(8)
    model = SentenceTransformer(model_path, device="cpu", local_files_only=True)
    model.max_seq_length = 256
    documents: list[str] = []
    owners: list[int] = []
    for i, unit in enumerate(units):
        for passage in passages(unit.text):
            documents.append(passage)
            owners.append(i)
    document_vectors = model.encode(documents, normalize_embeddings=True,
                                     show_progress_bar=False, batch_size=16)
    queries: list[str] = []
    query_owners: list[int] = []
    for i, chunk in enumerate(chunks):
        for passage in passages(chunk.text):
            queries.append(passage)
            query_owners.append(i)
    query_vectors = model.encode(queries, normalize_embeddings=True,
                                  show_progress_bar=False, batch_size=16)
    matrix = query_vectors @ document_vectors.T
    # Average query passages; take the best supporting passage on each page.
    scores = np.zeros((len(chunks), len(units)), dtype=np.float32)
    owners = np.array(owners)
    query_owners = np.array(query_owners)
    for i in range(len(chunks)):
        for j in range(len(units)):
            scores[i, j] = matrix[query_owners == i][:, owners == j].max(axis=1).mean()
    return scores


def rank_scores(units: list[Unit], chunks: list[Chunk], scores: np.ndarray,
                config: dict, method: str) -> list[dict]:
    if scores.shape != (len(chunks), len(units)) or not np.isfinite(scores).all():
        raise ValueError("Invalid score matrix")
    results = []
    for chunk, row in zip(chunks, scores):
        ordered = sorted(range(len(units)), key=lambda j: (-float(row[j]), units[j].unit_id))
        best = ordered[0]
        margin = float(row[best] - row[ordered[1]]) if len(ordered) > 1 else float(row[best])
        duplicate = len(ordered) > 1 and (
            normalize(units[best].text).casefold() ==
            normalize(units[ordered[1]].text).casefold()
        )
        accepted = (float(row[best]) >= config["automatic_score_floor"] and
                    margin >= config["automatic_margin_floor"] and not duplicate)
        results.append({
            **asdict(chunk), "method": method, "status": "accepted" if accepted else "review",
            "unit_id": units[best].unit_id if accepted else None,
            "margin": round(margin, 6),
            "candidates": [{"unit_id": units[j].unit_id, "score": round(float(row[j]), 6)}
                           for j in ordered[:config["top_k"]]],
        })
    return results


def align(units: list[Unit], chunks: list[Chunk], config: dict,
          embedding_path: str | None = None) -> tuple[dict, dict]:
    # Canonicalize opaque IDs so import order cannot affect feature fitting or ties.
    units = sorted(units, key=lambda u: u.unit_id)
    started = time.perf_counter()
    lexical = lexical_scores(units, chunks)
    results = {"lexical": rank_scores(units, chunks, lexical, config, "lexical")}
    timing = {"lexical_seconds": round(time.perf_counter() - started, 3)}
    if embedding_path:
        started = time.perf_counter()
        semantic = semantic_scores(units, chunks, embedding_path)
        combined = (config["semantic_weight"] * semantic +
                    config["lexical_weight"] * lexical)
        results["hybrid"] = rank_scores(units, chunks, combined, config, "hybrid")
        timing["semantic_seconds"] = round(time.perf_counter() - started, 3)
    return results, timing
