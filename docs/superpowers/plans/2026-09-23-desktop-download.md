# ShareDownload Desktop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Biến client tải tệp TCP đa nguồn thành desktop app PySide6 theo mockup đã duyệt, giữ CLI, cho phép hủy an toàn và tiếp tục tải các chunk đã xác minh.

**Architecture:** Lõi `client.py` phát sự kiện có kiểu dữ liệu rõ ràng cho CLI/Qt cùng dùng; worker Qt chạy tác vụ ngoài GUI thread. Giao diện Qt Widgets/QSS hiển thị dashboard, cấu hình và nhật ký. Checkpoint sidecar xác minh lại metadata lẫn byte trên đĩa trước khi bỏ qua chunk đã tải; chỉ publish file sau SHA-256 toàn tệp.

**Tech Stack:** Python 3.11+, TCP/threading/JSON/hashlib của standard library, PySide6 >=6.9,<7 chỉ cho desktop, `unittest`. Đặc tả: `docs/superpowers/specs/2026-09-23-desktop-download-design.md`.

## Global Constraints

- Demo chính Windows 11 ở 1366×768, DPI 125–150%; lõi CLI/server vẫn chạy Python 3.11+ trên Windows/Linux.
- Giữ format frame TCP, kiểm tra metadata đa số, SHA-256 từng chunk/toàn file; không viết lại transport hoặc parse stdout.
- Một phiên GUI tại một thời điểm, không để GUI thread chờ network/hash; không kết thúc thread cưỡng bức.
- Tệp đích chỉ được xuất bản sau khi hash đúng; hủy/lỗi không biến `.part` thành file kết quả.
- UI theo mockup đã duyệt: sidebar xanh đậm, nền trung tính, hero tiến độ, bốn chỉ số, bảng server, sự kiện gần đây; thông tin mạng là số liệu thật.
- Phân biệt đang tải 100% chunk với đang kiểm tra SHA-256; trạng thái không chỉ bằng màu, keyboard focus và tooltip đường dẫn dài.
- Chỉ triển khai một tệp mỗi phiên; không thêm discovery LAN, TLS, chat, web hoặc bộ cài.
- Mỗi task là một mốc kiểm chứng riêng; chỉ chạy full suite, formatter/linter sau cùng, không chạy lặp giữa các task.

## File map và hợp đồng chung

- `client.py`: giữ `download(config, progress_callback=None)` tương thích; Task 1 thêm `event_callback`, Task 4 thêm `controller`, Task 5 thêm `resume` (mặc định `False`). CLI cũ tiếp tục ghi đè output theo hành vi hiện tại; GUI hỏi rõ trước khi thay thế.
- `scheduler.py`: cho phép khởi tạo với tập chunk đã kiểm tra từ checkpoint; `total_count` vẫn là tổng toàn tệp.
- `file_utils.py`: giữ `prepare_part_file` cho chế độ tải mới; thêm đường mở `.part` hiện có không truncate trong chế độ khôi phục.
- `checkpoint.py` (mới): định nghĩa `CheckpointIdentity(filename, file_size, file_sha256, chunk_size)` độc lập với `client.py` để tránh circular import; đọc/ghi sidecar `<output>.part.state.json`, version=1, `completed` ánh xạ chunk id thành SHA-256. Ghi tệp tạm và `os.replace` có đồng bộ; không tin sidecar khi không khớp byte `.part`.
- `desktop_app.py` (mới): entry point tạo `QApplication`, chạy cửa sổ chính; `client.py` và `server.py` vẫn chạy độc lập không cần PySide6.
- `desktop/window.py` (mới): bố cục, trạng thái nút, sidebar, trang tổng quan và trang nhật ký.
- `desktop/config_form.py` (mới): nhập/xuất JSON với schema `load_config()`, trình sửa server, advanced settings và thông báo lỗi theo trường; `client.py` tách `parse_config(raw, base_dir)` để UI/CLI dùng một validation path.
- `desktop/worker.py` (mới): bridge giữa callback lõi và Qt signals; tiến độ coalesce trên GUI timer 200ms, sự kiện trạng thái/lỗi không được bỏ.
- `desktop/theme.py` (mới): palette/QSS và độ đo khoảng cách/font, không gắn logic tải.
- `desktop/summary.py` (mới ở Task 7): biểu diễn throughput/đóng góp và kết quả; chỉ dùng mẫu số liệu thật.
- `requirements-desktop.txt` (mới): `PySide6>=6.9,<7`.
- `tests/test_client.py`, `tests/test_scheduler.py`, `tests/test_resume.py` (mới): kiểm tra sự kiện, hủy, checkpoint; GUI chỉ cần smoke cửa sổ thật nếu không có edge case UI đáng giữ thành test.
- `README.md`: hướng dẫn `python -m pip install -r requirements-desktop.txt`, `python desktop_app.py`, cách chạy server/VM và ý nghĩa hủy/resume.

