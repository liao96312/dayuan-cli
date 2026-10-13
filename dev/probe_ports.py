"""实测本机几个 loopback 入口：9882 / 9883 / 50018 / 50010。"""

from __future__ import annotations

import json
import socket
import sys
import time

HOST = "127.0.0.1"


def raw(port: int, payload: bytes, timeout: float = 4.0) -> str:
    """裸 socket：能区分"拒绝连接/被 RST/挂起/有响应"。"""
    try:
        s = socket.create_connection((HOST, port), timeout=timeout)
    except ConnectionRefusedError:
        return "连接被拒绝（端口没在听）"
    except Exception as exc:  # noqa: BLE001
        return f"连接失败 {exc}"
    try:
        s.settimeout(timeout)
        s.sendall(payload)
        chunks = []
        start = time.time()
        while time.time() - start < timeout:
            try:
                data = s.recv(65536)
            except socket.timeout:
                return ("挂起（超时无任何响应）" if not chunks
                        else "已收 " + str(sum(len(c) for c in chunks)) + " 字节后挂起")
            except ConnectionResetError:
                return "被 RST 重置" + (f"（先前收到 {sum(len(c) for c in chunks)} 字节）" if chunks else "")
            if not data:
                return ("对端主动关闭" if not chunks
                        else f"对端关闭，共收到 {sum(len(c) for c in chunks)} 字节")
            chunks.append(data)
            body = b"".join(chunks)
            if b"\r\n\r\n" in body:
                head, _, rest = body.partition(b"\r\n\r\n")
                try:
                    clen = int([l.split(b":")[1] for l in head.split(b"\r\n")
                                if l.lower().startswith(b"content-length")][0])
                except Exception:  # noqa: BLE001
                    clen = 0
                if len(rest) >= clen:
                    return "响应:\n" + body.decode("utf-8", "replace")[:1200]
        return "响应:\n" + b"".join(chunks).decode("utf-8", "replace")[:1200]
    finally:
        s.close()


def http(port: int, method: str, path: str, body: dict | None = None,
         headers: dict | None = None, timeout: float = 4.0) -> str:
    hdrs = {"Host": f"{HOST}:{port}", "Connection": "close", "Accept": "*/*"}
    hdrs.update(headers or {})
    data = b""
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(data))
    req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
    return raw(port, req.encode() + data, timeout)


def main() -> int:
    tests = [
        ("9883 GET /v1/models  无鉴权",
         lambda: http(9883, "GET", "/v1/models")),
        ("9883 GET /v1/models  Bearer sk-test",
         lambda: http(9883, "GET", "/v1/models", headers={"Authorization": "Bearer sk-test"})),
        ("9883 GET /",
         lambda: http(9883, "GET", "/")),
        ("9883 POST /v1/chat/completions",
         lambda: http(9883, "POST", "/v1/chat/completions",
                      body={"model": "x", "messages": [{"role": "user", "content": "hi"}]}, timeout=6)),
        ("9882 GET /mcp",
         lambda: http(9882, "GET", "/mcp")),
        ("9882 POST /mcp initialize",
         lambda: http(9882, "POST", "/mcp",
                      body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=6)),
        ("50018 直接连（看握手）",
         lambda: raw(50018, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")),
        ("50010 直接连",
         lambda: raw(50010, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")),
    ]
    for name, fn in tests:
        print(f"--- {name}")
        try:
            print("    " + fn().replace("\n", "\n    "))
        except Exception as exc:  # noqa: BLE001
            print(f"    异常 {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
