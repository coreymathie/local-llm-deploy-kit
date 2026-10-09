# Answer-quality evaluation

`python scripts/rag_eval.py --check` runs the gateway's real ingestion, access control and retrieval over the
console's own sample library (Cypress Harbor Credit Union, a fictional credit union: 59 documents in six
collections, 3 of them restricted) and fails if a metric drops below `thresholds.json`. CI runs it on every push,
and the console's Answer quality screen shows the committed results (`demo/data/rag_eval.json`).

## The golden set

`golden/questions.jsonl` holds 59 questions, one JSON object per line:

- `persona`: who asks, as defined in `demo/engine.py` (Priya in HR, Dana in IT and digital banking, Marcus in
  Compliance and BSA, Audrey in internal audit, and the lobby kiosk). Their groups and roles decide what they can
  read, exactly as in the console.
- `doc` and `answer_contains`: the document that answers the question and a phrase the answer must contain.
- `doc: null`: a question the assistant should decline, either because no document answers it or because the
  answer sits in a document the asker is not cleared for (for example, an engineer asking for a salary band, or
  the kiosk asking about teller cash limits).

The 50 answerable questions cover every collection, including all three restricted documents asked by someone
who may read them. The 9 decline questions include four restricted-document questions from personas without
access, two kiosk questions about internal procedures and three questions nothing in the library answers.

Questions are paraphrased the way staff and members ask ("Can I carry unused vacation days into January?"), not
copied from the documents, and were written after the library was finished. Neither the documents nor the
answerer were changed in response to eval results; a miss stays a miss. The library and the questions share one
author, so treat the numbers as a regression baseline for this pipeline, not a quality claim about a deployment.

## ACL leak check

Every question is also asked as every persona, and each retrieval (top 12 passages) is checked against what that
persona may read according to the catalog's collection and document access lists. The eval decides readability
itself (`rag_eval.readable`), independently of `gateway/identity.py`, so a regression in the gateway's access code
shows up as a leak. Any passage from a document the persona may not read counts; the gate is 0.

## Thresholds

Each minimum in `thresholds.json` is one point below the value measured when this golden set was introduced,
rounded down, so the gate catches regressions without failing on the current state. Raise a threshold when
retrieval or answering improves; never lower one to make a change pass. `max_acl_leaks` stays 0.

Decline accuracy is low (0.222 for hybrid retrieval, 0.111 for bm25). The console declines only when the best
passage's embedding similarity is below a fixed floor (`RELEVANCE_FLOOR` in `demo/engine.py`), so a question that
shares a few words with an unrelated document is answered from it instead of declined. In bm25-only mode there
is no embedding similarity, so the floor never applies. Those answers never contain restricted content (the leak
gate proves that); they are unhelpful, not unsafe. A real model told to answer only from the sources declines
more often; measure it against a deployment before relying on this number.

## What is measured

The embedder is the browser demo's hashed bag-of-words vector (deterministic, offline), not a neural embedding
model, and answers come from the demo's extractive answerer (no LLM) with the console's relevance floor. Results
with a real embedding model and LLM will differ; run the same questions against a deployment to measure them.

The previous golden set (12 generic policy documents and 43 questions written for an unrelated fictional
company) is kept in git history only; its numbers are no longer published.
