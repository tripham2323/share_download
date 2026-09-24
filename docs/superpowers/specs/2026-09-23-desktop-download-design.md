# Thiết kế ứng dụng desktop tải tệp đa nguồn

## Mục tiêu và phạm vi

Tạo ứng dụng desktop Python/PySide6 trên máy client C1 để cấu hình và giám sát một phiên tải tệp từ các server TCP S1/S2/S3. Giữ giao thức, thuật toán phân phối chunk, kiểm tra SHA-256 và CLI hiện có; PySide6 chỉ là dependency của entry point desktop. Windows là môi trường demo chính; đường tải lõi vẫn hỗ trợ Python 3.11+ trên Windows/Linux. Không yêu cầu chạy server qua UI trong đợt này: server.py vẫn chạy trên từng máy/VM.

Bản desktop có ba năng lực: (1) cấu hình và quan sát phiên tải; (2) hủy phiên an toàn; (3) khôi phục các chunk đã xác minh sau khi đóng/mở lại ứng dụng. Mỗi năng lực là một mốc chạy được độc lập, triển khai tuần tự. Không thêm chat, khám phá LAN, dashboard web, TLS, tải nhiều tệp cùng lúc hoặc đóng gói bộ cài trong phạm vi này.

## Kiến trúc

- `client.py` giữ `ClientConfig`, `download()` và `DownloadResult`; mở rộng lõi bằng tín hiệu sự kiện cấu trúc, yêu cầu hủy và chế độ khôi phục mà CLI và GUI cùng sử dụng. Không phân tích stdout của CLI và không viết lại giao thức TCP.
- Một adapter Qt chạy tác vụ tải trong `QThread`/worker object, chuyển sự kiện và kết quả/lỗi thành Qt signals. Chỉ luồng GUI được cập nhật widget. Đóng cửa sổ lúc đang tải phải yêu cầu hủy, chờ worker dừng rồi mới thoát; không hủy thread cưỡng bức.
- UI gồm màn hình cấu hình (danh sách server tên/IP/port, tên tệp, đường lưu và tùy chọn nâng cao), màn hình phiên tải (tiến độ, số byte, tốc độ phiên và từng server, số chunk, trạng thái kết nối, log sự kiện), và trạng thái kết thúc (SHA-256, đường dẫn file, lý do lỗi). Không cần màn hình server điều khiển từ xa.
- Cấu hình hiện tại vẫn đọc được từ `client_config.example.json`/`client_config.json`. Lưu qua GUI bằng schema JSON hiện hành; việc chọn đường lưu phải nhất quán với quy tắc `load_config()` giải quyết đường dẫn tương đối theo vị trí tệp cấu hình. Cấu hình không được chứa checkpoint hoặc thống kê runtime.

## Ngôn ngữ thiết kế và tiêu chí UX doanh nghiệp

Giao diện desktop dùng **Qt Widgets với QSS nhẹ**, theo mockup dashboard đã được duyệt: nền trung tính sáng, thanh điều hướng xanh đậm, một màu nhấn xanh lam, typography rõ cấp bậc, khoảng trắng nhất quán, thẻ thông tin có viền nhẹ và trạng thái server có cả nhãn chữ lẫn màu. Thanh điều hướng có **Tổng quan phiên tải**, **Cấu hình máy chủ** và **Nhật ký phiên**; trang tổng quan có thanh tác vụ, hero tiến độ, bốn chỉ số, bảng đóng góp từng server và hoạt động gần đây. Cấu hình nâng cao được thu gọn để người dùng mới không phải hiểu chunk/timeout trước khi tải. Không lạm dụng gradient, đổ bóng hoặc biểu đồ giả.

