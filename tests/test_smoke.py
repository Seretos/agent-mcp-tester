from mcp_tester_plugin.server import ping


def test_ping():
    assert ping() == "pong"
