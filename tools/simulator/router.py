"""
Module Router: Mô phỏng Router R đứng giữa Client C1 và các Server S1, S2, S3.

Đặc điểm theo đề bài:
1. Nhánh Server - Router có băng thông giới hạn (100 ~ 1000 Kbps) và có thể mất gói (Packet Loss).
2. Nhánh Router - C1 băng thông cao (10 Mbps).
3. Đóng vai trò làm Network Proxy điều phối lưu lượng.
"""

import socket
import threading
import time
import random
from typing import List, Dict, Any, Optional

from .rate_limiter import TokenBucketLimiter


class RouterChannel(threading.Thread):
    """Một kênh chuyển tiếp dữ liệu của Router ứng với 1 Server cụ thể."""
    def __init__(
        self,
        channel_id: str,
        listen_port: int,
        target_host: str,
        target_port: int,
        bandwidth_bps: int,
        packet_loss_rate: float = 0.0
    ):
        super().__init__(daemon=True)
        self.channel_id = channel_id
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.limiter = TokenBucketLimiter(bandwidth_bps)
        self.loss_rate = packet_loss_rate
        self.is_running = False
        self._server_sock: Optional[socket.socket] = None

    def run(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("0.0.0.0", self.listen_port))
        self._server_sock.listen(20)
        self.is_running = True

        while self.is_running:
            try:
                client_conn, _ = self._server_sock.accept()
                threading.Thread(
                    target=self._handle_proxy,
                    args=(client_conn,),
                    daemon=True
                ).start()
            except OSError:
                break

    def _handle_proxy(self, client_sock: socket.socket):
        """Kết nối tới server mục tiêu và chuyển tiếp 2 chiều."""
        try:
            target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_sock.connect((self.target_host, self.target_port))
        except Exception:
            client_sock.close()
            return

        def forward(src: socket.socket, dst: socket.socket, is_server_to_client: bool):
            try:
                while self.is_running:
                    data = src.recv(8192)
                    if not data:
                        break

                    # Nếu là chiều Server -> Client, áp dụng giới hạn băng thông và rớt gói
                    if is_server_to_client:
                        # 1. Bóp băng thông
                        self.limiter.limit(len(data))

                        # 2. Mô phỏng rớt gói (Packet Loss ngẫu nhiên)
                        if self.loss_rate > 0 and random.random() < self.loss_rate:
                            # Đột ngột ngắt kết nối để mô phỏng rớt kết nối mạng / mất gói
                            break

                    dst.sendall(data)
            except Exception:
                pass
            finally:
                src.close()
                dst.close()

        # Luồng 1: Client -> Server
        threading.Thread(target=forward, args=(client_sock, target_sock, False), daemon=True).start()
        # Luồng 2: Server -> Client (áp dụng Rate Limiter + Packet Loss)
        threading.Thread(target=forward, args=(target_sock, client_sock, True), daemon=True).start()

    def stop(self):
        self.is_running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass


class NetworkRouter:
    """Mô phỏng toàn bộ Router R với các kênh nối tới S1, S2, S3."""
    def __init__(self, routes_config: List[Dict[str, Any]]):
        self.channels: List[RouterChannel] = []
        for cfg in routes_config:
            ch = RouterChannel(
                channel_id=cfg["channel_id"],
                listen_port=cfg["listen_port"],
                target_host=cfg["target_host"],
                target_port=cfg["target_port"],
                bandwidth_bps=cfg["bandwidth_bps"],
                packet_loss_rate=cfg.get("packet_loss_rate", 0.0)
            )
            self.channels.append(ch)

    def start(self):
        for ch in self.channels:
            ch.start()
        print("[Router R] Đã khởi động các kênh chuyển tiếp:")
        for ch in self.channels:
            bw_kbps = ch.limiter.rate_bytes_per_sec * 8 / 1000
            print(f"  • {ch.channel_id}: Router Port {ch.listen_port} ➔ Target {ch.target_port} "
                  f"[Băng thông: {bw_kbps:.0f} Kbps, Rớt gói: {ch.loss_rate * 100:.1f}%]")

    def stop(self):
        for ch in self.channels:
            ch.stop()
        print("[Router R] Đã dừng tất cả các kênh.")
