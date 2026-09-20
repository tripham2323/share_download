# Share Download

Chương trình tải đồng thời một tệp từ nhiều máy chủ bằng Python và TCP. Máy khách chia tệp thành các chunk, phân phối chunk động cho S1, S2, S3, ghép dữ liệu theo đúng offset và xác minh SHA-256 trước khi công bố tệp kết quả.

## Tính năng

- Tải `config.dat` đồng thời từ nhiều server.
- Tự thích nghi với băng thông khác nhau: server nhanh hoàn thành nhiều chunk hơn.
- TCP xử lý mất gói, sắp xếp lại dữ liệu và truyền lại.
- SHA-256 cho từng chunk và toàn bộ tệp.
- Tiếp tục tải bằng các server còn lại khi một server ngắt kết nối.
- Phát hiện server chứa phiên bản file khác.
- Chạy được trên localhost, VMware và nhiều máy thật.
- Chỉ sử dụng Python standard library, không cần cài package ngoài.

## Yêu cầu

- Python 3.11 trở lên.
- Các server phải chứa tệp có cùng tên `config.dat`.
- Khi chạy trên nhiều máy, client phải kết nối được tới địa chỉ IP và TCP port của từng server.

Kiểm tra Python:

```bash
python --version
```

## Kiến trúc

```text
                        TCP
                 ┌───────────── S1
                 │
C1 + Chunk Queue ├───────────── S2
                 │
                 └───────────── S3
```

Client thực hiện:

1. Lấy metadata từ các server: tên file, kích thước và SHA-256.
2. Chọn nhóm server có cùng phiên bản file.
3. Chia file thành các chunk, mặc định 256 KiB.
4. Chạy một worker TCP cho mỗi server.
5. Ghi chunk vào đúng offset trong file `.part`.
6. Tính SHA-256 toàn file.
7. Chỉ đổi tên `.part` thành file đích khi hash hợp lệ.

Mỗi thông điệp TCP gồm:

```text
4 byte độ dài header | JSON header | binary payload
```

## Cấu trúc dự án

```text
share_download/
├── client.py
├── server.py
├── protocol.py
├── scheduler.py
├── file_utils.py
├── client_config.example.json
├── docs/
│   └── superpowers/
│       ├── specs/
│       └── plans/
└── tests/
```

- `server.py`: phục vụ metadata và vùng byte của file.
- `client.py`: tải đồng thời, lập lịch chunk, kiểm tra hash và giao diện CLI.
- `protocol.py`: đóng khung thông điệp TCP.
- `scheduler.py`: quản lý trạng thái và hàng đợi chunk.
- `file_utils.py`: chia chunk, tính SHA-256 và quản lý file tạm.

# Chạy trên một máy

## 1. Chuẩn bị file

Tạo thư mục `data` và đặt file vào đó:

```text
data/config.dat
```

Ba server localhost có thể cùng đọc file này.

## 2. Chạy ba server

Mở ba terminal trong thư mục dự án.

### Server S1

```bash
python server.py --host 127.0.0.1 --port 5001 --file data/config.dat
```

### Server S2

```bash
python server.py --host 127.0.0.1 --port 5002 --file data/config.dat
```

### Server S3

```bash
python server.py --host 127.0.0.1 --port 5003 --file data/config.dat
```

Kết quả trên mỗi server có dạng:

```text
Serving config.dat on 127.0.0.1:5001
```

## 3. Tạo cấu hình client

Trên Windows PowerShell:

```powershell
Copy-Item client_config.example.json client_config.json
```

Trên Linux:

```bash
cp client_config.example.json client_config.json
```

Cấu hình localhost:

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

Các thuộc tính:

| Thuộc tính | Ý nghĩa |
|---|---|
| `servers` | Danh sách tên, IP và port của các server |
| `filename` | Tên file cần tải; không phải đường dẫn |
| `output` | Đường dẫn file đầu ra, tính tương đối từ vị trí file cấu hình |
| `chunk_size` | Kích thước chunk, từ 1 byte đến 1 MiB |
| `connect_timeout` | Số giây chờ thiết lập kết nối |
| `read_timeout` | Số giây tối đa chờ dữ liệu trên socket |
| `reconnect_attempts` | Số lần kết nối lại khi server mất kết nối |

## 4. Chạy client

```bash
python client.py --config client_config.json
```

Kết quả mẫu:

```text
Connecting:
  S1 127.0.0.1:5001
  S2 127.0.0.1:5002
  S3 127.0.0.1:5003
Progress: 40/40 (100.0%)
File size: 10.00 MiB
  S1: 14 chunks, 3670016 bytes, 5200.0 Kbps
  S2: 13 chunks, 3407872 bytes, 4800.0 Kbps
  S3: 13 chunks, 3407872 bytes, 4700.0 Kbps
SHA-256 verified: aecf...
Saved: .../downloads/config.dat
```

# Chạy trên VMware hoặc nhiều máy thật

Ví dụ mô hình mạng:

