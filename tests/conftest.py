import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexer import db  # noqa: E402


@pytest.fixture()
def conn():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set (needs a throwaway Postgres)")
    c = db.connect(url)
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS transfers, chain_state, labels, ccip_messages, prices_hourly CASCADE")
    c.commit()
    db.init_schema(c)
    yield c
    c.close()
