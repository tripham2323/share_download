# Thiết kế hệ thống tải tệp đa nguồn

## 1. Mục tiêu

Xây dựng bằng Python một hệ thống trong đó máy khách C1 tải đồng thời tệp `config.dat` từ ba máy chủ S1, S2 và S3. Hệ thống phải chạy được trên một máy qua `localhost`, trên các máy ảo VMware và trên nhiều máy thật chỉ bằng cách đổi cấu hình địa chỉ mạng.

Hệ thống sử dụng TCP để bảo đảm truyền byte tin cậy khi mạng mất gói. Giao thức tầng ứng dụng chịu trách nhiệm đóng khung thông điệp, lấy metadata, phân phối động các đoạn tệp giữa nhiều server, khôi phục khi server mất kết nối, ghép tệp và xác minh toàn vẹn bằng SHA-256.

## 2. Phạm vi

### Trong phạm vi

- Một tệp nguồn tên `config.dat`, kích thước dự kiến khoảng 10 MiB.
- Ba server chạy cùng một chương trình Python và chứa cùng nội dung tệp.
- Một client tải từ các server hợp lệ đang hoạt động.
- Phân phối chunk động để thích nghi với băng thông server không biết trước.
- Kiểm tra SHA-256 cho từng chunk và toàn tệp.
- Chạy qua cấu hình `localhost`, IP máy ảo hoặc IP máy thật.
- Tiếp tục tải khi một server mất kết nối nếu còn server hợp lệ.
- Hiển thị tiến độ, tốc độ và số chunk do từng server cung cấp.

### Ngoài phạm vi

- Giao diện đồ họa.
- TLS, xác thực hoặc phân quyền.
- Tự động khám phá server trong mạng LAN.
- Tải nhiều tệp trong một yêu cầu.
- Tiếp tục phiên tải sau khi client đã tắt và khởi động lại.
- Nén dữ liệu.
- Tự xây dựng lại cơ chế truyền lại gói của TCP.

## 3. Kiến trúc

### 3.1. Server

S1, S2 và S3 chạy cùng `server.py` với host, port và đường dẫn tệp được truyền qua dòng lệnh. Mỗi server:

1. Lắng nghe kết nối TCP.
2. Xử lý kết nối client trong thread riêng.
3. Trả metadata của tệp cấu hình.
4. Kiểm tra và phục vụ yêu cầu vùng byte bằng `seek(offset)`.
5. Tính SHA-256 của payload chunk trước khi gửi.
6. Chỉ phục vụ đúng tệp đã cấu hình; client không được cung cấp đường dẫn tùy ý.

Ví dụ chạy:

```text
python server.py --host 0.0.0.0 --port 5000 --file ./data/config.dat
```

### 3.2. Client

Client gồm các thành phần logic:

- **Metadata manager:** lấy và đối chiếu metadata từ mọi server.
- **Chunk scheduler:** tạo hàng đợi chunk, cấp việc và nhận lại chunk thất bại.
- **Server workers:** mỗi server có một thread và một kết nối TCP lâu dài.
- **File writer:** ghi chunk đúng offset dưới khóa đồng bộ.
- **Verifier:** kiểm tra SHA-256 từng chunk và toàn tệp.
- **Progress reporter:** hiển thị tiến độ và thống kê theo server.

Client sử dụng `queue.Queue` để không giao cùng một chunk cho hai worker. Server nhanh hoàn thành nhiều vòng lặp hơn và tự nhiên nhận nhiều chunk hơn.

## 4. Giao thức tầng ứng dụng

### 4.1. Định dạng frame

Mỗi frame TCP có định dạng:

```text
4-byte header length (unsigned, big-endian)
N-byte JSON header encoded as UTF-8
M-byte binary payload, nếu header khai báo payload
```

Bên nhận luôn dùng `recv_exact(socket, n)`; không giả định một lần `recv()` trả về trọn frame. Header và payload có giới hạn kích thước hợp lệ để tránh cấp phát tùy ý từ dữ liệu mạng.

Mọi header có trường `version` với giá trị ban đầu là `1` và trường `type` xác định loại thông điệp.

