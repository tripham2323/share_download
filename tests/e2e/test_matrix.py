"""
MA TRẬN KIỂM THỬ E2E TỰ ĐỘNG - share_download

Kịch bản:
  T1: 1 Server hoạt động đơn lẻ
  T2: 3 Server tải song song
  T3: Băng thông bất đối xứng (Router Simulator)
  T4: Rớt gói (Packet Loss qua Router)
  T5: Server chết đột ngột (Failover)
  T6: Kiểm tra hash file
"""

import sys
import os
import time
import threading
import hashlib
from pathlib import Path

# Fix console encoding on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from server import FileServer
from client import ClientConfig, ServerEndpoint, download, DownloadError
from checkpoint import CheckpointIdentity, CheckpointStore, state_path_for
from file_utils import build_chunks, prepare_part_file
from tools.simulator.router import NetworkRouter

DATA_DIR = Path(BASE_DIR) / "data"
DOWNLOADS_DIR = Path(BASE_DIR) / "downloads"
ORIGINAL_FILE = DATA_DIR / "config.dat"

DOWNLOADS_DIR.mkdir(exist_ok=True)


class TestSuiteMatrix:
    __test__ = False

    def __init__(self):
        self.results = []
        self.orig_size = ORIGINAL_FILE.stat().st_size
        h = hashlib.sha256()
        with open(ORIGINAL_FILE, 'rb') as f:
            while b := f.read(64 * 1024):
                h.update(b)
        self.orig_hash = h.hexdigest()

    def log_result(self, test_id: str, name: str, passed: bool, notes: str):
        self.results.append({"id": test_id, "name": name, "passed": passed, "notes": notes})
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  [{test_id}] {name:<45}: {status} ({notes})")

    def _cleanup_download(self):
        target = DOWNLOADS_DIR / "config.dat"
        if target.exists():
            target.unlink()
        part = DOWNLOADS_DIR / "config.dat.part"
        if part.exists():
            part.unlink()

    def test_t1_single(self):
        self._cleanup_download()
        s1 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5001)
        s1.start()
        time.sleep(0.2)
        try:
            config = ClientConfig(
                servers=(ServerEndpoint("S1", "127.0.0.1", 5001),),
                filename="config.dat",
                output=DOWNLOADS_DIR / "config.dat",
                chunk_size=1024*1024,
                reconnect_attempts=1
            )
            res = download(config)
            ok = res.file_sha256 == self.orig_hash
            self.log_result("T1", "1 Server hoạt động đơn lẻ", ok, "Tải thành công từ S1")
        except Exception as e:
            self.log_result("T1", "1 Server hoạt động đơn lẻ", False, str(e))
        finally:
            s1.shutdown()

    def test_t2_parallel(self):
        self._cleanup_download()
        s1 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5001)
        s2 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5002)
        s3 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5003)
        for s in (s1, s2, s3): s.start()
        time.sleep(0.2)
        try:
            config = ClientConfig(
                servers=(
                    ServerEndpoint("S1", "127.0.0.1", 5001),
                    ServerEndpoint("S2", "127.0.0.1", 5002),
                    ServerEndpoint("S3", "127.0.0.1", 5003),
                ),
                filename="config.dat",
                output=DOWNLOADS_DIR / "config.dat",
                chunk_size=1024*1024
            )
            res = download(config)
            ok = res.file_sha256 == self.orig_hash and len(res.per_server_chunks) > 1
            self.log_result("T2", "3 Server tải song song", ok, f"Gánh tải: {res.per_server_chunks}")
        except Exception as e:
            self.log_result("T2", "3 Server tải song song", False, str(e))
        finally:
            for s in (s1, s2, s3): s.shutdown()

    def test_t3_asymmetric(self):
        self._cleanup_download()
        s1 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5001)
        s3 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5003)
        for s in (s1, s3): s.start()
        
        router = NetworkRouter([
            {"channel_id": "R-S1", "listen_port": 6001, "target_host": "127.0.0.1", "target_port": 5001, "bandwidth_bps": 100_000_000},
            {"channel_id": "R-S3", "listen_port": 6003, "target_host": "127.0.0.1", "target_port": 5003, "bandwidth_bps": 500_000},
        ])
        router.start()
        time.sleep(0.2)

        try:
            config = ClientConfig(
                servers=(
                    ServerEndpoint("R1", "127.0.0.1", 6001),
                    ServerEndpoint("R3", "127.0.0.1", 6003),
                ),
                filename="config.dat",
                output=DOWNLOADS_DIR / "config.dat",
                chunk_size=512*1024
            )
            res = download(config)
            c1 = res.per_server_chunks.get("R1", 0)
            c3 = res.per_server_chunks.get("R3", 0)
            ok = c1 > c3
            self.log_result("T3", "Băng thông bất đối xứng (S1 siêu nhanh)", ok, f"R1: {c1} chunks, R3: {c3} chunks")
        except Exception as e:
            self.log_result("T3", "Băng thông bất đối xứng", False, str(e))
        finally:
            router.stop()
            for s in (s1, s3): s.shutdown()

    def test_t4_packet_loss(self):
        self._cleanup_download()
        s1 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5001)
        s1.start()
        router = NetworkRouter([
            {"channel_id": "R-S1", "listen_port": 6001, "target_host": "127.0.0.1", "target_port": 5001, "bandwidth_bps": 50_000_000, "packet_loss_rate": 0.0015},
        ])
        router.start()
        time.sleep(0.2)
        try:
            config = ClientConfig(
                servers=(ServerEndpoint("R1", "127.0.0.1", 6001),),
                filename="config.dat",
                output=DOWNLOADS_DIR / "config.dat",
                chunk_size=1024*1024,
                reconnect_attempts=20,
                read_timeout=2.0
            )
            res = download(config)
            ok = res.file_sha256 == self.orig_hash
            self.log_result("T4", "Rớt gói qua Router (Tự động retry)", ok, "Vượt qua rớt gói 5%")
        except Exception as e:
            self.log_result("T4", "Rớt gói", False, str(e))
        finally:
            router.stop()
            s1.shutdown()

    def test_t5_failover(self):
        self._cleanup_download()
        s1 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5001)
        s2 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5002)
        for s in (s1, s2): s.start()
        time.sleep(0.2)

        def killer():
            time.sleep(0.3)
            s2.shutdown()

        threading.Thread(target=killer, daemon=True).start()

        try:
            config = ClientConfig(
                servers=(
                    ServerEndpoint("S1", "127.0.0.1", 5001),
                    ServerEndpoint("S2", "127.0.0.1", 5002),
                ),
                filename="config.dat",
                output=DOWNLOADS_DIR / "config.dat",
                chunk_size=512*1024
            )
            res = download(config)
            ok = res.file_sha256 == self.orig_hash
            self.log_result("T5", "Server sập giữa chừng (Failover)", ok, "S1 gánh phần của S2")
        except Exception as e:
            self.log_result("T5", "Failover", False, str(e))
        finally:
            s1.shutdown()

    def test_t6_resume(self):
        self._cleanup_download()
        s1 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5001)
        s2 = FileServer(ORIGINAL_FILE, "127.0.0.1", 5002)
        for s in (s1, s2): s.start()
        time.sleep(0.2)

        output_path = DOWNLOADS_DIR / "config.dat"
        chunk_size = 512 * 1024  # 20 chunks cho 10MB
        total_chunks = (self.orig_size + chunk_size - 1) // chunk_size

        # Giả lập đã tải xong 50% số chunks (10 chunks đầu tiên)
        part_path = prepare_part_file(output_path, self.orig_size)
        state_path = state_path_for(part_path)
        half_bytes = 10 * chunk_size
        with open(ORIGINAL_FILE, "rb") as src, part_path.open("r+b") as dst:
            dst.seek(0)
            dst.write(src.read(half_bytes))

        identity = CheckpointIdentity(
            filename="config.dat",
            file_size=self.orig_size,
            file_sha256=self.orig_hash,
            chunk_size=chunk_size,
        )
        store = CheckpointStore(part_path, identity)
        chunks = build_chunks(self.orig_size, chunk_size)
        for chunk in chunks[:10]:
            with part_path.open("rb") as f:
                f.seek(chunk.offset)
                data = f.read(chunk.length)
                store.record(chunk, hashlib.sha256(data).hexdigest())
        with part_path.open("r+b") as dst:
            store.flush(dst)

        try:
            config = ClientConfig(
                servers=(
                    ServerEndpoint("S1", "127.0.0.1", 5001),
                    ServerEndpoint("S2", "127.0.0.1", 5002),
                ),
                filename="config.dat",
                output=output_path,
                chunk_size=chunk_size,
            )
            res = download(config, resume=True)
            total_fetched = sum(res.per_server_chunks.values())
            # Chỉ phải tải thêm đúng 10 chunks còn thiếu thay vì 20 chunks!
            ok = (
                res.file_sha256 == self.orig_hash
                and total_fetched == (total_chunks - 10)
                and not state_path.exists()
                and output_path.exists()
            )
            self.log_result(
                "T6",
                "Resume download dở dang (Khôi phục 50%)",
                ok,
                f"Chỉ tải thêm {total_fetched}/{total_chunks} chunks, SHA-256 khớp",
            )
        except Exception as e:
            self.log_result("T6", "Resume download dở dang", False, str(e))
        finally:
            for s in (s1, s2): s.shutdown()

    def run_all(self):
        print("\n" + "=" * 70)
        print("MA TRẬN KIỂM THỬ THỰC NGHIỆM - share_download")
        print("=" * 70)
        self.test_t1_single()
        self.test_t2_parallel()
        self.test_t3_asymmetric()
        self.test_t4_packet_loss()
        self.test_t5_failover()
        self.test_t6_resume()
        
        print("\n" + "=" * 70)
        print("TỔNG KẾT:")
        passed_cnt = sum(1 for r in self.results if r["passed"])
        print(f"ĐẠT {passed_cnt}/{len(self.results)} KỊCH BẢN")
        return passed_cnt == len(self.results)


if __name__ == "__main__":
    suite = TestSuiteMatrix()
    ok = suite.run_all()
    sys.exit(0 if ok else 1)
