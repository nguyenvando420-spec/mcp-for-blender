"""BAS-06 socket security regression suite; real listener, stub bpy main-thread pump."""
import json
import socket
import time
import pytest
from test_server_threading import _make_server, _free_port

TOKEN = "a" * 32

def _request(server, command, pump=True):
    with socket.create_connection(("localhost", server.port), timeout=3) as client:
        client.settimeout(3)
        client.sendall((json.dumps(command) + "\n").encode())
        if pump:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                server._drain_command_queue()
                try:
                    return json.loads(client.recv(65536))
                except socket.timeout:
                    pass
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