### 4.2. Metadata

Client gửi:

```json
{
  "version": 1,
  "type": "FILE_INFO_REQUEST",
  "filename": "config.dat"
}
```

Server trả về:

```json
{
  "version": 1,
  "type": "FILE_INFO_RESPONSE",
  "status": "OK",
  "filename": "config.dat",
  "file_size": 10485760,
  "file_sha256": "<sha256>"
}
```

Client nhóm server theo bộ `(filename, file_size, file_sha256)`:

- Ba phản hồi giống nhau: dùng cả ba server.
- Ba phản hồi gồm hai server giống nhau và một server khác: dùng nhóm hai server, loại server khác biệt và cảnh báo.
- Ba phản hồi đều khác nhau: dừng vì không xác định được phiên bản đúng.
- Chỉ có hai phản hồi và chúng giống nhau: dùng cả hai server.
- Chỉ có hai phản hồi nhưng chúng khác nhau: dừng vì không có căn cứ chọn phiên bản đúng.
- Chỉ có một server phản hồi: vẫn tải được từ server đó nhưng báo phiên tải không còn đa nguồn.
- Server không kết nối được hoặc trả metadata không hợp lệ: loại khỏi tập phản hồi.

### 4.3. Yêu cầu và phản hồi chunk

Client gửi:

```json
{
  "version": 1,
  "type": "GET_CHUNK",
  "filename": "config.dat",
  "chunk_id": 7,
  "offset": 1835008,
  "length": 262144
}
```

Server trả header rồi gửi payload nhị phân:

```json
{
  "version": 1,
  "type": "CHUNK_DATA",
  "status": "OK",
  "chunk_id": 7,
  "offset": 1835008,
  "length": 262144,
  "chunk_sha256": "<sha256>"
}
```

Client chỉ chấp nhận payload khi `chunk_id`, `offset`, `length` và SHA-256 đều khớp yêu cầu.

Server trả `ERROR` với mã ổn định như `FILE_NOT_FOUND`, `INVALID_RANGE`, `INVALID_REQUEST` hoặc `INTERNAL_ERROR` khi không thể phục vụ yêu cầu.

## 5. Chia tệp và lập lịch

Kích thước chunk mặc định là 256 KiB và có thể cấu hình. Với `chunk_id` bắt đầu từ 0:

```text
offset = chunk_id * chunk_size
length = min(chunk_size, file_size - offset)
```

Mỗi chunk có một trong các trạng thái:

```text
PENDING -> IN_PROGRESS -> COMPLETED
                    \-> PENDING khi tải thất bại
```

Mỗi worker lặp:

1. Lấy một chunk `PENDING` từ hàng đợi.
2. Gửi `GET_CHUNK` qua kết nối TCP đang giữ.
3. Nhận đủ header và payload.
4. Kiểm tra response và SHA-256 chunk.
5. Ghi chunk vào đúng offset trong tệp tạm.
6. Đánh dấu `COMPLETED` và lấy chunk tiếp theo.

Payload của chunk hiện tại được giữ trong RAM đến khi hash hợp lệ. Với chunk 256 KiB, ba worker chỉ cần giữ khoảng 768 KiB payload cùng lúc.

## 6. Ghi tệp và xác minh

Client tạo tệp tạm `<output>.part` với kích thước tệp đích. Vì cần tương thích Windows và Linux, thao tác `seek + write` dùng chung một `threading.Lock`.

Sau khi mọi chunk hoàn thành:

1. Đóng hoặc flush tệp tạm.
2. Tính SHA-256 toàn tệp theo kiểu streaming.
3. So sánh với hash metadata đã thống nhất.
4. Nếu khớp, đổi tên nguyên tử tệp tạm thành đường dẫn đầu ra.
5. Nếu không khớp, không tạo tệp đích hợp lệ và báo lỗi.

## 7. Xử lý lỗi

