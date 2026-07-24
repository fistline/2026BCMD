---
title: Chunking Rules
owner: platform
---

# Chunking Rules

Chunking is deterministic. The same bytes always yield the same chunk ids, so a
rebuild never duplicates rows in the serving index.

## Markdown

Markdown is split on ATX headings first. A section longer than the window is cut
again on the last blank line inside the window, with a small overlap carried into
the next chunk so a sentence spanning a boundary is still retrievable.

## Source code

Python is split by top-level definition using the standard library abstract
syntax tree. Keeping a whole function in one chunk is what lets an exact
identifier match and a semantic match land on the same unit of text.
