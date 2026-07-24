---
title: Architecture Overview
depends_on: [ingestion-pipeline, serving-index]
owner: platform
---

# Architecture Overview

This spoke node keeps every byte of data on local disk. The control plane (this
Git repository) carries logic only; the data plane (`data/`) carries bytes only.

## Layers

Files land in `data/inbox/documents/`, are promoted unchanged into `data/raw/`,
are extracted by Meltano into a DuckDB landing database, are refined by SQLMesh
through bronze, silver and gold, and are finally indexed into one SQLite file.

## Retrieval

The agent asks questions through `hybrid_search`, which fuses a sqlite-vec
nearest-neighbour ranking with an FTS5 keyword ranking using reciprocal rank
fusion. Impact questions go through `graph_query` instead, which walks the
`edges` table with a recursive common table expression.

See also [[ingestion-pipeline]] and [[serving-index]].
