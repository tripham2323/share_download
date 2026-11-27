# RFC: ĐẶC TẢ GIAO THỨC TẢI FILE ĐA NGUỒN (SHARE_DOWNLOAD PROTOCOL v1)

- **Tên giao thức:** SDP (Share Download Protocol)
- **Phiên bản:** `1.0`
- **Tầng mạng:** Tầng Ứng dụng (Application Layer)
- **Giao thức giao vận:** TCP (Transmission Control Protocol)
- **Mã hóa chuỗi:** UTF-8
- **Byte Order:** Big-Endian (Network Byte Order)

---

## 1. TỔNG QUAN (OVERVIEW)

Giao thức **SDP v1** được thiết kế để phục vụ việc phân phối và tải các khối dữ liệu (chunks) của một tệp tin lớn đồng thời từ nhiều máy chủ TCP. Giao thức tối ưu hóa cho độ tin cậy, tính toàn vẹn dữ liệu và khả năng chịu lỗi (fault-tolerance) trên các đường truyền mạng có độ trễ và mất gói.

---

## 2. ĐÓNG KHUNG THÔNG ĐIỆP (MESSAGE FRAMING)

Do TCP là giao thức hướng luồng (Byte Stream), SDP cài đặt cơ chế đóng khung **Length-Prefixed Framing** để phân định ranh giới giữa các thông điệp:

```text
+-----------------------+-----------------------+-----------------------+
|  Header Length (4B)   |   JSON Header (N B)   |  Binary Payload (M B) |
|  Big-Endian uint32    |      UTF-8 Text       |     Raw File Bytes    |
+-----------------------+-----------------------+-----------------------+
```

### Chi tiết các trường:
1. **Header Length (4 bytes):** Một số nguyên không dấu 32-bit (`uint32`) biểu diễn độ dài bằng byte của phần JSON Header ngay sau đó.
2. **JSON Header (N bytes):** Chuỗi JSON được mã hóa UTF-8 chứa các trường siêu dữ liệu (metadata), chỉ dẫn và mã băm.
3. **Binary Payload (M bytes):** Luồng byte nhị phân. `payload_length` do `send_frame` ghi vào JSON header; với `CHUNK_DATA`, giá trị này bằng `length` của mảnh, còn với yêu cầu `GET_CHUNK` thì bằng `0`.

> **Chống phân mảnh (De-fragmentation):** Bên nhận bắt buộc phải sử dụng vòng lặp `recv_exact(length)` để tích lũy đủ $4$ bytes header length, $N$ bytes JSON, và $M$ bytes payload, không phụ thuộc vào bộ đệm của tầng socket.

---

## 3. DANH MỤC THÔNG ĐIỆP (MESSAGE DEFINITIONS)

### 3.1. Truy vấn thông tin tệp (FILE_INFO)

Dùng để lấy metadata trước khi tải. C1 chọn nhóm cùng metadata chiếm quá nửa số phản hồi hợp lệ; nếu chỉ có một phản hồi hợp lệ, nó vẫn có thể tải. Đây không phải giao thức đồng thuận ghi.

#### A. Request: `FILE_INFO_REQUEST`
- **Người gửi:** Client ➔ Server
- **Payload:** Không có ($M = 0$)
- **JSON Header:**
  ```json
  {
    "version": 1,
    "type": "FILE_INFO_REQUEST",
    "filename": "config.dat"
  }
  ```

#### B. Response: `FILE_INFO_RESPONSE`
- **Người gửi:** Server ➔ Client
- **Payload:** Không có ($M = 0$)
- **JSON Header:**
  ```json
  {
    "version": 1,
    "type": "FILE_INFO_RESPONSE",
    "status": "OK",
    "filename": "config.dat",
    "file_size": 10485760,
    "file_sha256": "6f73c163176f29f2c9763c8bd0c30c4185041bd31cded1b8e0bd3fe6856bfd65"
  }
  ```

---

### 3.2. Yêu cầu tải mảnh tệp (CHUNK_DATA)

Dùng để tải một đoạn byte cụ thể trong tệp tin.

