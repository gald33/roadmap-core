"""The roadmap graph, as a standalone package.

Import the graph explicitly rather than re-exporting it here::

    from roadmap_core import graph
    from roadmap_core.graph import derive_status, validate_graph

Kept bare on purpose. ``scripts/roadmap.py`` loads ``roadmap_core/graph.py`` by
path with nothing installed, and a package ``__init__`` that imported submodules
would make that load depend on the package being importable as a package — which
is the dependency this layout exists to avoid.
"""
