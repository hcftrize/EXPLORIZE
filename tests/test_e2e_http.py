"""End to end over real HTTP: CLI -> RpcClient -> fake JSON-RPC node -> Postgres."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from indexer.__main__ import main
from tests.fakes import make_log

HEAD = 2500
DEPLOY = 40
LOGS = [make_log(b, 0, 0xA1 if b != DEPLOY else 0, 0xB2, 500) for b in range(DEPLOY, HEAD, 11)]
SUPPLY = sum(500 for l in LOGS if l["topics"][1].endswith("00" * 20))  # only mints count


def rpc_result(method, params):
    if method == "eth_blockNumber":
        return hex(HEAD)
    if method == "eth_chainId":
        return hex(8453)
    if method == "eth_getCode":
        b = HEAD if params[1] == "latest" else int(params[1], 16)
        return "0x6080" if b >= DEPLOY else "0x"
    if method == "eth_call":
        sel = params[0]["data"]
        if sel == "0x18160ddd":
            return hex(SUPPLY)
        if sel == "0x313ce567":
            return hex(18)
        return "0x" + (32).to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex() + b"RIZE".hex().ljust(64, "0")
    if method == "eth_getBlockByNumber":
        return {"timestamp": hex(1_700_000_000 + int(params[0], 16) * 2)}
    if method == "eth_getLogs":
        f = params[0]
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        if hi - lo + 1 > 600:
            raise ValueError("block range too large")
        return [l for l in LOGS if lo <= int(l["blockNumber"], 16) <= hi]
    raise KeyError(method)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        calls = body if isinstance(body, list) else [body]
        out = []
        for c in calls:
            try:
                out.append({"jsonrpc": "2.0", "id": c["id"], "result": rpc_result(c["method"], c["params"])})
            except ValueError as e:
                out.append({"jsonrpc": "2.0", "id": c["id"], "error": {"code": -32005, "message": str(e)}})
        data = json.dumps(out if isinstance(body, list) else out[0]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def node():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_cli_end_to_end(node, conn, monkeypatch, capsys):
    import os
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("RPC_URL_BASE", node)
    monkeypatch.setenv("RPC_MAX_RPS", "0")
    assert main(["init-db", "--chain", "base"]) == 0
    assert main(["probe", "--chain", "base"]) == 0
    assert main(["sync", "--chain", "base"]) == 0
    assert main(["sync", "--chain", "base"]) == 0  # idempotent second run
    assert main(["verify", "--chain", "base"]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "MISMATCH" not in out
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM transfers WHERE chain_id = 8453")
        assert cur.fetchone()[0] == len(LOGS)
    conn.commit()