---

### Task 1: Sự kiện tải có cấu trúc từ lõi

**Files:** Modify `client.py:57-65,209-360`; Test `tests/test_client.py`.

**Interfaces:** `@dataclass(frozen=True, slots=True) class DownloadEvent: kind: str; server: str|None=None; completed_chunks: int=0; total_chunks: int=0; bytes_delta: int=0; message: str=''`. Task này thêm `download(config, progress_callback=None, *, event_callback: Callable[[DownloadEvent],None]|None=None)`. Task 4 thêm `controller`; Task 5 thêm `resume`; các task sau giữ nguyên callback CLI.

- [ ] **Step 1: Viết test hành vi với FileServer thật.** Thêm vào `ParallelDownloadTests` test tạo hai server, thu `events`, gọi `download(config, event_callback=events.append)`, rồi kiểm tra có `metadata`, `chunk`, `verifying`, `completed` theo thứ tự giai đoạn; tổng `bytes_delta` của sự kiện `chunk` bằng kích thước nguồn, tổng chunk hoàn thành khớp, file đích hash đúng. Giữ test CLI hiện hành.

```python
events = []
result = download(self.config_for([self.start_server("S1"), self.start_server("S2")]),
                  event_callback=events.append)
kinds = [event.kind for event in events]
self.assertLess(kinds.index("metadata"), kinds.index("chunk"))
self.assertLess(kinds.index("chunk"), kinds.index("verifying"))
self.assertLess(kinds.index("verifying"), kinds.index("completed"))
self.assertEqual(len(self.source_bytes),
                 sum(e.bytes_delta for e in events if e.kind == "chunk"))
self.assertEqual(self.source_bytes, result.output.read_bytes())
```
- [ ] **Step 2: Chạy riêng test mới để thấy chưa có `event_callback`.** `python -m unittest tests.test_client.ParallelDownloadTests.test_emits_structured_events -v` → FAIL trước thay đổi.
- [ ] **Step 3: Cài đặt tối thiểu.** Tạo `DownloadEvent`, hàm emit đồng bộ dưới khóa riêng khi hoàn thành chunk, phát `metadata` sau đồng thuận, `chunk` sau `scheduler.complete`, `verifying` trước `sha256_file`, `completed` sau publish. Worker thất bại phát `server_failed` kèm tên và lý do; khi chunk lỗi quay lại queue phát `chunk_requeued` kèm server, server bị loại phát `server_excluded`. Không giữ write lock khi gọi callback để tránh deadlock; callback không thao tác widget.

```python
@dataclass(frozen=True, slots=True)
class DownloadEvent:
    kind: str
    server: str | None = None
    completed_chunks: int = 0
    total_chunks: int = 0
    bytes_delta: int = 0
    message: str = ""
```
- [ ] **Step 4: Chạy test mới và CLI smoke thực tế** với một nguồn nhỏ và `FileServer` localhost trong script tạm; xác nhận file đích SHA-256 đúng, CLI callback vẫn nhận progress. `python -m unittest tests.test_client.ParallelDownloadTests.test_emits_structured_events -v` → PASS. Commit riêng.

### Task 2: Desktop configuration và mockup tương tác

**Files:** Create `desktop_app.py`, `desktop/__init__.py`, `desktop/config_form.py`, `desktop/theme.py`, `desktop/window.py`, `requirements-desktop.txt`; Modify `client.py`, `README.md`; Test bằng mở GUI thật.

**Interfaces:** `parse_config(raw: dict[str, Any], base_dir: Path) -> ClientConfig` tách từ `load_config(path)`, giữ nguyên validation. `ConfigForm.to_client_config() -> ClientConfig` gọi `parse_config`; `ConfigForm.load_file(path: Path)` và `save_file(path: Path)` giữ nguyên keys JSON. `MainWindow.set_state(state: str)` với các state `idle`, `checking`, `downloading`, `verifying`, `completed`, `cancelled`, `error`.

