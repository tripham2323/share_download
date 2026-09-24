"""
Live Visual Terminal Dashboard for Multi-source File Downloader.
Renders real-time per-server progress, overall progress, and chunk allocation map.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Dict, List, Optional

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


class VisualDashboard:
    """Flicker-free ANSI terminal dashboard for multi-source downloading."""

    # ANSI Colors
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"
    BG_DARK = "\033[40m"

    SERVER_COLORS = [GREEN, CYAN, YELLOW, MAGENTA, BLUE]

    def __init__(self, filename: str, file_size: int, total_chunks: int, server_names: List[str]):
        self.filename = filename
        self.file_size = file_size
        self.total_chunks = total_chunks
        self.server_names = server_names
        
        self.server_color_map = {
            name: self.SERVER_COLORS[i % len(self.SERVER_COLORS)]
            for i, name in enumerate(server_names)
        }

        self._lock = threading.Lock()
        self.start_time = time.monotonic()
        self.last_render_time = 0.0

        # Stats per server
        self.server_chunks: Dict[str, int] = {s: 0 for s in server_names}
        self.server_bytes: Dict[str, int] = {s: 0 for s in server_names}
        self.server_speed: Dict[str, float] = {s: 0.0 for s in server_names}
        self.server_active: Dict[str, bool] = {s: True for s in server_names}

        # Chunk status: 0=PENDING, 1=DOWNLOADING, 2=DONE, 3=FAILED
        self.chunk_state: List[str] = ["PENDING"] * total_chunks
        self.chunk_owner: List[Optional[str]] = [None] * total_chunks

        self.retries = 0
        self.sha256_status = "Pending..."
        self.completed_count = 0
        self.is_finished = False

    def setup_metadata(self, filename: str, file_size: int, total_chunks: int, server_names: List[str]):
        with self._lock:
            self.filename = filename
            self.file_size = file_size
            self.total_chunks = total_chunks
            self.server_names = server_names
            self.server_color_map = {
                name: self.SERVER_COLORS[i % len(self.SERVER_COLORS)]
                for i, name in enumerate(server_names)
            }
            self.server_chunks = {s: 0 for s in server_names}
            self.server_bytes = {s: 0 for s in server_names}
            self.server_speed = {s: 0.0 for s in server_names}
            self.server_active = {s: True for s in server_names}
            self.chunk_state = ["PENDING"] * total_chunks
            self.chunk_owner = [None] * total_chunks

        self.retries = 0
        self.sha256_status = "Pending..."
        self.completed_count = 0
        self.is_finished = False

        # Clear screen once at beginning
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

    def on_chunk_start(self, server: str, chunk_id: int):
        with self._lock:
            if 0 <= chunk_id < self.total_chunks:
                self.chunk_state[chunk_id] = "DOWNLOADING"
                self.chunk_owner[chunk_id] = server
        self.render()

    def on_chunk_complete(self, server: str, chunk_id: int, chunk_bytes: int):
        with self._lock:
            if 0 <= chunk_id < self.total_chunks:
                self.chunk_state[chunk_id] = "DONE"
                self.chunk_owner[chunk_id] = server
            self.server_chunks[server] = self.server_chunks.get(server, 0) + 1
            self.server_bytes[server] = self.server_bytes.get(server, 0) + chunk_bytes
            self.completed_count += 1

            # Calculate speed
            elapsed = max(time.monotonic() - self.start_time, 0.001)
            self.server_speed[server] = (self.server_bytes[server] * 8 / 1000) / elapsed
        self.render()

    def on_chunk_retry(self, server: str, chunk_id: int):
        with self._lock:
            if 0 <= chunk_id < self.total_chunks:
                self.chunk_state[chunk_id] = "PENDING"
                self.chunk_owner[chunk_id] = None
            self.retries += 1
        self.render()

    def on_server_fail(self, server: str):
        with self._lock:
            self.server_active[server] = False
        self.render()

    def set_sha256_status(self, status: str):
        with self._lock:
            self.sha256_status = status
        self.render()

    def finish(self, final_hash: str = ""):
        with self._lock:
            self.is_finished = True
            if final_hash:
                self.sha256_status = f"{self.GREEN}VERIFIED (✓ {final_hash[:16]}...){self.RESET}"
        self.render(force=True)
        print("\n" + "=" * 70)
        print(f" {self.GREEN}{self.BOLD}[✓] DOWNLOAD SUCCESSFUL!{self.RESET} File saved to downloads folder.")
        print("=" * 70 + "\n")

    def _format_size(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.2f} KiB"
        return f"{size / (1024 * 1024):.2f} MiB"

    def _make_bar(self, percent: float, width: int = 24, filled_char: str = "█", empty_char: str = "░") -> str:
        filled_len = int(round(width * percent / 100))
        return (filled_char * filled_len) + (empty_char * (width - filled_len))

    def render(self, force: bool = False):
        now = time.monotonic()
        # Rate limit rendering to max ~20 FPS unless forced or finished
        if not force and not self.is_finished and (now - self.last_render_time) < 0.05:
            return
        self.last_render_time = now

        with self._lock:
            lines = []
            lines.append("=" * 72)
            lines.append(f" {self.BOLD}{self.CYAN}MULTI-SERVER CONCURRENT DOWNLOADER (ĐỀ TÀI SỐ 3){self.RESET}")
            lines.append("=" * 72)

            file_str = self._format_size(self.file_size)
            lines.append(f" File: {self.BOLD}{self.filename}{self.RESET} | Size: {file_str} | Chunks: {self.total_chunks}")
            lines.append("-" * 72)

            # Per-server progress
            lines.append(f" {self.BOLD}SERVERS ACTIVITY:{self.RESET}")
            for sname in self.server_names:
                chunks = self.server_chunks.get(sname, 0)
                pct = (chunks / self.total_chunks * 100) if self.total_chunks else 0
                bar = self._make_bar(pct, width=20)
                color = self.server_color_map.get(sname, self.RESET)
                status = f"{self.GREEN}ACTIVE{self.RESET}" if self.server_active.get(sname, True) else f"{self.RED}OFFLINE{self.RESET}"
                speed = self.server_speed.get(sname, 0.0)
                lines.append(
                    f"  {color}{sname:<4}{self.RESET} [{color}{bar}{self.RESET}] "
                    f"{pct:5.1f}%  {speed:7.1f} Kbps ({chunks:2d} chunks) [{status}]"
                )

            lines.append("-" * 72)

            # Overall progress
            overall_pct = (self.completed_count / self.total_chunks * 100) if self.total_chunks else 100.0
            overall_bar = self._make_bar(overall_pct, width=28)
            elapsed = max(now - self.start_time, 0.001)
            eta_str = "--:--"
            if self.completed_count > 0 and self.completed_count < self.total_chunks:
                sec_left = int((self.total_chunks - self.completed_count) / (self.completed_count / elapsed))
                eta_str = f"{sec_left // 60:02d}:{sec_left % 60:02d}"
            elif self.completed_count == self.total_chunks:
                eta_str = "00:00"

            lines.append(f" {self.BOLD}OVERALL PROGRESS:{self.RESET}")
            lines.append(
                f"  [{self.GREEN}{overall_bar}{self.RESET}] {overall_pct:5.1f}% "
                f"({self.completed_count}/{self.total_chunks} chunks | ETA: {eta_str})"
            )
            active_count = sum(1 for a in self.server_active.values() if a)
            lines.append(
                f"  Verified Chunks: {self.completed_count}/{self.total_chunks} | "
                f"Retries: {self.RED if self.retries else ''}{self.retries}{self.RESET} | "
                f"Active Servers: {active_count}/{len(self.server_names)}"
            )

            lines.append("-" * 72)

            # Real-time chunk allocation map
            lines.append(f" {self.BOLD}REAL-TIME CHUNK MAP (Xem từng mảnh dữ liệu gán cho ai):{self.RESET}")
            chunk_tokens = []
            for cid in range(self.total_chunks):
                st = self.chunk_state[cid]
                owner = self.chunk_owner[cid]
                if st == "DONE":
                    color = self.server_color_map.get(owner, self.GREEN)
                    # Label with last digit of server name (e.g. S1 -> 1, S2 -> 2)
                    lbl = owner[-1] if owner else "✓"
                    chunk_tokens.append(f"{color}[{lbl}]{self.RESET}")
                elif st == "DOWNLOADING":
                    color = self.server_color_map.get(owner, self.YELLOW)
                    chunk_tokens.append(f"{color}[~]{self.RESET}")
                else: # PENDING
                    chunk_tokens.append(f"{self.DIM}[.]{self.RESET}")

            # Print chunks in rows of 20
            for r in range(0, len(chunk_tokens), 20):
                row_str = "  " + "".join(chunk_tokens[r:r+20])
                lines.append(row_str)

            # Legend
            legend_parts = []
            for sname in self.server_names:
                color = self.server_color_map.get(sname, self.RESET)
                lbl = sname[-1]
                legend_parts.append(f"{color}[{lbl}]={sname}{self.RESET}")
            legend_parts.append(f"{self.YELLOW}[~]=Tải{self.RESET}")
            legend_parts.append(f"{self.DIM}[.]=Đợi{self.RESET}")
            lines.append("  Chú thích: " + "  ".join(legend_parts))

            lines.append("-" * 72)
            lines.append(f" {self.BOLD}DATA INTEGRITY (SHA-256):{self.RESET} {self.sha256_status}")
            lines.append("=" * 72)

            output = "\n".join(lines)

            if sys.stdout.isatty():
                # Move cursor to top without flicker
                sys.stdout.write("\033[H" + output)
                sys.stdout.flush()
            else:
                # Fallback for non-interactive output (in milestone snapshots)
                should_print = force or self.is_finished
                if not should_print and self.total_chunks > 0:
                    step = max(self.total_chunks // 4, 1)
                    if self.completed_count > 0 and self.completed_count % step == 0:
                        if getattr(self, "_last_logged_count", -1) != self.completed_count:
                            self._last_logged_count = self.completed_count
                            should_print = True
                if should_print:
                    print(output + "\n")
