"""
Module RateLimiter: Triển khai thuật toán Token Bucket để bóp băng thông mạng (Traffic Shaping).

Nguyên lý:
- Token được bơm vào xô (bucket) với tốc độ cố định (rate bytes/giây).
- Khi có dữ liệu truyền qua, phải tiêu thụ số token tương ứng với số bytes.
- Nếu không đủ token, luồng truyền phải sleep để chờ token mới được bơm vào.
"""

import time
import threading


class TokenBucketLimiter:
    def __init__(self, rate_bps: int):
        """
        Args:
            rate_bps: Tốc độ tối đa tính bằng bits per second (ví dụ: 1000_000 cho 1000 Kbps).
        """
        self.rate_bytes_per_sec = rate_bps / 8.0  # Đổi bits -> bytes
        self.capacity = max(self.rate_bytes_per_sec * 0.1, 4096)  # Dung tích xô (burst)
        self.tokens = self.capacity
        self.last_update = time.time()
        self.lock = threading.Lock()

    def limit(self, num_bytes: int):
        """Chặn luồng (sleep) nếu truyền vượt quá tốc độ cho phép."""
        if self.rate_bytes_per_sec <= 0:
            return

        with self.lock:
            now = time.time()
            elapsed = now - self.last_update
            self.last_update = now

            # Bơm thêm token tương ứng với thời gian trôi qua
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_bytes_per_sec)

            if self.tokens < num_bytes:
                # Thiếu token -> tính thời gian cần chờ để bơm đủ token
                needed = num_bytes - self.tokens
                sleep_time = needed / self.rate_bytes_per_sec
                self.tokens = 0
            else:
                self.tokens -= num_bytes
                sleep_time = 0

        if sleep_time > 0:
            time.sleep(sleep_time)
