"""BAS-06 socket security regression suite; real listener, stub bpy main-thread pump."""
import json
import socket
import time
import pytest
from test_server_threading import _make_server, _free_port

TOKEN = "a" * 32

def _request(server, command, pump=True):
    with socket.create_connection(("localhost", server.port), timeout=3) as client:
        client.settimeout(0.03 if pump else 3)
        client.sendall((json.dumps(command) + "\n").encode())
        if pump:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                server._drain_command_queue()
                try:
                    return json.loads(client.recv(65536))
                except socket.timeout:
                    pass
            raise TimeoutError("socket unit test main loop did not respond")
        return json.loads(client.recv(65536))

def test_token_rejected_before_enqueue():
    s = _make_server(); s.start()
    try:
        resp = _request(s, {"type":"ping","auth":"wrong"},pump=False)
        assert resp["code"] == "unauthorized"
        assert s.command_queue.empty()
    finally: s.stop()

def test_oversized_rejected_before_enqueue():
    s = _make_server(); s.start()
    try:
        with socket.create_connection(("localhost",s.port),timeout=3) as sock:
            sock.settimeout(3)
            sock.sendall(b"x"*(s.MAX_COMMAND_BYTES+1))
            assert json.loads(sock.recv(8192))["code"] == "payload_too_large"
        assert s.command_queue.empty()
    finally: s.stop()

def test_default_profile_denies_code_and_long_work():
    s=_make_server(); s.profile="observer"; s.start()
    try:
        for kind, expected in [("execute_code","permission_denied"),
                               ("create_hunyuan_job","async_required"),
                               ("studio_create_object_v1","permission_denied")]:
            response=_request(s,{"auth":TOKEN,"type":kind,"params":{}},pump=False)
            assert response["code"]==expected
    finally: s.stop()

def test_queue_backpressure_and_bounded_drain():
    s=_make_server(); s.running=True
    class Writer:
        def __init__(self): self.data=[]
        def sendall(self,data): self.data.append(json.loads(data))
    client=Writer()
    for i in range(s.MAX_QUEUE):
        assert s._receive_command(client,json.dumps({"auth":TOKEN,"type":"ping","request_id":str(i)}).encode())
    s._receive_command(client,json.dumps({"auth":TOKEN,"type":"ping","request_id":"full"}).encode())
    assert client.data[-1]["code"] == "queue_full"
    assert s.command_queue.qsize()==s.MAX_QUEUE
    s._drain_command_queue()
    assert s.command_queue.qsize()>=s.MAX_QUEUE-s.DRAIN_COMMANDS

def test_loopback_only_and_missing_secret_fail_closed():
    from test_server_threading import BlenderMCPServer
    with pytest.raises(ValueError):
        BlenderMCPServer(host="0.0.0.0",token=TOKEN).start()
    with pytest.raises(ValueError):
        BlenderMCPServer(token="",profile="observer").start()


def test_100_commands_and_disconnect_recovery():
    s = _make_server()
    s.profile = "studio-local"
    s.start()
    try:
        for i in range(100):
            rid = f"stress-{i}"
            with socket.create_connection(("127.0.0.1", s.port), timeout=3) as sock:
                sock.settimeout(0.03)
                sock.sendall((json.dumps({"type":"ping","auth":TOKEN,
                    "request_id":rid}) + "\n").encode())
                deadline = time.monotonic() + 3
                while True:
                    s._drain_command_queue()  # Called by stub GUI main thread
                    try:
                        response = json.loads(sock.recv(8192))
                        break
                    except socket.timeout:
                        assert time.monotonic() < deadline
                assert response["status"] == "success"
                assert response["request_id"] == rid
        # A response persists after the original socket is closed.
        result = _request(s, {"type":"get_request_status","auth":TOKEN,
            "params":{"request_id":"stress-99"}},pump=True)
        assert result["status"] == "success", result
        assert result["result"]["state"] == "completed"
        assert result["result"]["response"]["request_id"] == "stress-99"
    finally:
        s.stop()


def test_request_id_replay_is_safe_and_conflict_denied():
    s = _make_server()
    s.running = True
    class Writer:
        def __init__(self): self.sent = []
        def sendall(self, data): self.sent.append(json.loads(data))
    a = Writer()
    original = {"type":"ping","params":{},"request_id":"fixed","auth":TOKEN}
    assert s._receive_command(a,json.dumps(original).encode())
    assert s.command_queue.qsize() == 1
    assert s._receive_command(a,json.dumps(original).encode())
    assert s.command_queue.qsize() == 1
    assert a.sent[-1]["status"] == "pending"
    conflict = {**original, "params":{"different": True}}
    s._receive_command(a,json.dumps(conflict).encode())
    assert a.sent[-1]["code"] == "request_id_conflict"


def test_client_socket_timeout_must_not_claim_cancellation(monkeypatch):
    from blender_mcp.server import BlenderConnection
    monkeypatch.setenv("BLENDER_MCP_SOCKET_TOKEN",TOKEN)
    class SilentSocket:
        def settimeout(self, seconds): pass
        def sendall(self, data): pass
        def recv(self, size): raise socket.timeout("peer stalled")
        def close(self): pass
    connection = BlenderConnection(host="127.0.0.1",port=9876,sock=SilentSocket())
    with pytest.raises(TimeoutError, match="UNKNOWN"):
        connection._send_command_locked("ping")
    assert connection.sock is None


def test_invalid_complete_frame_does_not_reach_main_thread():
    from test_socket_unicode import _ScriptedSocket
    s = _make_server()
    s.running = True
    sock = _ScriptedSocket([b'{"auth": "x", "type": ping}\n'])
    s._handle_client(sock)
    assert sock.sent and json.loads(sock.sent[0])["code"] == "malformed_payload"
    assert s.command_queue.empty()
