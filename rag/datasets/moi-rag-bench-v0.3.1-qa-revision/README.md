# MOI RAG Benchmark v0.3.1 QA revision

This is a QA-only revision of v0.3-final. `ready_for_eval/corpus.jsonl` and all 297 Markdown documents are byte-identical to the parent package.

- Documents: 297 (frozen)
- QA / Gold: 275 / 275
- Added QA: 33; removed QA: 33
- Original question-bank reuse was checked before source-grounded construction.
- Existing embeddings/indexes may be reused because corpus bytes did not change. QA generation and Judge scoring must be rerun.