- [ ] **Step 1: Tạo dependency desktop rồi cài trong môi trường riêng.** Tạo `requirements-desktop.txt` với `PySide6>=6.9,<7`, chạy `python -m pip install -r requirements-desktop.txt`; không thêm PySide6 vào đường CLI/server. Chạy `python client.py --help` và `python server.py --help` để kiểm tra import không bị trói vào Qt.
- [ ] **Step 2: Dựng cửa sổ theo mockup đã duyệt.** Sidebar ba trang, tiêu đề/tác vụ, progress hero, bốn metrics, bảng S1/S2/S3 và activity list; cấu hình máy chủ có nút thêm/xóa, nhập tên/IP/port, file name/output chooser, nhóm advanced đóng mặc định. `desktop/theme.py` chứa QSS token cho navy, blue, neutral, green/amber/red; không dùng số liệu giả khi chạy app: trạng thái rỗng trước phiên và dấu `—` cho số liệu chưa biết.
- [ ] **Step 3: Nạp/lưu config bằng một đường validation.** `load_file` dùng `load_config`; khi lưu JSON sang file chọn, đường output tương đối tính theo file config, đường output tuyệt đối giữ nguyên. Dữ liệu nhập kiểm tra cùng giới hạn 1..65535, 1..1048576, timeout >0, tên server duy nhất. Hiện lỗi tại trường và vô hiệu Bắt đầu khi sai. Nếu output đã tồn tại, yêu cầu lựa chọn rõ `Thay thế`/`Hủy` trước khi bắt đầu; không xóa `.part` khi người dùng hủy prompt.

```python
def load_config(path: str | Path) -> ClientConfig:
    config_path = Path(path).resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON config: {exc}") from exc
    return parse_config(raw, config_path.parent)

def to_client_config(self) -> ClientConfig:
    return parse_config(self.as_json_dict(), self.config_path.parent)
```
- [ ] **Step 4: Mở UI thật** bằng `python desktop_app.py` (trên máy có display), quan sát ở 1366×768, DPI 125% và 150%: không cắt nút, trang cấu hình thêm/xóa server lưu và mở lại JSON, keyboard tab/focus hoạt động, trạng thái rỗng không có con số giả. Commit riêng.

### Task 3: Kết nối GUI với metadata và luồng tải

**Files:** Create `desktop/worker.py`; Modify `desktop/window.py`, `desktop/config_form.py`, `client.py`; Test `tests/test_client.py` nếu logic lõi đổi.

**Interfaces:** `DownloadWorker(config: ClientConfig)` có signals `status(DownloadEvent)`, `finished(DownloadResult)`, `failed(str)`; Task 4 gắn controller, Task 5 thêm `resume`. `MetadataWorker(config)` trả per-server `(ServerEndpoint, FileMetadata|None, error|None)` từ `query_metadata`, rồi gọi `select_consistent_servers` để đánh dấu khác phiên bản.

- [ ] **Step 1: Reproduce nguy cơ treo GUI.** Kết nối tới server test có đọc chậm; test/throwaway script giữ timer GUI cập nhật trong lúc metadata/download. Trước khi có worker, lời gọi đồng bộ làm timer không chạy.
- [ ] **Step 2: Đưa network/hash vào Qt worker thread.** `QObject.moveToThread(QThread)` và queued signals; mọi cập nhật widget ở main thread. `MetadataWorker` chỉ phục vụ kiểm tra trước, `download()` vẫn truy vấn lại metadata khi Bắt đầu. Khi worker kết thúc, `quit()`/`wait()` và giải phóng object; chỉ cho một phiên hoạt động.

```python
thread = QThread(window)
worker = DownloadWorker(config)
worker.moveToThread(thread)
thread.started.connect(worker.run)
worker.finished.connect(window.show_result)
worker.failed.connect(window.show_error)
worker.finished.connect(thread.quit)
worker.failed.connect(thread.quit)
thread.finished.connect(worker.deleteLater)
thread.finished.connect(thread.deleteLater)
thread.start()
```
- [ ] **Step 3: Giới hạn cập nhật 200ms.** Tích lũy `chunk` theo server và latest completed/total trong bridge, QTimer đẩy một snapshot lên bảng/progress; giữ toàn bộ sự kiện trạng thái/lỗi quan trọng. Tính tốc độ từ delta byte trong cửa sổ 5 giây đo bằng `time.monotonic()`, không dùng Kbps trung bình cả phiên làm tốc độ tức thời. Hiện `verifying` riêng trước success, hiển thị SHA-256 đầy đủ và đường dẫn khi hoàn thành.
- [ ] **Step 4: Smoke GUI với ba FileServer localhost** từ nguồn >=100 MiB: xác minh cửa sổ còn phản hồi khi đang tải, trạng thái server và progress thay đổi, file đích hash bằng nguồn. Dừng một server trong phiên để nhìn thấy failover trong activity/bảng. Commit riêng.