#### A. Request: `GET_CHUNK`
- **Người gửi:** Client ➔ Server
- **Payload:** Không có ($M = 0$)
- **JSON Header:**
  ```json
  {
    "version": 1,
    "type": "GET_CHUNK",
    "filename": "config.dat",
    "chunk_id": 5,
    "offset": 1310720,
    "length": 262144
  }
  ```

#### B. Response: `CHUNK_DATA`
- **Người gửi:** Server ➔ Client
- **Payload:** $M = 262144$ bytes nhị phân
- **JSON Header:**
  ```json
  {
    "version": 1,
    "type": "CHUNK_DATA",
    "status": "OK",
    "chunk_id": 5,
    "offset": 1310720,
    "length": 262144,
    "chunk_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
  ```

---

### 3.3. Thông báo lỗi (ERROR)

Khi server gặp sự cố hoặc client gửi yêu cầu không hợp lệ.

- **Người gửi:** Server ➔ Client
- **Payload:** Không có ($M = 0$)
- **JSON Header:**
  ```json
  {
    "version": 1,
    "type": "ERROR",
    "status": "ERROR",
    "code": "INVALID_RANGE",
    "message": "requested range is outside the file"
  }
  ```

### 3.4. Danh mục tệp (LIST_FILES / FILE_LIST)

`LIST_FILES` không mang payload. `after` là tên tệp cuối của trang trước; bỏ qua với trang đầu.
Server trả `FILE_LIST` với `status: "OK"`, `files` (tối đa 40 mục gồm `filename`, `file_size`
và `file_sha256`) và `next_after` (`null` ở trang cuối). Danh mục chỉ liệt kê tệp đã công bố
trên node đó. Client đời cũ có thể bỏ qua thao tác này và yêu cầu metadata trực tiếp.

---

## 4. BẢNG MÃ LỖI CHUẨN (STANDARD ERROR CODES)

| Mã lỗi | Ý nghĩa | Hành động của Client |
| :--- | :--- | :--- |
| `FILE_NOT_FOUND` | Server không chứa tệp được yêu cầu | Loại server khỏi danh sách hoạt động |
| `INVALID_RANGE` | Offset hoặc độ dài vượt phạm vi tệp | Đánh dấu lỗi nguồn cho yêu cầu đó |
| `INVALID_REQUEST` | Thao tác hoặc tham số không được hỗ trợ | Kiểm tra lại yêu cầu; frame sai định dạng có thể bị đóng kết nối |
| `INTERNAL_ERROR` | Lỗi đọc đĩa I/O hoặc lỗi nội bộ server | Retry mảnh tệp trên server khác |

---

## 5. CƠ CHẾ XÁC THỰC TOÀN VẸN (DATA INTEGRITY)

Giao thức áp dụng nguyên tắc **Zero-Trust Transfer** với 2 tầng mã băm:
1. **Per-chunk Integrity:** Mọi gói tin `CHUNK_DATA` đều chứa `chunk_sha256`. Client ngay sau khi nhận đủ byte payload sẽ băm SHA-256 lại. Nếu không khớp $\implies$ Hủy gói tin ngay lập tức, chuyển trạng thái chunk thành `PENDING` để thử lại.
2. **Whole-file Integrity:** Sau khi ghép đủ $N$ chunks vào file `.part`, Client băm lại toàn bộ file trên đĩa và so sánh với `file_sha256` ban đầu. Chỉ khi khớp 100% mới đổi tên thành tệp chính thức.

---

## 6. KHẢ NĂNG TƯƠNG THÍCH VÀ MỞ RỘNG (EXTENSIBILITY)
- Các trường mở rộng trong tương lai có thể được bổ sung vào JSON Header mà không làm hỏng tính tương thích ngược (Backward Compatibility).
- Giới hạn header là 64 KiB và chunk là 1 MiB. `version` trên dây là số nguyên `1`.
- SDP không cấp quyền ghi. Phát hành, danh mục đồng bộ và xác nhận bản sao đi qua kênh HTTPS riêng trên S1; S2/S3 xác minh SHA-256 trước khi xác nhận. Lời mời theo node không được đưa vào hồ sơ công khai của C1.