| Máy | IP | Port |
|---|---|---|
| S1 | `192.168.10.11` | `5000` |
| S2 | `192.168.10.12` | `5000` |
| S3 | `192.168.10.13` | `5000` |
| C1 | `192.168.10.20` | — |

## 1. Cấu hình mạng

Các máy phải nằm trong cùng mạng và liên lạc được với nhau. Với VMware có thể dùng:

- **Host-only** để demo nội bộ trên máy cá nhân.
- **Bridged** để VM xuất hiện như một máy riêng trong LAN.
- **NAT** nếu đã cấu hình để các VM kết nối trực tiếp được với nhau.

Từ C1, kiểm tra:

```bash
ping 192.168.10.11
ping 192.168.10.12
ping 192.168.10.13
```

## 2. Chạy server

Trên mỗi S1, S2, S3:

1. Clone repository.
2. Đặt cùng nội dung `config.dat` vào `data/config.dat`.
3. Chạy server trên mọi network interface.

```bash
python server.py --host 0.0.0.0 --port 5000 --file data/config.dat
```

`0.0.0.0` chỉ dùng ở phía server. Client phải sử dụng IP thật của từng server.

Nếu thay đổi `config.dat` khi server đang chạy, phải khởi động lại server để tính lại metadata và SHA-256.

## 3. Mở firewall

Windows PowerShell với quyền Administrator:

```powershell
New-NetFirewallRule `
  -DisplayName "Share Download TCP 5000" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 5000 `
  -Action Allow
```

Linux dùng UFW:

```bash
sudo ufw allow 5000/tcp
```

## 4. Cấu hình client C1

```json
{
  "servers": [
    {"name": "S1", "host": "192.168.10.11", "port": 5000},
    {"name": "S2", "host": "192.168.10.12", "port": 5000},
    {"name": "S3", "host": "192.168.10.13", "port": 5000}
  ],
  "filename": "config.dat",
  "output": "downloads/config.dat",
  "chunk_size": 262144,
  "connect_timeout": 5,
  "read_timeout": 60,
  "reconnect_attempts": 3
}
```

Chạy:

```bash
python client.py --config client_config.json
```

# Quy tắc chọn server

- Ba server có cùng metadata và hash: dùng cả ba.
- Hai server giống nhau, một server khác: dùng hai server giống nhau và cảnh báo server bị loại.
- Hai server phản hồi nhưng hash khác nhau: dừng vì không có căn cứ chọn bản đúng.
- Một server hợp lệ: vẫn tải được nhưng không còn tải đa nguồn.
- Không server hợp lệ: báo lỗi và không tạo file đích.

# Kiểm tra SHA-256 thủ công

Windows:

```powershell
Get-FileHash data/config.dat -Algorithm SHA256
Get-FileHash downloads/config.dat -Algorithm SHA256
```

Linux:

```bash
sha256sum data/config.dat
sha256sum downloads/config.dat
```

Hash nguồn và file tải về phải giống nhau.

# Thử khả năng chịu lỗi

1. Khởi động ba server và client.
2. Trong lúc client tải, dừng một server bằng `Ctrl+C`.
3. Hai server còn lại tiếp tục xử lý các chunk chưa hoàn thành.
4. Client chỉ xuất bản file khi SHA-256 toàn file hợp lệ.

Với file 10 MiB trên localhost, quá trình có thể kết thúc rất nhanh. Khi demo có thể dùng file lớn hơn hoặc giới hạn băng thông VM để dễ quan sát.

# Chạy kiểm thử

```bash
python -m unittest discover -s tests -v
```

Xem trợ giúp CLI:

```bash
python server.py --help
python client.py --help
```

# Xử lý lỗi

## `Connection refused`

Kiểm tra:

- Server đã chạy chưa.
- IP và port trong cấu hình có đúng không.
- Firewall có cho phép TCP port không.
- Khi chạy nhiều máy, server có bind `0.0.0.0` không.

## `no valid metadata responses`

Client không nhận được metadata hợp lệ từ server nào. Kiểm tra kết nối, tên file và firewall.

## `no metadata majority`

Các server đang chứa nhiều phiên bản `config.dat` khác nhau. Sao chép lại cùng một file và khởi động lại server.

## `all usable servers failed before download completed`

Mọi server hợp lệ đã mất kết nối hoặc hết số lần kết nối lại.

## Còn file `.part`

Phiên tải chưa hoàn tất hoặc SHA-256 toàn file không hợp lệ. File `.part` không phải file kết quả hoàn chỉnh.

# Quy trình Git đề xuất

Cập nhật nhánh chính:

```bash
git switch main
git pull origin main
```

Tạo nhánh cho thay đổi mới:

```bash
git switch -c feature/ten-tinh-nang
```

Commit và push:

```bash
git add .
git commit -m "feat: mô tả thay đổi"
git push -u origin feature/ten-tinh-nang
```

Sau đó tạo Pull Request trên GitHub và merge vào `main` sau khi kiểm tra.