### Task 4: Hủy phiên nhanh và an toàn

**Files:** Modify `client.py`, `desktop/worker.py`, `desktop/window.py`; Test `tests/test_client.py`.

**Interfaces:** `DownloadController.cancel()` thread-safe; `DownloadCancelled(DownloadError)`; `download(..., controller=controller)` trả kết quả hoặc raise `DownloadCancelled`. Controller theo dõi sockets worker đang mở và đóng chúng khi hủy, để không phải chờ hết `read_timeout=60`; trạng thái cancel thắng lỗi socket phát sinh vì đóng chủ động.

- [ ] **Step 1: Viết test với server thật cố tình chặn một chunk.** Khởi chạy `download()` trong thread, chờ nhận event `chunk` đầu, gọi `controller.cancel()` khi lần đọc kế tiếp chờ; `join` trong thời hạn hữu hạn, assert `DownloadCancelled`, file đích không được publish, `.part` còn; lần chạy bình thường vẫn có hash đúng.

```python
class StallSecondChunkServer(FileServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocked = threading.Event()
        self.release = threading.Event()

    def _send_chunk(self, client, header):
        if header["chunk_id"] == 1:
            self.blocked.set()
            self.release.wait(timeout=10)
            return
        super()._send_chunk(client, header)

server = StallSecondChunkServer(self.source, "127.0.0.1", 0)
server.start()
self.addCleanup(server.shutdown)
self.addCleanup(server.release.set)
controller = DownloadController()
outcome = {}
def run():
    try:
        download(self.config_for([ServerEndpoint("S1", *server.address)]),
                 controller=controller)
    except DownloadCancelled as exc:
        outcome["cancelled"] = exc
thread = threading.Thread(target=run)
thread.start()
self.assertTrue(server.blocked.wait(timeout=3))
controller.cancel()
thread.join(timeout=3)
self.assertFalse(thread.is_alive())
self.assertIn("cancelled", outcome)
self.assertFalse((self.root / "downloaded.dat").exists())
```
- [ ] **Step 2: Chạy riêng test mới, xác nhận fail trước thay đổi.** `python -m unittest tests.test_client.ParallelDownloadTests.test_cancel_during_blocked_read -v`.
- [ ] **Step 3: Cài đặt controller và trạng thái UI.** Check cancel trước/sau metadata, trước acquire, sau recv, trước verify/publish; close sockets đang đăng ký ngoài khóa controller, worker trả chunk chưa hoàn thành về scheduler; join mọi worker và đóng file trước trả về. Nút Hủy chuyển thành `Đang dừng…`, khóa Start; đóng cửa sổ hỏi xác nhận và chỉ thoát sau worker xong, không gọi `terminate()`. Việc dừng kết nối đang `create_connection()` tối đa chịu connect timeout cấu hình; GUI vẫn phản hồi.
- [ ] **Step 4: Chạy test hủy và smoke UI** với server đọc chậm, xác minh trạng thái hủy khác lỗi, không có file đích/worker treo, có thể tải lại. Commit riêng.

### Task 5: Checkpoint bền vững và khôi phục chunk

**Files:** Create `checkpoint.py`, `tests/test_resume.py`; Modify `scheduler.py`, `file_utils.py`, `client.py`, `.gitignore` (thêm `*.part.state.json` và tệp sidecar tạm); Test `tests/test_scheduler.py`.

**Interfaces:** `@dataclass(frozen=True) class CheckpointIdentity: filename: str; file_size: int; file_sha256: str; chunk_size: int`. `load_checkpoint(part_path: Path, identity: CheckpointIdentity, chunks: list[Chunk]) -> dict[int,str]` chỉ trả các id có hash byte đúng; `CheckpointStore.record(chunk: Chunk, digest: str)`, `flush(target)` đồng bộ byte rồi atomic replace JSON, `close(target)` flush nốt. `ChunkScheduler(chunks, completed_ids: frozenset[int]=frozenset())` chỉ enqueue PENDING. Không thay đổi đường tải mặc định không resume.

