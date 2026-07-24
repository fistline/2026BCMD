---
title: Ingestion Pipeline
depends_on: [chunking-rules]
owner: platform
---

# Ingestion Pipeline

Ingestion is a promotion, never an edit. The watcher copies each file out of the
inbox into `data/raw/documents/` and then leaves it alone forever. Because the
raw zone is immutable, everything under `data/processed/` can be deleted and
rebuilt from code plus raw at any time, on any machine.

## Promotion rules

A file is promoted only when its SHA-256 is not already present in the raw zone.
Re-dropping an identical file is therefore a no-op, which is what makes the whole
pipeline safe to re-run.

## Downstream

Chunk boundaries follow [[chunking-rules]]; the resulting units feed the gold
layer that the serving index reads.
