# CẨM NANG BẢO VỆ ĐỀ TÀI: TRÌNH TẢI FILE ĐA NGUỒN (MULTI-SOURCE DOWNLOADER)

Tài liệu này tổng hợp toàn bộ bản chất kỹ thuật, cấu trúc hệ thống và cách trả lời các câu hỏi hóc búa nhất (Problem & Solution) mà hội đồng/người phỏng vấn có thể hỏi bạn.

---

## 1. BẢN CHẤT ĐỀ TÀI LÀ GÌ?
Đây là một ứng dụng **Mạng máy tính (Computer Networking)** hoạt động ở tầng Ứng dụng (Application Layer), sử dụng giao thức giao vận **TCP** (tầng Transport). 
- **Mục tiêu:** Tăng tốc độ tải file và tăng tính sẵn sàng (Fault Tolerance) bằng cách tải đồng thời các mảnh (chunks) của một file từ nhiều Server khác nhau.
- **Mô hình:** Client-Server kết hợp cơ chế Cân bằng tải động (Dynamic Load Balancing) ở phía Client.

---

## 2. CÁC "PROBLEM" THỰC TẾ & "CODE GIẢI PHÁP" CỦA DỰ ÁN

Khi bảo vệ đề tài, việc hiểu tại sao code lại viết như vậy quan trọng hơn là học thuộc code. Dưới đây là các bài toán kinh điển hệ thống đã giải quyết:

### Problem 1: TCP là luồng byte (Byte Stream), làm sao phân biệt được đâu là thông tin (Header) đâu là dữ liệu file (Payload)?
- **Bản chất:** TCP không có khái niệm "gói tin" ranh giới rõ ràng. Gửi 1000 bytes có thể nhận thành 2 lần (300 và 700 bytes).
- **Code Giải pháp (`protocol.py`):** 
  - Tự định nghĩa một **Giao thức đóng gói (Framing)**. 
  - Mỗi thông điệp gửi đi bao gồm: `4 bytes (chứa độ dài Header)` + `N bytes (chuỗi JSON Header)` + `Payload nhị phân`.
  - Hàm `recv_exact()` sử dụng vòng lặp `while` liên tục gọi `sock.recv()` cho đến khi nhận ĐỦ số byte cần thiết, chống lại hiện tượng phân mảnh (fragmentation) của TCP.

### Problem 2: Làm sao để nhiều luồng (Threads) ghi vào cùng 1 file mà không bị lỗi/chồng chéo dữ liệu?
- **Bản chất:** Nếu Thread 1 và Thread 2 cùng mở 1 file `.part` và thi nhau ghi, dữ liệu sẽ bị hỏng hoàn toàn.
- **Code Giải pháp (`client.py`):**
  - Mở file `.part` 1 lần duy nhất ở chế độ `r+b`.
  - Sử dụng **Thread Lock (`threading.Lock`)** khi ghi đĩa. 
  - Mỗi khi một Thread tải xong 1 chunk, nó phải xin `Lock`, di chuyển con trỏ chuột đến đúng vị trí (`target.seek(chunk.offset)`), ghi dữ liệu (`target.write`), rồi mới nhả Lock ra cho luồng khác.

### Problem 3: Nếu một Server đang gửi dữ liệu thì bị sập (Failover/Rớt mạng), làm sao tải tiếp mà không lỗi?
- **Bản chất:** Cần cơ chế chịu lỗi (Fault Tolerance).
- **Code Giải pháp (`scheduler.py` & `client.py`):**
  - Quản lý các mảnh file bằng hàng đợi an toàn `ChunkScheduler`.
  - Khi Thread đang lấy `chunk_id = 5` mà mạng đứt (bắt được `socket.error` hoặc `timeout`), nó gọi `scheduler.retry(chunk)`. Chunk 5 lập tức bị đẩy ngược lại vào hàng đợi `_queue` chuyển trạng thái về `PENDING`.
  - Các Thread của Server khác (nhanh hơn, ổn định hơn) sẽ tự động bốc Chunk 5 ra để tải bù.

