#!/usr/bin/env python3
"""
本地认证代理转发器 - 解决 Chrome 150+ 不支持 --proxy-server 内嵌凭证的问题
Chrome -> 127.0.0.1:18082 (无需认证) -> gate.ipdeep.com:8082 (自动添加 Proxy-Authorization)
"""
import socket
import threading
import base64
import select
import logging

log = logging.getLogger("proxy_relay")

class LocalAuthProxyRelay:
    def __init__(self, upstream_host, upstream_port, username, password,
                 listen_host="127.0.0.1", listen_port=18082):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.username = username
        self.password = password
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._server_socket = None
        self._running = False
        self._thread = None
        credentials = f"{username}:{password}".encode()
        self._auth_header = f"Proxy-Authorization: Basic {base64.b64encode(credentials).decode()}\r\n"

    def start(self):
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.listen_host, self.listen_port))
        self._server_socket.listen(50)
        self._server_socket.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        log.info(f"proxy relay started: {self.listen_host}:{self.listen_port} -> {self.upstream_host}:{self.upstream_port}")
        return f"http://{self.listen_host}:{self.listen_port}"

    def stop(self):
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _accept_loop(self):
        while self._running:
            try:
                client_sock, addr = self._server_socket.accept()
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                pass

    def _handle_client(self, client_sock):
        try:
            client_sock.settimeout(30)
            request_data = b""
            while b"\r\n\r\n" not in request_data:
                chunk = client_sock.recv(8192)
                if not chunk:
                    client_sock.close()
                    return
                request_data += chunk
                if len(request_data) > 65536:
                    client_sock.close()
                    return

            header_end = request_data.index(b"\r\n\r\n")
            header_part = request_data[:header_end].decode("utf-8", errors="replace")
            body_part = request_data[header_end + 4:]

            lines = header_part.split("\r\n")
            request_line = lines[0]
            method = request_line.split(" ")[0] if " " in request_line else ""

            if method == "CONNECT":
                self._handle_connect(client_sock, request_line, body_part)
            else:
                self._handle_http(client_sock, header_part, body_part)
        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass

    def _handle_connect(self, client_sock, request_line, initial_data):
        parts = request_line.split(" ")
        if len(parts) < 2:
            client_sock.close()
            return
        target = parts[1]
        if ":" in target:
            target_host, target_port = target.rsplit(":", 1)
            target_port = int(target_port)
        else:
            target_host = target
            target_port = 443

        upstream = None
        try:
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.settimeout(15)
            upstream.connect((self.upstream_host, self.upstream_port))

            connect_req = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
                f"{self._auth_header}"
                f"\r\n"
            )
            upstream.sendall(connect_req.encode())

            response = b""
            while b"\r\n\r\n" not in response:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                response += chunk

            response_str = response.decode("utf-8", errors="replace")
            first_line = response_str.split("\r\n")[0] if "\r\n" in response_str else response_str

            if "200" in first_line:
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                extra = response[response.index(b"\r\n\r\n") + 4:] if b"\r\n\r\n" in response else b""
                if extra:
                    upstream.sendall(extra)
                if initial_data:
                    upstream.sendall(initial_data)
                self._tunnel(client_sock, upstream)
            else:
                log.debug(f"CONNECT rejected: {first_line}")
                client_sock.sendall(response)
                client_sock.close()
                upstream.close()
        except Exception as e:
            log.debug(f"CONNECT error: {e}")
            try:
                client_sock.close()
            except Exception:
                pass
            if upstream:
                try:
                    upstream.close()
                except Exception:
                    pass

    def _handle_http(self, client_sock, header_part, body_part):
        upstream = None
        try:
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.settimeout(15)
            upstream.connect((self.upstream_host, self.upstream_port))

            lines = header_part.split("\r\n")
            has_auth = any(l.lower().startswith("proxy-authorization:") for l in lines)
            if not has_auth:
                lines.insert(1, self._auth_header.strip())

            new_header = "\r\n".join(lines) + "\r\n\r\n"
            upstream.sendall(new_header.encode() + body_part)
            self._tunnel(client_sock, upstream)
        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass
            if upstream:
                try:
                    upstream.close()
                except Exception:
                    pass

    def _tunnel(self, sock1, sock2):
        sock1.setblocking(False)
        sock2.setblocking(False)
        try:
            while self._running:
                readable, _, exceptional = select.select([sock1, sock2], [], [sock1, sock2], 60)
                if exceptional:
                    break
                if not readable:
                    break
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    if s is sock1:
                        sock2.sendall(data)
                    else:
                        sock1.sendall(data)
        except Exception:
            pass
        finally:
            try:
                sock1.close()
            except Exception:
                pass
            try:
                sock2.close()
            except Exception:
                pass


_relay_instance = None

def start_relay(upstream_host, upstream_port, username, password, listen_port=18082):
    global _relay_instance
    if _relay_instance is not None:
        _relay_instance.stop()
    _relay_instance = LocalAuthProxyRelay(upstream_host, upstream_port, username, password,
                                           listen_port=listen_port)
    return _relay_instance.start()

def stop_relay():
    global _relay_instance
    if _relay_instance:
        _relay_instance.stop()
        _relay_instance = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    import json, time
    with open('config.json', 'r') as f:
        cfg = json.load(f)
    proxy_user = cfg.get('ip_proxy_user', '')
    proxy_pwd = cfg.get('ip_proxy_pwd', '')
    local_addr = start_relay("gate.ipdeep.com", 8082, proxy_user, proxy_pwd)
    print(f"Local proxy: {local_addr}")
    print("Test: curl -x http://127.0.0.1:18082 https://freestoryweb.com/")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_relay()
