# KỊCH BẢN KIỂM THỬ THỰC TẾ (MANUAL TEST PLAN / DEMO SCRIPT)

Đây là kịch bản từng bước (step-by-step) để bạn tự test bằng tay hoặc dùng để **Demo trực tiếp trước Hội đồng**. Mỗi kịch bản đều nhằm chứng minh một điểm mạnh cốt lõi của hệ thống.

---

## 🏗️ Chuẩn bị Môi trường (Chạy trước mỗi kịch bản)
1. Mở **3 cửa sổ Terminal** (gọi là T1, T2, T3) để chạy 3 Server.
2. Mở **1 cửa sổ Terminal** (gọi là TC) để chạy Client.

Lệnh chạy Server (mở sẵn ở T1, T2, T3):
```powershell
# T1
python server.py --host 127.0.0.1 --port 5001 --file data/config.dat
# T2
python server.py --host 127.0.0.1 --port 5002 --file data/config.dat
# T3
python server.py --host 127.0.0.1 --port 5003 --file data/config.dat
```

---

## 🎯 Kịch bản 1: Đường trường thuận lợi (Happy Path)
**Mục đích:** Chứng minh Client có khả năng chia nhỏ công việc và tải song song từ nhiều nguồn để tăng tốc độ.

**Thực hiện:**
1. Đảm bảo 3 Server đang chạy.
2. Tại TC (Terminal Client), chạy lệnh:
   ```powershell
   python client.py --config client_config.json
   ```
**Quan sát kết quả:**
- Giao diện Progress Bar chạy trơn tru, hiển thị `[==========>] 100%`.
- Khi kết thúc, bảng thống kê hiện ra: Cả `S1`, `S2`, `S3` đều chia nhau gánh số lượng chunks gần bằng nhau (VD: 13, 13, 14).
- Dòng cuối in ra: `SHA-256 verified: ...` chứng minh ghép file thành công.

---

## 💥 Kịch bản 2: Khả năng sinh tồn khi Server "Sập" (Failover)
**Mục đích:** Chứng minh nếu 1 Server bị mất điện/sập mạng giữa chừng, Client không bị crash mà tự động phân bổ lại chunk bị rớt cho các Server khác gánh.

**Thực hiện:**
1. Khởi động lại 3 Server.
2. Tại TC, chạy lệnh tải:
   ```powershell
   python client.py --config client_config.json
   ```
3. **Ngay lập tức**, khi thanh tiến trình đang chạy, bạn chuyển sang cửa sổ T3 và bấm **`Ctrl + C`** để ép Server 3 chết hẳn!
**Quan sát kết quả:**
- Client có thể hơi khựng lại 1-2 giây (do Timeout).
- Sau đó, thanh tiến trình tiếp tục chạy.
- Kết quả cuối cùng: File vẫn tải thành công 100%. Bảng tổng kết sẽ cho thấy S1 và S2 gánh nhiều chunks hơn hẳn, trong khi S3 gánh rất ít (do sập sớm).

---

## 🔄 Kịch bản 3: Phục hồi tải dở dang (Resume Download)
**Mục đích:** Chứng minh Client có lưu tiến độ. Bị ngắt mạng thì tải tiếp chứ không mất công tải lại từ đầu.

**Thực hiện:**
1. Khởi động 3 Server bình thường.
2. Xoá file trong thư mục `downloads` (nếu có).
3. Tại TC, chạy lệnh tải:
   ```powershell
   python client.py --config client_config.json
   ```
4. **HÀNH ĐỘNG NHANH:** Khi thanh Progress Bar chạy đến khoảng `30% - 50%`, ngay lập tức bấm **`Ctrl + C`** tại cửa sổ Client (TC) để tắt ngúm quá trình tải.
5. Xem trong thư mục `downloads` sẽ thấy 2 file: `config.dat.part` và `config.dat.state`.
6. Chạy lại lệnh Client 1 lần nữa:
   ```powershell
   python client.py --config client_config.json
   ```
**Quan sát kết quả:**
- Thanh tiến trình **KHÔNG** bắt đầu từ `0%`. Nó sẽ nhảy ngay lập tức lên mức `30% - 50%` (mức lúc nãy vừa ngắt) và tải tiếp tục phần còn lại cho đến `100%`.
- SHA-256 ở cuối vẫn báo `verified` chuẩn xác!

---

## 🕵️ Kịch bản 4: Chống dữ liệu giả mạo (Consensus / File Mismatch)
**Mục đích:** Đề phòng trường hợp 1 Server bị hacker đổi file hoặc file bị cũ. Client phải thông minh phát hiện ra và từ chối tải từ Server đó.

**Thực hiện:**
1. Tắt Server 3 (`Ctrl + C` ở T3).
2. Tạo 1 file khác với nội dung bất kỳ và lưu thành `data/fake_config.dat` (hoặc mở `config.dat` copy ra file mới, thêm/xóa vài chữ cho dung lượng bị lệch).
3. Bật lại Server 3 nhưng trỏ vào file giả:
   ```powershell
   python server.py --host 127.0.0.1 --port 5003 --file data/fake_config.dat
   ```
4. Đảm bảo T1 và T2 vẫn đang host file `config.dat` xịn.
5. Chạy Client:
   ```powershell
   python client.py --config client_config.json
   ```
**Quan sát kết quả:**
- Client lấy metadata (SHA-256, Size) từ cả 3 server, thấy S1 và S2 giống nhau, S3 khác biệt.
- Theo nguyên tắc "số đông" (Majority Consensus), Client phớt lờ S3.
- Kết quả cuối cùng: In ra `Warning: unused servers: S3`. Chỉ có S1 và S2 gánh tải. File gốc vẫn an toàn!

---

## 🚀 Kịch bản 5 (Tuỳ chọn): Trải nghiệm tốc độ với File Lớn
**Mục đích:** Nhìn rõ tốc độ MB/s và tính toán ETA của thanh Progress Bar, chứng minh hệ thống không bị tràn RAM khi xử lý file cực to.

**Thực hiện:**
1. Dùng PowerShell tạo nhanh 1 file rác nặng 500MB (Gõ ở Terminal):
   ```powershell
   fsutil file createnew data/large_file.dat 524288000
   ```
2. Mở file `client_config.json`, đổi tên file thành `"filename": "large_file.dat"`.
3. Khởi động 3 Server, trỏ `--file` vào `data/large_file.dat`.
4. Chạy Client và tận hưởng giao diện thanh tiến trình xịn xò như IDM!
