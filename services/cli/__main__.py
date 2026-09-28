"""``python -m services.cli run <url-or-id>`` (same as the ``ytdigest`` script)."""

import sys

from services.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