- [ ] **Step 1: Viết test biên khó bằng TCP thật.** Dùng server đếm `GET_CHUNK`, hủy sau một số chunk rồi chạy `download(..., resume=True)`: assert chỉ yêu cầu chunk thiếu; sửa một byte ở chunk đã checkpoint rồi chạy lại và assert chunk đó được yêu cầu lại; đổi metadata nguồn thì nhận `ResumeConflict`; corrupt JSON/thiếu `.part` không công bố file sai; file rỗng và crash trước commit vẫn có hash đúng.

```python
source = self.source_bytes
part_path = self.root / "downloaded.dat.part"
part_path.write_bytes(source)
chunks = build_chunks(len(source), 16_384)
identity = CheckpointIdentity("config.dat", len(source), sha256(source).hexdigest(), 16_384)
store = CheckpointStore(part_path, identity)
store.record(chunks[0], sha256(source[:16_384]).hexdigest())
with part_path.open("r+b") as target:
    store.flush(target)
self.assertEqual({0}, set(load_checkpoint(part_path, identity, chunks)))
with part_path.open("r+b") as target:
    target.seek(0)
    target.write(b"x")
self.assertEqual({}, load_checkpoint(part_path, identity, chunks))
```
- [ ] **Step 2: Chạy riêng `python -m unittest tests.test_resume -v` và xác nhận fail trước thay đổi.** Không sửa test chỉ để làm pass.
- [ ] **Step 3: Cài đặt format v1 và commit tối đa 16 chunk/lần.** Check header/indices/hash shape/kích thước `.part`, hash lại từng vùng đã ghi; clone/ghi bản sidecar vào `*.tmp`, flush+fsync sidecar rồi `os.replace`. Trước sidecar commit, flush+fsync `.part`; khi hủy/lỗi/hoàn tất flush batch còn chờ. Không khóa file ghi và callback cùng lúc. Trên resume, mở `.part` `r+b` thay vì gọi `prepare_part_file()`; kiểm tra `scheduler.total_count`/`completed_count` bao gồm các chunk đã khôi phục.

```python
def flush(self, target):
    target.flush()
    os.fsync(target.fileno())
    payload = {"version": 1, "filename": self.identity.filename,
               "file_size": self.identity.file_size,
               "file_sha256": self.identity.file_sha256,
               "chunk_size": self.identity.chunk_size, "completed": self.completed}
    with self.temp_path.open("w", encoding="utf-8") as sidecar:
        json.dump(payload, sidecar, sort_keys=True)
        sidecar.flush()
        os.fsync(sidecar.fileno())
    os.replace(self.temp_path, self.state_path)
```
- [ ] **Step 4: Giải quyết dữ liệu cũ không khớp an toàn.** Lõi báo lý do `ResumeConflict` thay vì âm thầm truncate `.part`; GUI cho người dùng chọn `Tải lại từ đầu` hoặc `Giữ dữ liệu và hủy`. CLI chỉ tải lại từ đầu nếu người dùng chạy mode mặc định; khi `resume=True` thì trả lỗi rõ. Một phiên GUI và một output đang hoạt động không trùng nhau. Giữ tương thích CLI mặc định.
- [ ] **Step 5: Chạy `python -m unittest tests.test_resume -v` và smoke lại kịch bản hủy → đóng app → mở app → tiếp tục**, đối chiếu SHA-256 và số `GET_CHUNK` thật. Commit riêng.

### Task 6: UX tiếp tục tải và lỗi có hướng xử lý

**Files:** Modify `client.py`, `desktop/window.py`, `desktop/worker.py`, `desktop/config_form.py`, `README.md`; Test bằng UI thật.

**Interfaces:** `idle -> checking -> downloading -> verifying -> completed`; nhánh `cancelled`/`error` giữ `.part`. `MainWindow.show_resume_banner(verified_chunks: int, total_chunks: int)` và `show_resume_conflict(reason: str, keep_part: bool)` chỉ được gọi từ GUI thread. Phát hiện checkpoint hợp lệ sau truy vấn metadata ngoài GUI thread, nhưng chỉ gọi `download(resume=True)` khi người dùng nhấn `Tiếp tục tải`; khi không có checkpoint và `.part` thì tải mới.

- [ ] **Step 1: Hiển thị banner tiếp tục phiên** gồm tên/kích thước file, % chunk đã xác minh, server khả dụng; nút `Tiếp tục tải` và `Tải lại từ đầu` (hành động thứ hai hỏi xác nhận vì bỏ checkpoint). Chỉ chạy `load_checkpoint` ngoài GUI thread; thanh tiến độ mở đầu từ số chunk xác minh, không về 0 sai.

