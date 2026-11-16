"""离线检索基准：小规模可直接运行，完整 500/10k 规模需真实 embedding。"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

# 允许直接以 ``python scripts/bench_retrieval.py`` 运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kernel.retrieval import DeterministicEmbedding, LocalVectorStore, RetrievalService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=int, default=100)
    parser.add_argument("--queries", type=int, default=20)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        service = RetrievalService(LocalVectorStore(Path(directory) / "bench.json", DeterministicEmbedding()))
        for i in range(args.documents):
            service.index_document(content=f"document {i} topic-{i % 10} UH retrieval", source=f"doc-{i}.md", title=f"doc-{i}")
        latencies = []
        hits = 0
        for i in range(args.queries):
            query = f"topic-{i % 10}"
            start = time.perf_counter()
            result = service.search(query, top_k=5)
            latencies.append((time.perf_counter() - start) * 1000)
            if result["docs"] and any(f"topic-{i % 10}" in d["snippet"] for d in result["docs"]):
                hits += 1
        latencies.sort()
        p95 = latencies[min(len(latencies) - 1, max(0, int(len(latencies) * 0.95) - 1))] if latencies else 0
        print({"embedding": "deterministic-hash", "documents": args.documents, "queries": args.queries, "recall": hits / max(1, args.queries), "p95_ms": round(p95, 3), "mean_ms": round(statistics.mean(latencies), 3) if latencies else 0})


if __name__ == "__main__":
    main()