### Problem 4: Làm sao biết file tải về là chuẩn, không bị sửa đổi hay chứa mã độc?
- **Bản chất:** Tính toàn vẹn dữ liệu (Data Integrity).
- **Code Giải pháp:** 
  - Sử dụng thuật toán băm **SHA-256**.
  - Xác thực 2 lớp: 
    1. Trẻ nhỏ (Per-chunk): Mỗi mảnh tải về (`GET_CHUNK`), Server gửi kèm `chunk_sha256`. Client băm payload nhận được, nếu sai -> Xóa, đưa chunk vào hàng đợi tải lại.
    2. Toàn cục (Whole-file): Khi tải xong 100%, Client quét toàn bộ file `.part` để băm ra SHA-256 tổng, đem so sánh với metadata ban đầu. Khớp mới đổi tên thành file thật.

### Problem 5: Lỡ 3 Server chứa 3 file có nội dung khác nhau nhưng trùng tên thì sao?
- **Bản chất:** Sự thống nhất dữ liệu đa nguồn (Consensus).
- **Code Giải pháp (`client.py - select_consistent_servers`):**
  - Client không cắm đầu tải ngay. Đầu tiên, nó gửi `FILE_INFO_REQUEST` cho tất cả Server.
  - Gom nhóm các Server trả về cùng một Hash và Size.
  - Chỉ chọn nhóm có **số lượng Server đông nhất (Majority Consensus)** để tải. Tránh việc tải râu ông nọ cắm cằm bà kia gây hỏng file.

### Problem 6: Resume Download - Đang tải 50% tắt máy, hôm sau tải tiếp thế nào?
- **Bản chất:** Cần lưu trạng thái độc lập.
- **Code Giải pháp:**
  - File `.part` chứa dữ liệu nhị phân đã ghi.
  - File `.state` (JSON) chứa danh sách các `chunk_id` đã ghi thành công.
  - Lúc khởi động, nếu hash trên server KHÔNG ĐỔI, `ChunkScheduler` sẽ nạp `.state` và gán các chunk đó thành `COMPLETED` ngay từ đầu, bỏ qua chúng và chỉ nhét các chunk chưa tải vào hàng đợi.

---

## 3. CÁC CÂU HỎI MỞ RỘNG (Q&A) THƯỜNG GẶP

**Hỏi: Tại sao dùng Python Threading bị dính GIL (Global Interpreter Lock) mà tốc độ vẫn nhanh?**
> **Đáp:** GIL chỉ làm chậm các tác vụ tính toán nặng (CPU-bound). Ứng dụng này chủ yếu là tải mạng và ghi ổ cứng (I/O-bound). Khi Python gọi `sock.recv()` hoặc `file.write()`, nó sẽ **nhả GIL** ra, cho phép các Thread khác chạy song song thoải mái.

**Hỏi: Hàm `seek()` trong quá trình ghi đĩa có làm chậm hệ thống ổ cứng (HDD) không?**
> **Đáp:** Có thể gây phân mảnh trên ổ HDD cũ do đầu từ phải nhảy liên tục. Nhưng hệ điều hành hiện đại có bộ đệm file (Page Cache / OS Cache), nên việc `seek` và `write` thực chất là ghi vào RAM trước khi flush xuống ổ cứng. Với SSD thì hoàn toàn không có độ trễ cơ học nên tác động gần như bằng 0.

**Hỏi: Nếu file 100GB thì RAM có bị tràn không?**
> **Đáp:** Hoàn toàn không. Mỗi Thread chỉ xin cấp phát 1 bộ đệm RAM bằng đúng `chunk_size` (mặc định 256KB - 1MB). Tải xong chunk nào ghi ngay xuống đĩa chunk đó, không bao giờ nạp toàn bộ file vào RAM.

**Hỏi: Thiết kế kiến trúc dạng này giống với công nghệ nào thực tế?**
> **Đáp:** Giống với cơ chế tải chia nhỏ của **IDM (Internet Download Manager)**, giao thức **BitTorrent** (tải từng piece dựa trên SHA-1/SHA-256), hoặc kiến trúc tải file tĩnh trên các mạng **CDN (Content Delivery Network)**.
