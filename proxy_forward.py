#!/usr/bin/env python3
"""本地转发代理：自动给上游 HTTP 代理附加 Proxy-Authorization 头。

用途：Chrome/Selenium 无法可靠地在 --proxy-server 里内嵌认证（Chrome 150+ 移除），
MV3 扩展 onAuthRequired 在 Chrome 151 有 service worker 竞态（实测 100% 失败），
CDP Fetch.authRequired 需要 Playwright 事件 API（Selenium execute_cdp_cmd 不支持事件推送）。
本代理作为中间层：Chrome 无认证直连本地 -> 本地代理连上游并自动带 Proxy-Authorization。

用法: proxy_forward.py <listen_port> <upstream_host> <upstream_port> <username> <password>
"""
import socket
import threading
import sys
import base64


def build_auth(user, pwd):
    return ("Basic " + base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")).encode("ascii")


def pipe(src, dst):
    """单向字节流转发直到 EOF"""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def handle(client, upstream, auth):
    up = None
    try:
        # 读请求头（直到 \r\n\r\n），限制大小防滥用
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                return
            data += chunk
            if len(data) > 2_000_000:
                return
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        first = lines[0]
        is_connect = first.upper().startswith(b"CONNECT ")

        up = socket.create_connection(upstream, timeout=20)

        if is_connect:
            target = first.split(b" ", 2)[1]
            req = (b"CONNECT " + target + b" HTTP/1.1\r\n"
                   b"Host: " + target + b"\r\n"
                   b"Proxy-Authorization: " + auth + b"\r\n"
                   b"Proxy-Connection: keep-alive\r\n\r\n")
            up.sendall(req)
            resp = b""
            while b"\r\n\r\n" not in resp:
                c = up.recv(4096)
                if not c:
                    break
                resp += c
            status_line = resp.split(b"\r\n", 1)[0] if resp else b""
            status_code = status_line.split(b" ", 2)[1] if len(status_line.split(b" ", 2)) > 1 else b""
            if status_code == b"200":
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                t1 = threading.Thread(target=pipe, args=(client, up), daemon=True)
                t2 = threading.Thread(target=pipe, args=(up, client), daemon=True)
                t1.start()
                t2.start()
                t1.join()
                t2.join()
            else:
                # 上游拒绝（407 等），原样回给客户端
                client.sendall(resp or b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        else:
            # 普通 HTTP 代理请求（绝对 URI 形式）：保留原始头，仅注入认证
            headers = [l for l in lines if not l.lower().startswith(b"proxy-authorization")]
            new_head = b"\r\n".join(headers) + b"\r\nProxy-Authorization: " + auth + b"\r\n\r\n"
            up.sendall(new_head + rest)
            while True:
                c = up.recv(65536)
                if not c:
                    break
                client.sendall(c)
    except Exception as _e:
        import traceback
        print(f"[proxy_forward] handle error: {type(_e).__name__}: {_e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except Exception:
            pass
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            if up:
                up.close()
        except Exception:
            pass


def main():
    if len(sys.argv) < 6:
        print("usage: proxy_forward.py <listen_port> <upstream_host> <upstream_port> <username> <password>")
        sys.exit(1)
    listen_port = int(sys.argv[1])
    up_host, up_port = sys.argv[2], int(sys.argv[3])
    user, pwd = sys.argv[4], sys.argv[5]
    auth = build_auth(user, pwd)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen_port))
    srv.listen(64)
    print(f"proxy_forward ready: 127.0.0.1:{listen_port} -> {up_host}:{up_port} (auth={'yes' if user else 'no'})", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c, (up_host, up_port), auth), daemon=True).start()


if __name__ == "__main__":
    main()
