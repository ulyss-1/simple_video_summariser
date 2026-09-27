"""Repository layer (issue #14, architecture.md §2, §3).

One module per aggregate (``channels``, ``videos``, ``transcripts``,
``analyses``), giving services typed functions for reading and writing
domain rows so no handler ever writes SQL. Every function takes a psycopg
connection as its first argument and runs parameterized SQL - no ORM, no
query builder, no string-formatted SQL. ``common/repo/`` imports only
``common/`` (AGENTS.md -> Rules); it never imports ``adapters/``.

No function here commits or rolls back. Transaction boundaries belong to
the caller - this is what lets ``analyses.save_analysis`` be one statement
group inside a larger atomic write (#30).
"""

from __future__ import annotations
