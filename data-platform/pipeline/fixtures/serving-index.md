---
title: Serving Index
depends_on: [chunking-rules]
owner: platform
---

# Serving Index

Everything the agent reads at query time lives in a single SQLite file,
`data/serving/index.sqlite`. One file means one `VACUUM`, one snapshot, one
upload, and no chance of the vector store and the keyword store disagreeing
about which documents exist.

## Tables

`chunks` holds the text, `chunks_vec` is a sqlite-vec virtual table holding the
embeddings, `chunks_fts` is an FTS5 index over the same rows, and `nodes` and
`edges` hold the graph. All five are written in one transaction per build.

## Query paths

`hybrid_search` fuses vector and keyword rankings; `graph_query` walks edges.
Neither touches the network, so retrieval keeps working offline.
