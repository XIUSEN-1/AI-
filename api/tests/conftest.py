import os
import tempfile
from pathlib import Path

os.environ.setdefault("COMPASS_DB", str(Path(tempfile.mkdtemp(prefix="compass-test-")) / "test.db"))
