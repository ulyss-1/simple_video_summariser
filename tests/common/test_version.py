import importlib.metadata

import common


def test_version_matches_installed_distribution_metadata() -> None:
    assert common.__version__ == importlib.metadata.version("ytdigest")
