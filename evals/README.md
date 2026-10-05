# RAG evaluation

`python scripts/rag_eval.py --check` runs the gateway's real ingestion, access control and retrieval
over `golden/` and fails if a metric drops below `thresholds.json`. CI runs it on every push.

- `golden/docs/`: 12 fictional policy documents (no real organizations or people).
- `golden/questions.jsonl`: 43 questions, each with the document that answers it and a phrase the answer
  must contain. Questions and documents were written by the same author, so they share vocabulary more
  than real users' questions would; treat the numbers as a regression baseline, not a quality claim.
- `golden/acl.json`: two documents restricted to `group:hr` and `group:security`. Every question is also
  asked by a caller without those groups; any retrieved restricted passage counts as a leak (must be 0).

The embedder is the browser demo's hashed bag-of-words vector (deterministic, offline), not a neural
embedding model, and answers come from the demo's extractive answerer (no LLM). Results with a real
embedding model and LLM will differ; run the same questions against your deployment to measure them.