```python
if checkpoint_state == "valid":
    window.show_resume_banner(verified_chunks, total_chunks)
elif checkpoint_state == "conflict":
    window.show_resume_conflict(reason, keep_part=True)
# Start download(resume=True) only from the user's \"Tiếp tục tải\" action.
```
- [ ] **Step 2: Map lỗi và đường khắc phục rõ.** Mở rộng `DownloadError` bằng `code: str = "DOWNLOAD_ERROR"` mà không đổi `str(exc)` cho CLI; gán `NO_METADATA`, `METADATA_CONFLICT`, `ALL_SERVERS_FAILED`, `HASH_MISMATCH`, `RESUME_CONFLICT` tại các điểm raise hiện có, bắt `OSError`/`errno.ENOSPC` làm `DISK_FULL`. `NO_METADATA` → kiểm tra IP/port/firewall; `METADATA_CONFLICT` → đồng bộ file server; `ALL_SERVERS_FAILED` → giữ `.part` và thử lại; `HASH_MISMATCH` → không publish và báo kiểm tra nguồn; `DISK_FULL` → dọn chỗ trống. Không so khớp text exception.
- [ ] **Step 3: Smoke GUI theo ba kịch bản** mất S3 nhưng phiên xong, hủy rồi mở lại thấy nút tiếp tục và hash đúng, đổi file server dẫn tới prompt không ghi đè dữ liệu cũ. Không hiển thị thành công trước publish. Commit riêng.

### Task 7: Báo cáo mạng và nghiệm thu giao diện

**Files:** Create `desktop/summary.py`; Modify `desktop/window.py`, `README.md`; Test không bắt buộc nếu chỉ là biểu diễn dữ liệu đã có.

**Interfaces:** Nhận `DownloadEvent` và `DownloadResult`; bảng đóng góp mỗi server (bytes, chunks, tốc độ quan sát được), timeline disconnect/reassign; biểu đồ tốc độ từ mẫu đo thật mỗi giây, tự vẽ QWidget bằng QPainter. Nếu chỉ có một mẫu thì hiển thị `Không đủ mẫu để vẽ` thay vì nội suy giả.

- [ ] **Step 1: Xây màn tổng kết** ghi file size, SHA-256, server đã dùng/loại, số chunk, lượng byte, thời gian và đồ thị tốc độ từ samples; file nhỏ tải quá nhanh thì hiển thị `Không đủ mẫu để vẽ`, không tự tạo đường giả. Có nút sao chép SHA-256 và mở thư mục kết quả.
- [ ] **Step 2: Gắn Nhật ký phiên** hiển thị timestamp và sự kiện metadata, server rớt, reassign, xác minh; giới hạn bộ nhớ hiển thị khi tải file lớn, nhưng giữ trạng thái quan trọng. Tên server, mã lỗi và trạng thái là text ngoài màu.
- [ ] **Step 3: Nghiệm thu trên cửa sổ thật** ở 1366×768/DPI 125%, 150%: tải, error, verify, success, resume, server mất kết nối; kiểm tra contrast chữ QSS >=4.5:1, keyboard tab và tooltip đường dẫn dài. Chụp màn hình so với mockup và sửa chỗ lệch về hierarchy/spacing.
- [ ] **Step 4: Cập nhật README** phần desktop, cách demo ba VM, script setup, kiểm tra hash và giải thích ứng dụng dùng TCP/metadata/chunk failover/checkpoint. Chạy `python -m unittest discover -s tests -v` một lần sau toàn bộ task; chạy GUI và CLI smoke thực tế; bỏ script tạm. Commit riêng.

## Review trước khi bắt tay code

1. Rủi ro: hiện tại `download()` ghi đè `.part` ngay đầu và `os.replace()` output. Task 5 phải tránh truncate khi resume; UI phải hỏi trước khi thay thế output, nhưng đường CLI mặc định giữ tương thích.
2. Rủi ro: `read_timeout=60` và `worker.join()` hiện có thể làm đóng app rất chậm; task 4 đóng socket chủ động, đợi worker ngoài GUI thread.
3. Rủi ro: số liệu mockup chỉ là ví dụ. App thực tế chỉ báo tốc độ theo mẫu nhận được và status từ lõi, không mặc định giả là 3 server online.
4. Rủi ro: manifest ghi quá thường xuyên sẽ làm tải lớn chậm; commit nhóm tối đa 16 chunk, nhưng mọi chunk vẫn được hash lại khi resume.
5. Nếu PySide6 chưa được cài, CLI/server không được lỗi import; GUI phải có thông báo cài dependency rõ từ `desktop_app.py`.
