GOOGLE MINESWEEPER AUTOPILOT (PROTOTYPE)
========================================

Dành ưu tiên cho Google Dò mìn chạy trên Chrome/Edge ở Windows 10/11.
Tool chụp vùng màn hình đã chọn và giả lập click chuột; không đọc dữ liệu riêng bên trong trang web.

YÊU CẦU
-------
- Python 3.10+ (khuyến nghị Python 3.11/3.12)
- Windows 10/11
- mss: chụp vùng màn hình nhanh hơn trên Windows (tự dùng ImageGrab dự phòng nếu MSS không hoạt động)
- Tesseract OCR tùy chọn, chỉ dùng làm phương án dự phòng; bộ nhận diện màu Google hoạt động không cần OCR
- Google Minesweeper trên trình duyệt, bàn cờ hiển thị rõ và không bị cửa sổ che

CÀI ĐẶT
-------
Mở PowerShell trong thư mục này và chạy:
  python -m pip install -r requirements.txt

Tesseract là tùy chọn. Nếu cài để dùng OCR dự phòng, hãy đảm bảo lệnh tesseract chạy được trong PATH.

CHẠY
----
Chạy run.bat hoặc:
  python minesweeper_autopilot.py

CÁCH DÙNG
---------
1. Mở Google Minesweeper trên Chrome/Edge và chọn độ khó trong game.
2. Trong tool, chọn đúng mức độ trước:
   - Dễ: 10 cột × 8 hàng, 10 mìn
   - Trung bình: 18 cột × 14 hàng, 40 mìn
   - Khó: 24 cột × 20 hàng, 99 mìn
   - Tùy chỉnh: tự nhập tổng mìn, hàng và cột.
   Đây là kích thước lưới của giao diện Google Minesweeper; nếu bàn cờ trên màn hình của bạn khác, dùng Tùy chỉnh.
3. Bấm "Kéo chọn vùng" và chỉ kéo sát vùng lưới ô xanh/be. Không lấy thanh điểm, bộ đếm hoặc menu phía trên.
4. Bấm "Quét thử" trước. Kiểm tra từng hàng trong log: ■ = ô chưa mở, F = cờ, 0 = ô trống, 1..8 = số, ? = chưa nhận diện được.
5. Chỉ bấm "Bắt đầu tự chơi" khi kết quả quét khớp ảnh và không có '?'. Khi bắt đầu, tool tự thu nhỏ để không che game rồi chạy tự động; không cần bấm mỗi lượt.
6. Bot ưu tiên suy luận chắc chắn. Nếu bật "Cho phép đoán", khi hết nước chắc chắn bot chọn ô có nguy cơ ước lượng thấp nhất; vẫn có thể thua.
7. Tùy chỉnh "Tốc độ mỗi nước (ms)": nhập 0 để bật TURBO. TURBO không chèn thêm thời gian chờ trước lô nước tiếp theo; nó bắt đầu polling ngay và chỉ đợi đến khi ảnh/hoạt ảnh thực sự ổn định. Từ cùng một ảnh, bot gom các nước chắc chắn thành một lô, sắp xếp đường đi của chuột theo ô gần nhất để giảm quãng rê, rồi quét/giải lại sau lô đó. Chuột vẫn di chuyển có làm mượt; thời gian hoạt ảnh của Google và xử lý click của trình duyệt vẫn không thể bỏ qua. Nhận diện ô độc lập sử dụng tối đa 12 luồng; phần suy luận logic dùng bitmask để tăng tốc các phép tập con, còn xác suất đoán dùng DFS có cắt nhánh. Bot ghi thời gian suy luận mỗi lô vào log. Khi xác minh bị lỗi ở TURBO, thời gian chờ thử lại được tính từ độ trễ đo thực tế (không lặp lại liên tục ở 0 ms). Ô chưa rõ được đọc lại tối đa 5 lần; nếu vẫn mơ hồ, tool cách ly ô đó như một ô ẩn tiềm năng, không click nó, nhưng tiếp tục suy luận những ô khác. Nếu sau đó ảnh trở nên rõ thì ô tự được đưa lại vào bộ suy luận. Chụp ảnh dùng MSS nếu cài được, chỉ tính xác suất đoán khi thật sự cần.
8. Nhấn F6 ở bất kỳ cửa sổ nào để dừng khẩn cấp toàn cục trên Windows. Nút "DỪNG" cũng dừng bot. Khi tác vụ kết thúc, cửa sổ tool tự hiện lại để xem nhật ký.
9. Nút "ĐI TIẾP" chỉ dùng khi bạn tắt chế độ đoán và bot đang chờ vì không còn nước chắc chắn. Bot không chủ động dừng sau khi đã cắm đủ số cờ; nó vẫn tiếp tục mở các ô an toàn.

LƯU Ý
-----
- Đây là nguyên mẫu, chưa được kiểm thử trực tiếp trên mọi độ phân giải, mức zoom hoặc biến thể giao diện. Thread pool nhận diện ô được giới hạn ở tối đa 12 worker; các click chuột cố ý chạy tuần tự để tránh tranh chấp thiết bị đầu vào.
- Bộ nhận diện dùng màu RGB/HSV của Google trước, OCR là phương án dự phòng. Luôn kiểm tra Quét thử trước khi bật tự chơi.
- Sau lệnh click, tool polling ảnh ngay trong TURBO (không thêm độ trễ cố định), theo dõi hoạt ảnh qua nhiều khung hình và chỉ suy luận lại khi ảnh ổn định. Khi xác minh bị lỗi, TURBO dùng khoảng chờ thích nghi từ độ trễ đo được. Nếu một ô vẫn không đọc được sau 5 lần, ô đó được cách ly và không bị click; solver xem nó là ô ẩn tiềm năng để tránh suy luận quá lạc quan, đồng thời tiếp tục phần còn lại của bàn cờ. Nếu trạng thái sau đó đọc rõ, ô tự được bỏ cách ly.
- Nếu log có '?', nhận diện số/cờ không đúng, hoặc tool báo mâu thuẫn suy luận, đừng cố cho chạy tiếp: kiểm tra đúng mức độ, vùng chọn và mức zoom.
- Tự động hóa bằng click giả lập chỉ dùng ở trò chơi cá nhân và nơi việc tự động hóa được cho phép.