Đường thao tác chính nhìn thấy ngay: chọn cấu hình hoặc dùng cấu hình đã nạp, chọn nơi lưu, **Kiểm tra server**, **Bắt đầu tải**. Khi tải, chỉ còn hành động hợp lệ (**Hủy tải**); trạng thái chờ metadata, đang tải, đang kiểm tra SHA-256, hoàn tất, bị hủy và lỗi phải phân biệt rõ. Mỗi thông báo lỗi nêu vấn đề và hành động khắc phục (ví dụ kiểm tra IP/port, đồng bộ nội dung server, thử lại); log chi tiết có thể mở rộng nhưng không thay thế thông báo chính. Giao diện không khóa khi mạng chậm, không nhảy bố cục khi số server thay đổi, và giữ số liệu cuối phiên để người dùng đọc sau khi hoàn tất.

Tiêu chí chấp nhận trực quan: cửa sổ sử dụng được ở độ phân giải 1366×768 và màn hình DPI 125–150%; không cắt mất nút/tên server/đường dẫn (dùng co giãn, cuộn hoặc rút gọn có tooltip khi cần); tab order và focus hiển thị rõ, nhãn và trạng thái không chỉ dựa vào màu; màu chữ và nền của các thành phần tùy biến đạt tỷ lệ tương phản tối thiểu 4.5:1 cho chữ thường. Có empty state khi chưa cấu hình, loading state khi truy vấn, trạng thái mất server và success state sau hash; nút nguy hiểm có xác nhận theo ngữ cảnh. Bản đầu ưu tiên giao diện sáng nhất quán; dark mode, animation và icon pack ngoài Qt không nằm trong phạm vi.

## Luồng tương tác

1. Người dùng mở ứng dụng, nạp/sửa cấu hình, thêm/xóa server và chọn nơi lưu. UI kiểm tra giá trị với cùng quy tắc lõi: server hợp lệ và tên duy nhất, port hợp lệ, chunk từ 1 byte đến 1 MiB, timeout dương. Nút bắt đầu vô hiệu khi cấu hình chưa hợp lệ hoặc đang có phiên.
2. Người dùng có thể kiểm tra kết nối/metadata trước khi tải bằng các lời gọi TCP hiện có, ngoài luồng UI. Hiện trạng thái từng server: kết nối được, không phản hồi, hoặc khác phiên bản; quy tắc chọn đa số vẫn như `select_consistent_servers()`. Kết quả kiểm tra trước chỉ để xem, không thay thế việc lấy lại metadata ngay trước phiên tải.
3. Bắt đầu phiên: nhận metadata và quyết định nhóm server, xử lý checkpoint nếu có, phân phối chunk chưa hoàn tất. Sự kiện cấu trúc gồm giai đoạn, tiến độ chunk/byte, thống kê từng server, server bị loại/rớt và lý do, giai đoạn xác minh SHA-256, kết quả. UI cập nhật có giới hạn tần suất để không bắn một signal/widget paint cho mọi chunk của file rất lớn; không đổi các bảo đảm của worker TCP.
4. Hủy: UI gửi yêu cầu hủy tới lõi, không chặn GUI. Worker dừng lấy chunk mới, kết thúc/đóng socket và tài nguyên đang dùng, không publish file đích. Phần dữ liệu đã xác minh vẫn còn trong `.part` và checkpoint cho lần sau; nút bắt đầu chỉ hoạt động trở lại sau khi worker kết thúc. Khi server rớt, các server còn lại tiếp tục như hiện tại; nếu tất cả rớt, UI báo lỗi và vẫn giữ dữ liệu có thể khôi phục.
5. Tải xong: xác minh SHA-256 toàn tệp rồi mới `os.replace` `.part` thành file đích; xóa checkpoint sau publish thành công. UI phân biệt tải 100% chunk với xác minh hash đang chạy; chỉ báo thành công sau publish. Với tệp rỗng, tiến độ và xác minh vẫn kết thúc đúng.

## Checkpoint và tính toàn vẹn

