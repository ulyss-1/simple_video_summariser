"""Side "b" of the xdist isolation probes (#296); see tests/xdist_probe.py."""

from tests.conftest import head_dsn, migrated_template, postgres_container  # noqa: F401
from tests.xdist_probe import (  # noqa: F401
    drop_signal,
    side,
    test_concurrent_tests_never_share_a_database,
    test_dropping_one_workers_database_does_not_touch_the_other,
    test_identity_first,
    test_identity_second_same_container,
    test_template_clone_not_blocked_across_workers,
    test_unfiltered_pg_stat_activity_counts_see_only_own_worker,
)

SIDE = "b"
