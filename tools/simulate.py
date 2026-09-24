"""
HỆ THỐNG MÔ PHỎNG MẠNG VÀ TRÌNH DIỄN TRỰC QUAN - share_download (ĐỀ TÀI SỐ 3)

Script tiện ích dùng cho nghiên cứu thực nghiệm và báo cáo:
- Tự động dựng 3 Server S1, S2, S3 trên các cổng 5001, 5002, 5003.
- Kết nối qua Router R mô phỏng băng thông bất đối xứng (Token Bucket).
- Hiển thị bảng điều khiển trực quan (Terminal Live Dashboard).
- Hỗ trợ cờ --failover để giả lập sập mạng và --loss để giả lập rớt gói.
"""

import argparse
import hashlib
import os
import sys
import time
import threading
from pathlib import Path

# Đảm bảo UTF-8 trên Windows Console
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Đảm bảo import được module từ thư mục gốc
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from server import FileServer
from client import ClientConfig, ServerEndpoint, download
from dashboard import VisualDashboard
from tools.simulator.router import NetworkRouter

DATA_DIR = BASE_DIR / "data"
DOWNLOADS_DIR = BASE_DIR / "downloads"
CONFIG_FILE = DATA_DIR / "config.dat"


def ensure_test_file():
    """Tạo file 10MB test nếu chưa có."""
    DATA_DIR.mkdir(exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    if not CONFIG_FILE.exists() or CONFIG_FILE.stat().st_size == 0:
        print("[Simulate] Đang tạo file mẫu 10MB data/config.dat...")
        with open(CONFIG_FILE, "wb") as f:
            chunk = os.urandom(1024 * 1024)
            for _ in range(10):
                f.write(chunk)


def run_visual_demo(failover: bool = False, loss: float = 0.0):
    ensure_test_file()

    # Xóa file cũ trong downloads để tải mới từ 0%
    target_file = DOWNLOADS_DIR / "config.dat"
    part_file = DOWNLOADS_DIR / "config.dat.part"
    state_file = DOWNLOADS_DIR / "config.dat.part.state.json"
    for f in (target_file, part_file, state_file):
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    print("\033[2J\033[H", end="")
    print("=" * 72)
    print(" KHỞI ĐỘNG HỆ THỐNG MÔ PHỎNG ĐỀ TÀI SỐ 3...")
    print("=" * 72)
    print("• Khởi động 3 Máy chủ dữ liệu (S1: 5001, S2: 5002, S3: 5003)...")
    s1 = FileServer(CONFIG_FILE, "127.0.0.1", 5001)
    s2 = FileServer(CONFIG_FILE, "127.0.0.1", 5002)
    s3 = FileServer(CONFIG_FILE, "127.0.0.1", 5003)
    s1.start()
    s2.start()
    s3.start()

    print("• Khởi động Bộ định tuyến Router với Băng thông bất đối xứng:")
    print("  - R-S1 (Port 6001): 8,000 Kbps (Siêu nhanh)")
    print(f"  - R-S2 (Port 6002): 4,000 Kbps (Trung bình){' [Loss ' + str(int(loss*100)) + '%]' if loss > 0 else ''}")
    print(f"  - R-S3 (Port 6003): 1,500 Kbps (Chậm){' [SẼ BỊ SẬP Ở 40%]' if failover else ''}")

    routes = [
        {"channel_id": "R-S1", "listen_port": 6001, "target_host": "127.0.0.1", "target_port": 5001, "bandwidth_bps": int(8000 * 1000 / 8), "packet_loss_rate": 0.0},
        {"channel_id": "R-S2", "listen_port": 6002, "target_host": "127.0.0.1", "target_port": 5002, "bandwidth_bps": int(4000 * 1000 / 8), "packet_loss_rate": loss},
        {"channel_id": "R-S3", "listen_port": 6003, "target_host": "127.0.0.1", "target_port": 5003, "bandwidth_bps": int(1500 * 1000 / 8), "packet_loss_rate": 0.0},
    ]
    router = NetworkRouter(routes)
    router.start()

    time.sleep(0.5)

    # Cấu hình Client kết nối qua các kênh Router
    config = ClientConfig(
        servers=(
            ServerEndpoint("S1", "127.0.0.1", 6001),
            ServerEndpoint("S2", "127.0.0.1", 6002),
            ServerEndpoint("S3", "127.0.0.1", 6003),
        ),
        filename="config.dat",
        output=target_file,
        chunk_size=256 * 1024, # 40 chunks
        connect_timeout=3.0,
        read_timeout=10.0,
        reconnect_attempts=3,
    )

    file_size = CONFIG_FILE.stat().st_size
    total_chunks = (file_size + config.chunk_size - 1) // config.chunk_size

    # Khởi tạo Dashboard trực quan
    dash = VisualDashboard(
        filename="config.dat",
        file_size=file_size,
        total_chunks=total_chunks,
        server_names=["S1", "S2", "S3"]
    )

    # Luồng giả lập kịch bản Failover (nếu có cờ --failover)
    def failover_killer():
        while not dash.is_finished:
            if dash.completed_count >= int(total_chunks * 0.35):
                # Tắt S3 đột ngột!
                for ch in router.channels:
                    if ch.channel_id == "R-S3":
                        ch.stop()
                s3.shutdown()
                dash.on_server_fail("S3")
                break
            time.sleep(0.1)

    if failover:
        t_fail = threading.Thread(target=failover_killer, daemon=True)
        t_fail.start()

    def _on_event(ev):
        if ev.kind == "chunk_start" and ev.chunk_id is not None and ev.server:
            dash.on_chunk_start(ev.server, ev.chunk_id)
        elif ev.kind == "chunk" and ev.server:
            dash.on_chunk_complete(ev.server, ev.chunk_id if ev.chunk_id is not None else -1, ev.bytes_delta)
        elif ev.kind == "chunk_requeued" and ev.server:
            dash.on_chunk_retry(ev.server, ev.chunk_id if ev.chunk_id is not None else -1)
        elif ev.kind in ("server_failed", "server_unavailable") and ev.server:
            dash.on_server_fail(ev.server)
        elif ev.kind == "verifying":
            dash.set_sha256_status("Kiểm tra SHA-256...")

    try:
        res = download(config, event_callback=_on_event)
        dash.finish(res.file_sha256)
    except Exception as e:
        print(f"\n[Lỗi tải file]: {e}")
    finally:
        # Dọn dẹp tiến trình
        router.stop()
        s1.shutdown()
        s2.shutdown()
        s3.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Chạy công cụ mô phỏng và trình diễn trực quan cho Đề tài số 3")
    parser.add_argument("--failover", action="store_true", help="Kích hoạt kịch bản tự động tắt S3 ở 35%% để xem Failover")
    parser.add_argument("--loss", type=float, default=0.0, help="Tỷ lệ rớt gói trên S2 (ví dụ 0.05 là 5%%)")
    args = parser.parse_args()

    run_visual_demo(failover=args.failover, loss=args.loss)


if __name__ == "__main__":
    main()