Checkpoint sidecar gắn với `<output>.part`, gồm phiên bản định dạng, tên/kích thước/SHA-256 file nguồn, kích thước chunk và danh sách chunk đã hoàn thành cùng SHA-256 từng chunk. Không lưu dữ liệu nhị phân trong JSON; ghi sidecar theo lối atomic replace và không cho ghi đồng thời từ nhiều worker. Gom tối đa 16 chunk thành một lần commit: đồng bộ byte `.part` xuống đĩa trước, rồi ghi sidecar mới và atomic replace; khi hủy hoặc hết phiên phải commit các chunk đã ghi còn chờ. Crash trước commit chỉ làm tải lại tối đa 16 chunk, không nhận dữ liệu chưa được ghi bền là hợp lệ. Không bao giờ tin chunk chỉ vì có byte trong `.part`.

Khi mở lại, luôn lấy metadata từ server theo quy tắc đồng thuận trước. Chỉ dùng checkpoint nếu metadata, chunk size và kích thước `.part` trùng. Đọc và hash lại từng chunk ghi trong checkpoint trước khi bỏ qua tải; chunk sai hoặc thiếu bị đưa lại hàng đợi. Nếu checkpoint thiếu, hỏng định dạng, khác phiên bản nguồn hoặc `.part` thiếu/sai kích thước, không tự truncate dữ liệu cũ: báo xung đột và cho người dùng chọn giữ lại hoặc tải lại từ đầu. Không ghi đè output đã tồn tại khi người dùng chưa chọn hành động thay thế rõ ràng. UI chỉ cho một phiên tải tại một thời điểm; không cam kết khóa liên tiến trình với CLI chạy đồng thời. CLI và GUI dùng cùng quy tắc checkpoint nếu chạy download với chế độ khôi phục; đường CLI cũ mặc định vẫn giữ hành vi tương thích nếu chưa chọn chế độ đó.

## Lộ trình ưu tiên

1. **Desktop quan sát được**: Qt app, quản lý cấu hình, kiểm tra metadata, tải ngoài luồng, tiến độ và trạng thái theo server, kết quả SHA-256/lỗi; giữ CLI. Đây là mốc demo tối thiểu.
2. **Hủy an toàn**: nút hủy, thoát cửa sổ đúng quy trình, giữ dữ liệu chưa publish.
3. **Tiếp tục phiên**: checkpoint theo chunk, kiểm tra dữ liệu trên đĩa và metadata trước resume, chứng minh chỉ yêu cầu lại chunk thiếu. Đây là phần nâng cấp mạng có giá trị cao nhất nhưng phức tạp hơn UI.
4. **Minh họa bài toán mạng**: đồ thị tốc độ/đóng góp theo server từ sự kiện sẵn có, trạng thái failover và báo cáo cuối phiên; không tạo giao thức đo riêng. Mốc này chỉ triển khai sau ba mốc trên.

## Lỗi và đánh giá

Lỗi nhập cấu hình hiển thị ngay tại trường liên quan. Lỗi mạng/khác metadata/mất toàn bộ server, hết chỗ đĩa, hash sai hoặc checkpoint không hợp lệ có thông báo rõ trạng thái và đường dẫn `.part` nếu còn; không được hiện thành công giả. Không chạy `query_metadata`, hash tệp lớn hoặc `download()` trên GUI thread.

Xác minh bằng server TCP thật trên localhost hoặc VMware: tải từ ít nhất hai server và đối chiếu SHA-256; dừng một server khi tải để quan sát chunk được chuyển; hủy giữa chừng và mở lại để thấy chunk đã xác minh không tải lại; thay đổi file nguồn/bit trong `.part` để xác nhận checkpoint không bị tin nhầm. Kiểm tra CLI hiện hành vẫn tải được. Kiểm tra UI trực quan trong cửa sổ thật, gồm trạng thái không phản hồi và lúc xác minh hash. Những ca rủi ro cao (hủy, checkpoint hỏng, source đổi, bit flip, failover) cần kiểm thử hành vi lõi; không viết test chỉ khẳng định nội dung widget hoặc text nguồn.
