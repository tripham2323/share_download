"""Tiện ích: Khởi động đồng thời 3 FileServer (S1: 5001, S2: 5002, S3: 5003)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from server import FileServer

CONFIG_FILE = (BASE_DIR / "data" / "config.dat").resolve()


def main() -> int:
    if not CONFIG_FILE.exists():
        print(f"Lỗi: Không tìm thấy tệp {CONFIG_FILE}!", file=sys.stderr)
        return 1

    print("=" * 65)
    print(" KHỞI ĐỘNG 3 FILE SERVER (S1: 5001, S2: 5002, S3: 5003)")
    print("=" * 65)

    s1 = FileServer(CONFIG_FILE, "0.0.0.0", 5001)
    s2 = FileServer(CONFIG_FILE, "0.0.0.0", 5002)
    s3 = FileServer(CONFIG_FILE, "0.0.0.0", 5003)

    s1.start()
    s2.start()
    s3.start()

    print("✓ Đã bật thành công cả 3 máy chủ phục vụ file data/config.dat:")
    print("  • Máy chủ S1 -> 127.0.0.1:5001")
    print("  • Máy chủ S2 -> 127.0.0.1:5002")
    print("  • Máy chủ S3 -> 127.0.0.1:5003")
    print("\n[INFO] Để cửa sổ này luôn mở khi chạy ứng dụng Desktop/CLI.")
    print("[INFO] Nhấn tổ hợp phím Ctrl + C để dừng 3 máy chủ khi cần.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nĐang dừng các máy chủ...")
        s1.shutdown()
        s2.shutdown()
        s3.shutdown()
        print("✓ Đã dừng 3 máy chủ.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
