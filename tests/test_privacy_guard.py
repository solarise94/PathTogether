"""Small repository guard for a production hostname leaked by legacy comments.

The literal hostname is intentionally not repeated here. Splitting one token
with a character class keeps the test useful without reintroducing the value
into a reachable Git blob.
"""
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(r"pingood[m]ice", re.IGNORECASE)


def test_tracked_files_do_not_reference_legacy_production_tunnel():
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT
    ).decode("utf-8").split("\0")
    hits = []
    for rel in filter(None, tracked):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if FORBIDDEN.search(text):
            hits.append(rel)
    assert not hits, "tracked files contain a legacy production tunnel hostname: %r" % hits
