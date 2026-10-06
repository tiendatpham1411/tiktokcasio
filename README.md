# FebonOS TikTok Cloud Server 🚀

Máy chủ phát trực tiếp TikTok và xử lý dữ liệu (API TikWM, QR Login, Transcoder FFmpeg sang màn hình ST75256 192x64 2-bit Monochrome) dành cho máy tính bỏ túi FebonOS (ESP32-S3).

---

## 🌟 Tính Năng
1. **Hoạt động Cloud 100% độc lập**: Không cần bật máy tính ở nhà làm server trung gian.
2. **Triển khai miễn phí 100% không cần thẻ tín dụng** (trên Render.com hoặc Zeabur).
3. **TikWM Data Engine**: Lấy dữ liệu video xu hướng, tìm kiếm hashtag/từ khóa, bình luận và thông tin hồ sơ trực tiếp không bị CAPTCHA chặn.
4. **Đăng nhập bằng mã QR**: Tạo mã QR TikTok Web Passport để quét trực tiếp bằng ứng dụng TikTok trên điện thoại.
5. **Trang cá nhân & Lưới Video (3-Column Grid)**: Hiển thị avatar, ảnh bìa banner mới, tiểu sử bio, số follow, và lưới 3 cột thumbnail video có số view để chọn xem trực tiếp.
6. **FFmpeg Transcoding siêu tối ưu**:
   - Video: 162x64 (hoặc 192x64 toàn màn hình) 2-bit Dithered Monochrome (4 mức xám: Đen, Xám đậm, Xám nhạt, Trắng).
   - Âm thanh: 16kHz 16-bit Mono PCM đồng bộ với chip DAC I2S (PCM5102/MAX98357A).

---

## 🚀 Hướng Dẫn Triển Khai Miễn Phí Trên Render.com (Không Cần Thẻ Tín Dụng)

*(Lưu ý: Hugging Face đã đổi chính sách bắt buộc trả phí gói PRO đối với Docker Spaces, do đó Render.com là giải pháp thay thế tốt nhất hiện nay, hoàn toàn miễn phí 750 giờ/tháng và không yêu cầu thẻ tín dụng).*

### Bước 1: Đưa code lên GitHub
1. Vào [github.com](https://github.com) tạo một kho lưu trữ (Repository) mới (ví dụ đặt tên: `febonos-tiktok-server`), chọn chế độ **Public** hoặc **Private**.
2. Upload 3 file trong thư mục này lên GitHub:
   - `Dockerfile`
   - `requirements.txt`
   - `server.py`

### Bước 2: Đăng ký và Deploy trên Render.com
1. Truy cập [render.com](https://render.com) và chọn **Sign in with GitHub** (Đăng nhập bằng tài khoản GitHub, không cần nhập thẻ ngân hàng).
2. Nhấn nút **New +** ở góc trên bên phải $\rightarrow$ Chọn **Web Service**.
3. Chọn kho lưu trữ `febonos-tiktok-server` vừa tạo ở Bước 1.
4. Điền các thông tin:
   - **Name**: Đặt tên bất kỳ (ví dụ: `my-febonos-tiktok`).
   - **Region**: Chọn `Singapore` (để có tốc độ kết nối về Việt Nam nhanh nhất).
   - **Language**: Render sẽ tự động nhận diện là **Docker** (nếu không, chọn `Docker`).
   - **Instance Type**: Kéo xuống chọn gói **Free ($0/month)**.
5. Nhấn nút **Deploy Web Service**.
6. Render sẽ tự động build Docker container (mất khoảng 1-2 phút). Khi thấy trạng thái chuyển thành **Live**, bạn sẽ có 1 đường link công khai dạng:
   `https://my-febonos-tiktok.onrender.com`

### Bước 3: Cấu hình vào ESP32 (`StreamApp.h`)
Mở file `lib/UI/Apps/TikTok/StreamApp.h` và chỉnh sửa:
```cpp
static const char* TIKTOK_SERVER_HOST = "my-febonos-tiktok.onrender.com"; // Thay bằng tên domain của bạn
static const uint16_t TIKTOK_SERVER_PORT = 443;
static const bool TIKTOK_USE_SSL = true;
```
Sau đó nạp lại firmware vào ESP32 là xong!

---

## 💻 Cách Chạy Thử Trên Máy Tính Cá Nhân (Local LAN)

Nếu bạn muốn chạy thử nghiệm ngay trên mạng WiFi ở nhà:

1. Cài đặt FFmpeg trên máy tính (đảm bảo gõ `ffmpeg` trong CMD nhận lệnh).
2. Mở Terminal / PowerShell tại thư mục này:
   ```bash
   pip install -r requirements.txt
   python server.py
   ```
3. Xem địa chỉ IP của máy tính (ví dụ: `192.168.1.15`).
4. Trong `StreamApp.h`, cấu hình:
   ```cpp
   static const char* TIKTOK_SERVER_HOST = "192.168.1.15";
   static const uint16_t TIKTOK_SERVER_PORT = 5001;
   static const bool TIKTOK_USE_SSL = false;
   ```
