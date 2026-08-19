"""The roadmap graph, store and CLI, as a standalone package.

Import the graph explicitly rather than re-exporting it here::

    from roadmap_core import graph
    from roadmap_core.graph import derive_status, validate_graph

Kept bare on purpose, for a reason that outlived the one it was written for.
It used to be that ``scripts/roadmap.py`` loaded ``graph.py`` *by path* with
nothing installed, and a package ``__init__`` importing submodules would have
broken that. The CLI lives in here now (``roadmap_core.cli``) and imports its
siblings normally, so that particular hazard is gone.

What remains is better: ``graph``, ``store`` and ``stores`` are importable with
no third-party package present at all, and ``cli`` is importable without
PyYAML — it imports it inside the two functions that parse item files. A
``__init__`` that reached for ``cli`` would drag that requirement onto every
caller of the graph, including the Lucille backend, which needs none of it.
``roadmap-core-tests.yml`` asserts exactly this by running the suite in an
environment where ``yaml`` cannot be imported at all.
"""