- **Timeout kết nối:** mặc định 5 giây, cấu hình được.
- **Timeout đọc:** mặc định 60 giây, cấu hình được để phù hợp đường truyền từ 100 Kbps.
- **Server mất kết nối giữa chunk:** payload dở bị bỏ, chunk trở về hàng đợi; worker thử kết nối lại tối đa `reconnect_attempts` lần, mặc định 3, rồi loại server khỏi phiên tải.
- **Server vi phạm giao thức hoặc trả dữ liệu sai:** loại server khỏi phiên tải và trả chunk về hàng đợi.
- **Còn server hợp lệ:** các worker còn lại tiếp tục tải.
- **Không còn server hợp lệ trước khi hoàn tất:** dừng, đóng tài nguyên, giữ `.part` để chẩn đoán nhưng lần chạy sau sẽ tạo lại tệp này.
- **Từ hai phản hồi metadata thành công trở lên nhưng không có nhóm chiếm đa số:** dừng thay vì đoán phiên bản đúng.

TCP chịu trách nhiệm phát hiện mất gói, sắp xếp lại byte và truyền lại. Timeout ứng dụng xử lý trường hợp kết nối hoặc server không còn tiến triển.

## 8. Cấu hình

Client đọc JSON có cấu trúc:

```json
{
  "servers": [
    {"name": "S1", "host": "127.0.0.1", "port": 5001},
    {"name": "S2", "host": "127.0.0.1", "port": 5002},
    {"name": "S3", "host": "127.0.0.1", "port": 5003}
  ],
  "filename": "config.dat",
  "output": "downloads/config.dat",
  "chunk_size": 262144,
  "connect_timeout": 5,
  "read_timeout": 60,
  "reconnect_attempts": 3
}
```

Khi chuyển sang VMware hoặc máy thật, chỉ đổi `host` và `port`; mã nguồn và giao thức giữ nguyên.

## 9. Cấu trúc mã nguồn dự kiến

```text
share_download/
├── client.py
├── server.py
├── protocol.py
├── scheduler.py
├── file_utils.py
├── client_config.example.json
├── data/
│   └── config.dat
└── tests/
    ├── test_protocol.py
    ├── test_scheduler.py
    └── test_integration.py
```

- `protocol.py`: framing, JSON header, `recv_exact` và kiểm tra giới hạn.
- `server.py`: lắng nghe, metadata và phục vụ range.
- `scheduler.py`: trạng thái và hàng đợi chunk.
- `file_utils.py`: SHA-256, tính range và ghi tệp.
- `client.py`: điều phối metadata, worker, tiến độ và hoàn tất.

## 10. Kiểm thử và tiêu chí chấp nhận

### Kiểm thử giao thức

- Header bị chia qua nhiều lần `recv()` vẫn được đọc đúng.
- Nhiều frame liền nhau không bị trộn.
- Header quá lớn, JSON sai và payload thiếu bị từ chối.

### Kiểm thử chia chunk

- Tệp rỗng.
- Tệp nhỏ hơn một chunk.
- Kích thước là bội số chính xác của chunk.
- Chunk cuối ngắn hơn chunk chuẩn.
- Các range phủ đúng toàn tệp, không chồng lấn và không có khoảng trống.

### Kiểm thử tích hợp localhost

- Ba server chạy trên ba port và cùng tham gia tải.
- SHA-256 tệp nhận bằng SHA-256 tệp nguồn.
- Server nhanh hơn nhận được nhiều chunk hơn khi có chênh lệch tốc độ đủ lớn.
- Tắt một server giữa phiên: hai server còn lại hoàn tất.
- Một server chứa tệp khác: server đó bị loại.
- Tắt mọi server trước khi hoàn tất: client báo lỗi và không xuất bản tệp đích.

### Kiểm thử nhiều máy

- S1, S2, S3 chạy cùng `server.py` trên các IP khác nhau.
- C1 kết nối bằng cấu hình IP/port mà không sửa mã nguồn.
- Giới hạn băng thông khác nhau cho các server để quan sát phân phối chunk động.
- Tệp đầu ra vượt qua kiểm tra SHA-256.

Hệ thống đạt yêu cầu khi C1 tạo được tệp có SHA-256 đúng, chứng minh đã nhận chunk từ nhiều server trong điều kiện bình thường, thích nghi với tốc độ server khác nhau và hoàn tất khi một server mất kết nối.