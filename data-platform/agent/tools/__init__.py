"""Tools the deployed agent calls against the built serving index.

Each module here is both importable (returns plain Python objects) and runnable
as a CLI that prints JSON, so the same implementation backs an in-process tool
call, a shell invocation, and an MCP wrapper without a second code path.

Every tool opens `data/serving/index.sqlite` read-only and touches nothing else.
None of them reach the network, which is the property that lets the agent keep
answering while offline.

The three entry points are resolved lazily. Importing them eagerly here would
make `python -m agent.tools.hybrid_search` import the module twice, once as a
package attribute and once as `__main__`, which Python warns about and which
gives the two copies separate module state.
"""

_EXPORTS = {
    "hybrid_search_tool": "agent.tools.hybrid_search",
    "graph_query_tool": "agent.tools.graph_query",
    "graph_rag_tool": "agent.tools.graph_rag",
    "scrapling_mcp_status": "agent.tools.scrapling_mcp",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    """PEP 562 lazy attribute access for the tool entry points."""
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)


def __dir__() -> list:
    return sorted(set(globals()) | set(_EXPORTS))
