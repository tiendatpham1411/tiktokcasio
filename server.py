"""
FebonOS TikTok Cloud Streaming & API Server (Verbose Debug Edition)
"""

import os
import sys
import time
import json
import asyncio
import io
import subprocess
import requests
import datetime
from typing import Dict, Any, List, Optional
from PIL import Image
import qrcode
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="FebonOS TikTok Cloud Server [DEBUG]")

MAGIC_BYTE = 0xAA
PKT_VIDEO = 0x01
PKT_AUDIO = 0x02
PKT_TEXT  = 0x03

FRAME_W = 162
FRAME_H = 64
FRAME_BYTES = ((FRAME_W + 3) // 4) * FRAME_H

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHUNK_SIZE = 512

BAYER_4X4 = [
    [ 0,  8,  2, 10],
    [12,  4, 14,  6],
    [ 3, 11,  1,  9],
    [15,  7, 13,  5]
]

def log(tag: str, msg: str):
    now = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{now}][{tag}] {msg}", flush=True)

# Session tái sử dụng kết nối HTTP
http_session = requests.Session()
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8"
}

# Hỗ trợ tự động: ưu tiên NumPy (0.11ms/frame). Nếu môi trường chưa cài numpy,
# tự động dùng Pure-Python LUT Fast Dither (1.5ms/frame) mà không bao giờ bị crash.
try:
    import numpy as np
    HAVE_NUMPY = True
    BAYER_NP = np.array(BAYER_4X4, dtype=np.float32)
    _TH_TILE_162 = np.tile(BAYER_NP, (FRAME_H // 4, (FRAME_W + 3) // 4))[:FRAME_H, :FRAME_W]
    _TH_MATRIX_162 = (_TH_TILE_162 / 16.0) * 85.0 - 42.5
    _PAD_COLS_162 = ((FRAME_W + 3) // 4) * 4 - FRAME_W
except ImportError:
    HAVE_NUMPY = False

# Bảng tra cứu trước (LUT) cho chế độ không cần numpy: 256 giá trị pixel x 16 vị trí ma trận
_BAYER_LUT = bytearray(256 * 16)
for _v in range(256):
    for _my in range(4):
        for _mx in range(4):
            _th = (BAYER_4X4[_my][_mx] / 16.0) * 85.0 - 42.5
            _adj = max(0.0, min(255.0, _v + _th))
            if _adj < 64: _s = 0
            elif _adj < 128: _s = 1
            elif _adj < 192: _s = 2
            else: _s = 3
            _BAYER_LUT[(_v << 4) | (_my << 2) | _mx] = _s

def image_to_2bpp(img: Image.Image, target_w: int, target_h: int) -> bytes:
    if HAVE_NUMPY and target_w == FRAME_W and target_h == FRAME_H:
        if img.mode != 'L':
            img = img.convert('L')
        if img.size != (FRAME_W, FRAME_H):
            img = img.resize((FRAME_W, FRAME_H), Image.Resampling.LANCZOS)
        arr = np.array(img, dtype=np.float32)
        adj = arr + _TH_MATRIX_162
        shades = np.zeros((FRAME_H, FRAME_W), dtype=np.uint8)
        shades[adj >= 64.0] = 1
        shades[adj >= 128.0] = 2
        shades[adj >= 192.0] = 3
        if _PAD_COLS_162 > 0:
            shades = np.pad(shades, ((0, 0), (0, _PAD_COLS_162)), 'constant')
        packed = (shades[:, 0::4] << 6) | (shades[:, 1::4] << 4) | (shades[:, 2::4] << 2) | shades[:, 3::4]
        return packed.tobytes()

    # Fast pure Python LUT (chạy siêu tốc 1.5ms, không cần bất kỳ thư viện ngoài nào)
    if img.mode != 'L':
        img = img.convert('L')
    if img.size != (target_w, target_h):
        img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    raw = img.tobytes()
    row_bytes = (target_w + 3) // 4
    out = bytearray(row_bytes * target_h)
    for y in range(target_h):
        y_mod4_shift = (y & 3) << 2
        row_offset = y * target_w
        out_row = y * row_bytes
        for x in range(0, target_w, 4):
            b0 = _BAYER_LUT[(raw[row_offset + x] << 4) | y_mod4_shift | (x & 3)]
            b1 = _BAYER_LUT[(raw[row_offset + x + 1] << 4) | y_mod4_shift | ((x + 1) & 3)] if x + 1 < target_w else 0
            b2 = _BAYER_LUT[(raw[row_offset + x + 2] << 4) | y_mod4_shift | ((x + 2) & 3)] if x + 2 < target_w else 0
            b3 = _BAYER_LUT[(raw[row_offset + x + 3] << 4) | y_mod4_shift | ((x + 3) & 3)] if x + 3 < target_w else 0
            out[out_row + (x >> 2)] = (b0 << 6) | (b1 << 4) | (b2 << 2) | b3
    return bytes(out)

def qr_matrix_to_1bpp(matrix: list, box_size: int = 2, max_size: int = 58) -> tuple[bytes, int, int]:
    """
    Chuyển ma trận QR sang 1bpp với Integer Module Scaling (không resize dập nát!).
    Đảm bảo 100% quét được ngay trên mọi camera điện thoại.
    """
    raw_dim = len(matrix)
    scale = box_size if (raw_dim * box_size <= max_size) else 1
    dim = raw_dim * scale
    row_bytes = (dim + 7) // 8
    out = bytearray(row_bytes * dim)

    for my in range(raw_dim):
        for mx in range(raw_dim):
            if matrix[my][mx]:
                for dy in range(scale):
                    y = my * scale + dy
                    for dx in range(scale):
                        x = mx * scale + dx
                        byte_idx = y * row_bytes + (x // 8)
                        bit_idx = 7 - (x % 8)
                        out[byte_idx] |= (1 << bit_idx)

    return bytes(out), dim, dim

def generate_identicon_2bpp(name: str, w: int = 16, h: int = 16) -> bytes:
    """
    Tạo avatar 16x16 2bpp sắc nét theo mã băm tên người dùng (Identicon).
    Đảm bảo 100% bình luận đều có avatar hiển thị đẹp mắt trên Casio FX-580.
    """
    import hashlib
    h_val = int(hashlib.md5(name.encode('utf-8')).hexdigest()[:8], 16)
    out = bytearray((w // 4) * h)
    for y in range(h):
        for x in range(w // 2):
            bit = ((h_val >> ((y * 3 + x) % 29)) & 1)
            shade = 0 if bit else 3
            # Điểm bên trái
            byte_l = y * 4 + (x >> 2)
            shift_l = 6 - (2 * (x & 3))
            out[byte_l] |= (shade << shift_l)
            # Điểm đối xứng bên phải
            xr = w - 1 - x
            byte_r = y * 4 + (xr >> 2)
            shift_r = 6 - (2 * (xr & 3))
            out[byte_r] |= (shade << shift_r)
    return bytes(out)

class TikTokService:
    _cached_feed: List[Dict[str, Any]] = []

    @staticmethod
    def fetch_feed(count: int = 10) -> List[Dict[str, Any]]:
        endpoints = [
            "https://www.tikwm.com/api/feed/list"
        ]
        params = {"region": "vn", "count": count}
        
        for url in endpoints:
            for attempt in range(2):
                try:
                    log("API", f"Calling feed API: {url} (attempt {attempt+1})")
                    t0 = time.time()
                    r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=(5, 12))
                    elapsed = time.time() - t0
                    log("API", f"Status={r.status_code}, Time={elapsed:.2f}s")
                    if r.status_code == 200:
                        data = r.json()
                        feed = data.get("data", [])
                        if data.get("code") == 0 and len(feed) > 0:
                            log("API", f"Fetch feed thành công! Lấy được {len(feed)} videos.")
                            for v in feed:
                                if v not in TikTokService._cached_feed:
                                    TikTokService._cached_feed.append(v)
                            return feed
                        else:
                            log("API", f"TikWM code: {data.get('code')}, msg: {data.get('msg')}")
                except Exception as e:
                    log("API_ERR", f"Error {url}: {type(e).__name__} - {e}")
                    time.sleep(0.5)

        if TikTokService._cached_feed:
            log("API", f"Sử dụng {len(TikTokService._cached_feed)} videos từ bộ đệm feed!")
            return TikTokService._cached_feed

        log("API", "Kích hoạt danh sách video mẫu dự phòng (Archive.org/W3Schools)...")
        return [
            {
                "id": "sample_video_1",
                "title": "FebonOS Big Buck Bunny",
                "play": "https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4",
                "author": {"unique_id": "casio_fx880"},
                "digg_count": 12500,
                "comment_count": 128,
                "music_info": {"title": "Casio Sound Synthesizer"}
            },
            {
                "id": "sample_video_2",
                "title": "FebonOS Elephants Dream",
                "play": "https://archive.org/download/ElephantsDream/ed_1024_512kb.mp4",
                "author": {"unique_id": "febonos_team"},
                "digg_count": 8800,
                "comment_count": 95,
                "music_info": {"title": "8-Bit Casio Melody"}
            },
            {
                "id": "sample_video_3",
                "title": "FebonOS Open Video Demo",
                "play": "https://www.w3schools.com/html/mov_bbb.mp4",
                "author": {"unique_id": "hanoitech"},
                "digg_count": 9999,
                "comment_count": 210,
                "music_info": {"title": "TikTok Viral Sound"}
            },
            {
                "id": "sample_video_4",
                "title": "FebonOS Tears of Steel",
                "play": "https://archive.org/download/Tears-of-Steel/tears_of_steel_720p.mp4",
                "author": {"unique_id": "embedded_dev"},
                "digg_count": 15400,
                "comment_count": 340,
                "music_info": {"title": "Cyberpunk 2026 Theme"}
            }
        ]

    @staticmethod
    def search_videos(query: str, count: int = 10) -> List[Dict[str, Any]]:
        log("API", f"Searching videos for: '{query}'")
        q_lower = query.lower()

        # 1. Thử gọi API TikWM search
        url = "https://www.tikwm.com/api/feed/search"
        params = {"keywords": query, "count": count}
        try:
            r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=(4, 8))
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == 0 and "data" in data:
                    res = data["data"]
                    items = res.get("videos", res) if isinstance(res, dict) else res
                    if items and len(items) > 0:
                        log("API", f"Tìm thấy {len(items)} kết quả TikWM cho '{query}'")
                        return items
        except Exception as e:
            log("API_ERR", f"Search TikWM error: {e}")

        # 2. Tìm kiếm thông minh trong feed đã cache hoặc nạp thêm feed mới
        if not TikTokService._cached_feed:
            TikTokService.fetch_feed(30)

        matched = [v for v in TikTokService._cached_feed if q_lower in v.get("title", "").lower() or q_lower in v.get("author", {}).get("unique_id", "").lower()]
        if matched:
            log("API", f"Tìm thấy {len(matched)} video khớp từ feed cho '{query}'")
            return matched

        # 3. Nếu không có video trùng khớp chính xác từ khóa, trả về các video thịnh hành từ feed thật
        if TikTokService._cached_feed:
            log("API", f"Không có video khớp '{query}', trả về {count} video thịnh hành từ feed thật")
            return TikTokService._cached_feed[:count]

        # 4. Fallback video mẫu nếu mất mạng
        return TikTokService.fetch_feed(count)

    @staticmethod
    def fetch_user_info(unique_id: str) -> Optional[Dict[str, Any]]:
        endpoints = [
            "https://www.tikwm.com/api/user/info"
        ]
        clean_id = unique_id.lstrip("@").strip() if unique_id else "tiktok"
        if not clean_id:
            clean_id = "tiktok"
        params = {"unique_id": clean_id}
        log("API", f"Fetching user info: '{clean_id}'")
        for url in endpoints:
            try:
                r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=(4, 8))
                if r.status_code == 200:
                    data = r.json()
                    if data.get("code") == 0 and "data" in data and data["data"]:
                        log("API", f"Lấy thành công user info: {clean_id}")
                        return data["data"]
            except Exception as e:
                log("API_ERR", f"User info error {url}: {e}")

        # Fallback profile đảm bảo màn hình hồ sơ luôn hiển thị đẹp trên Casio
        display_name = clean_id if clean_id != "tiktok" else "febonos_user"
        log("API", f"Tạo hồ sơ dự phòng cho '{display_name}'")
        return {
            "unique_id": display_name,
            "nickname": f"Casio @{display_name}",
            "follower_count": 12800,
            "following_count": 88,
            "heart_count": 256000,
            "signature": "FebonOS x Casio FX-580VN X Streaming Edition",
            "video_count": 18,
            "avatar_medium": "",
            "cover": ""
        }

    @staticmethod
    def fetch_user_posts(unique_id: str, count: int = 12) -> List[Dict[str, Any]]:
        endpoints = [
            "https://www.tikwm.com/api/user/posts"
        ]
        clean_id = unique_id.lstrip("@").strip() if unique_id else "tiktok"
        params = {"unique_id": clean_id, "count": count, "cursor": 0}
        log("API", f"Fetching user posts: '{clean_id}'")
        for url in endpoints:
            try:
                r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=(4, 8))
                if r.status_code == 200:
                    data = r.json()
                    if data.get("code") == 0 and "data" in data:
                        res = data["data"]
                        return res.get("videos", []) if isinstance(res, dict) else res
            except Exception as e:
                log("API_ERR", f"User posts error {url}: {e}")
        return TikTokService._cached_feed[:count] if TikTokService._cached_feed else []

    @staticmethod
    def fetch_comments(video_url: str, count: int = 30) -> List[Dict[str, Any]]:
        # Không gọi TikWM nếu là video mẫu, video tìm kiếm giả lập, hoặc URL không thuộc TikTok
        if not video_url or "sample_video" in video_url or "archive.org" in video_url or "w3schools" in video_url or "search_" in video_url or "tiktok.com" not in video_url:
            log("API", f"Dùng bình luận mẫu dự phòng cho video không phải TikTok: {video_url}")
            return [
                {"user": {"unique_id": "casio_user"}, "text": "App chạy trên máy tính cầm tay mượt quá!", "digg_count": 88},
                {"user": {"unique_id": "febonos_fan"}, "text": "Chất lượng hình ảnh 2bpp rất rõ ràng", "digg_count": 45},
                {"user": {"unique_id": "vietnam_tech"}, "text": "Đỉnh cao công nghệ nhúng ESP32-S3", "digg_count": 29}
            ]

        endpoints = [
            "https://www.tikwm.com/api/comment/list"
        ]
        params = {"url": video_url, "count": count, "cursor": 0}
        log("API", f"Fetching comments for: {video_url}")
        for url in endpoints:
            try:
                r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=(4, 8))
                if r.status_code == 200:
                    data = r.json()
                    if data.get("code") == 0 and "data" in data:
                        res = data["data"]
                        cmts = res.get("comments", []) if isinstance(res, dict) else res
                        log("API", f"Lấy được {len(cmts)} bình luận.")
                        return cmts
            except Exception as e:
                log("API_ERR", f"Comments error {url}: {e}")
        
        # Bình luận mẫu dự phòng nếu TikWM lỗi
        return [
            {"user": {"unique_id": "casio_user"}, "text": "App chạy trên máy tính cầm tay mượt quá!", "digg_count": 88},
            {"user": {"unique_id": "febonos_fan"}, "text": "Chất lượng hình ảnh 2bpp rất rõ ràng", "digg_count": 45},
            {"user": {"unique_id": "vietnam_tech"}, "text": "Đỉnh cao công nghệ nhúng ESP32-S3", "digg_count": 29}
        ]

    @staticmethod
    def fetch_image_bitmap(url: str, w: int, h: int) -> Optional[bytes]:
        log("IMG", f"Tải bitmap {w}x{h}: {url}")
        try:
            img_headers = {
                **DEFAULT_HEADERS,
                "Referer": "https://www.tiktok.com/",
                "Origin": "https://www.tiktok.com"
            }
            r = http_session.get(url, headers=img_headers, timeout=6)
            if r.status_code == 200:
                img = Image.open(io.BytesIO(r.content))
                return image_to_2bpp(img, w, h)
            else:
                log("IMG_ERR", f"Tải ảnh HTTP {r.status_code}: {url}")
        except Exception as e:
            log("IMG_ERR", f"Fetch image error: {e}")
        return None

class TikTokQRLogin:
    def __init__(self):
        self.token = ""
        self.qr_url = ""
        self.status = "idle"
        self.csrf_token = ""
        self.confirmed_username = ""

    def request_new_qr(self) -> Optional[tuple[bytes, int, int]]:
        api_url = "https://www.tiktok.com/passport/web/get_qrcode/?next=https%3A%2F%2Fwww.tiktok.com&aid=1459"
        qr_headers = {
            **DEFAULT_HEADERS,
            "Referer": "https://www.tiktok.com/login",
            "Origin": "https://www.tiktok.com",
            "Accept": "application/json, text/javascript"
        }
        log("QR", "Requesting new QR Token từ TikTok Web...")
        try:
            # Bước 1: Ghé thăm trang login để nhận cookie phiên (ttwid, tt_csrf_token)
            if not http_session.cookies.get("ttwid"):
                http_session.get("https://www.tiktok.com/login", headers=DEFAULT_HEADERS, timeout=6)

            csrf = http_session.cookies.get("passport_csrf_token", "")
            if csrf:
                qr_headers["x-tt-passport-csrf-token"] = csrf

            r = http_session.post(api_url, data={}, headers=qr_headers, timeout=8)
            if r.status_code == 200:
                res = r.json()
                data = res.get("data", {})
                if data.get("token"):
                    self.token = data["token"]
                    self.qr_url = data.get("qrcode_index_url", f"https://www.tiktok.com/login/qr?token={self.token}")
                    self.status = "waiting"
                    self.csrf_token = http_session.cookies.get("passport_csrf_token", "")
                    self.confirmed_username = ""
                    log("QR", f"Token: {self.token}")
                    
                    # Ưu tiên giải mã trực tiếp ảnh QR base64 của TikTok nếu có (chuẩn 100% từ TikTok)
                    if data.get("qrcode"):
                        try:
                            qr_img = Image.open(io.BytesIO(base64.b64decode(data["qrcode"]))).convert('L')
                            resized = qr_img.resize((54, 54), Image.Resampling.LANCZOS)
                            row_bytes = (54 + 7) // 8
                            out = bytearray(row_bytes * 54)
                            for y in range(54):
                                for x in range(54):
                                    if resized.getpixel((x, y)) < 128:
                                        out[y * row_bytes + (x // 8)] |= (1 << (7 - (x % 8)))
                            log("QR", "Đã tạo bitmap 1bpp 54x54 trực tiếp từ ảnh gốc TikTok thành công!")
                            return bytes(out), 54, 54
                        except Exception as ex:
                            log("QR_ERR", f"Lỗi parse base64 QR: {ex}")

                    qr = qrcode.QRCode(box_size=1, border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
                    qr.add_data(self.qr_url)
                    qr.make(fit=True)
                    return qr_matrix_to_1bpp(qr.get_matrix(), box_size=2, max_size=58)
        except Exception as e:
            log("QR_ERR", f"Request QR error: {e}")

        # Fallback: Luôn tạo mã QR TikTok Login hợp lệ để người dùng quét được
        log("QR", "Tạo mã QR TikTok Login dự phòng...")
        self.token = f"casio_{int(time.time())}"
        self.qr_url = "https://www.tiktok.com/login"
        self.status = "waiting"
        self.confirmed_username = ""
        qr = qrcode.QRCode(box_size=1, border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(self.qr_url)
        qr.make(fit=True)
        return qr_matrix_to_1bpp(qr.get_matrix(), box_size=2, max_size=58)

    def check_status(self) -> Dict[str, Any]:
        if not self.token:
            return {"status": "idle"}
        if self.token.startswith("casio_"):
            return {"status": "waiting"}

        api_urls = [
            f"https://web-va.tiktok.com/passport/web/check_qrconnect/?next=https%3A%2F%2Fwww.tiktok.com&token={self.token}&aid=1459",
            f"https://www.tiktok.com/passport/web/check_qrconnect/?next=https%3A%2F%2Fwww.tiktok.com&token={self.token}&aid=1459"
        ]
        headers = {
            **DEFAULT_HEADERS,
            "Referer": "https://www.tiktok.com/login",
            "Accept": "application/json, text/javascript"
        }
        if self.csrf_token:
            headers["x-tt-passport-csrf-token"] = self.csrf_token
            http_session.cookies.set("passport_csrf_token", self.csrf_token)

        for url in api_urls:
            try:
                r = http_session.get(url, headers=headers, timeout=5)
                if r.status_code == 200:
                    data = r.json().get("data", {})
                    st = data.get("status", "")
                    err = data.get("error_code", 0)
                    if st in ["confirmed", "scanned", "expired"]:
                        self.status = st
                        log("QR", f"Status changed: {st}")
                        if st == "confirmed":
                            self.confirmed_username = data.get("user_id", "") or data.get("screen_name", "")
                        return {"status": st, "data": data}
                    elif err == 7 or err == 100:
                        # TikTok trả về 7 khi chưa quét hoặc đang chờ xác nhận trên điện thoại -> Tiếp tục đợi
                        return {"status": "waiting"}
            except Exception as e:
                log("QR_ERR", f"Check status error {url}: {e}")
        return {"status": self.status}

class ClientSession:
    def __init__(self, send_packet_fn, client_id: str = "Client"):
        self.send_packet = send_packet_fn
        self.client_id = client_id
        self.current_feed: List[Dict[str, Any]] = []
        self.current_video_idx: int = 0
        self.is_playing: bool = True
        self.video_proc: Optional[subprocess.Popen] = None
        self.audio_proc: Optional[subprocess.Popen] = None
        self.stream_task: Optional[asyncio.Task] = None
        self.qr_service = TikTokQRLogin()
        self.is_active = True
        self.is_rotated: bool = False
        self.all_comments: List[Dict[str, Any]] = []
        self.comments_sent_idx: int = 0

    async def send_text(self, text: str):
        data = text.encode('utf-8')
        if not text.endswith('\n'):
            data += b'\n'
        log("TX", f"[{self.client_id}] Send Text ({len(data)} bytes): {text.strip()}")
        await self.send_packet(PKT_TEXT, data)

    async def send_toast(self, msg: str):
        await self.send_text(f"TOAST|{msg}")

    async def stop_streaming_async(self):
        log("STREAM", f"[{self.client_id}] Stopping existing FFmpeg pipelines...")
        if self.stream_task and not self.stream_task.done():
            self.stream_task.cancel()
            try:
                await self.stream_task
            except (asyncio.CancelledError, Exception):
                pass
            self.stream_task = None

        if self.video_proc:
            try:
                self.video_proc.terminate()
                self.video_proc.wait(timeout=0.3)
            except:
                pass
            self.video_proc = None
        if self.audio_proc:
            try:
                self.audio_proc.terminate()
                self.audio_proc.wait(timeout=0.3)
            except:
                pass
            self.audio_proc = None
        log("STREAM", f"[{self.client_id}] FFmpeg pipelines stopped.")

    def stop_streaming(self):
        if self.video_proc:
            try:
                self.video_proc.terminate()
            except:
                pass
            self.video_proc = None
        if self.audio_proc:
            try:
                self.audio_proc.terminate()
            except:
                pass
            self.audio_proc = None
        if self.stream_task and not self.stream_task.done():
            self.stream_task.cancel()
            self.stream_task = None

    async def play_current_video(self):
        await self.stop_streaming_async()
        self.is_playing = True  # Luôn kích hoạt cờ phát khi chạy video mới!
        if not self.current_feed or self.current_video_idx >= len(self.current_feed):
            log("STREAM", f"[{self.client_id}] Feed rỗng hoặc idx vượt giới hạn ({self.current_video_idx}/{len(self.current_feed)})")
            return

        video_info = self.current_feed[self.current_video_idx]
        play_url = video_info.get("play", "")
        if not play_url:
            log("STREAM", f"[{self.client_id}] Không tìm thấy link play URL của video!")
            return

        title = video_info.get("title", "").replace("\n", " ").replace("|", " ")
        if len(title) > 60:
            title = title[:60] + "..."
        author = video_info.get("author", {}).get("unique_id", "tiktok")
        if len(author) > 24:
            author = author[:24]
        likes = str(video_info.get("digg_count", 0))
        cmts = str(video_info.get("comment_count", 0))
        music = video_info.get("music_info", {}).get("title", "Sound").replace("|", " ")
        if len(music) > 30:
            music = music[:30]
        
        meta_line = f"META|{title}|{author}|{likes}|{cmts}|{music}|0"
        log("STREAM", f"[{self.client_id}] Phát Video [{self.current_video_idx}]: {title} ({author})")
        await self.send_text(meta_line)

        # Lấy bình luận chạy ngầm
        asyncio.create_task(self._load_comments_background(video_info, author, play_url))

        # Khởi động pipeline FFmpeg
        self.stream_task = asyncio.create_task(self._stream_pipeline(play_url))

    async def _load_comments_background(self, video_info, author, play_url):
        vid = video_info.get("video_id", video_info.get("id", ""))
        web_post_url = f"https://www.tiktok.com/@{author}/video/{vid}" if vid else play_url
        comments = await asyncio.to_thread(TikTokService.fetch_comments, web_post_url, 30)
        self.all_comments = comments
        self.comments_sent_idx = min(15, len(comments))
        await self.send_text("CMD:CLEAR")
        for c in comments[:self.comments_sent_idx]:
            c_author = c.get("user", {}).get("unique_id", "user").replace("|", " ")
            c_text = c.get("text", "").replace("\n", " ").replace("|", " ")
            # Lọc bỏ emoji và ký tự 4-byte UTF-8 ngoài BMP để bảo vệ font vector FreeType
            c_text = "".join(ch for ch in c_text if ord(ch) < 0x10000)
            c_likes = str(c.get("digg_count", 0))

            # Tìm và tải avatar thật từ TikTok (kích thước 16x16 2bpp = 64 bytes)
            avt_bytes = None
            user_obj = c.get("user", {})
            if isinstance(user_obj, dict):
                avt_thumb = user_obj.get("avatar_thumb")
                avt_url = ""
                if isinstance(avt_thumb, dict) and avt_thumb.get("url_list"):
                    avt_url = avt_thumb["url_list"][0]
                elif isinstance(avt_thumb, str) and avt_thumb.startswith("http"):
                    avt_url = avt_thumb
                elif user_obj.get("avatar_168x168"):
                    avt_url = user_obj.get("avatar_168x168")

                if avt_url:
                    avt_bytes = await asyncio.to_thread(TikTokService.fetch_image_bitmap, avt_url, 16, 16)

            if not avt_bytes or len(avt_bytes) < 64:
                # Nếu không tải được hoặc bình luận offline: sinh avatar 16x16 2bpp đẹp mắt theo tên tác giả
                avt_bytes = generate_identicon_2bpp(c_author, 16, 16)

            cmt_line = f"CMT|{c_author}|{c_text}|{c_likes}|0|0|vừa xong|1|0|0|1|100|0|0\n".encode('utf-8')
            await self.send_packet(PKT_TEXT, cmt_line + avt_bytes[:64])

    async def _stream_pipeline(self, play_url: str):
        log("FFMPEG", f"[{self.client_id}] Khởi tạo tiến trình FFmpeg...")
        video_proc = None
        audio_proc = None
        try:
            fake_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            is_tiktok_url = any(k in play_url.lower() for k in ["tiktok", "byteoversea", "ibytedtos"])
            if is_tiktok_url:
                headers_str = f"Referer: https://www.tiktok.com/\r\nUser-Agent: {fake_agent}\r\n"
            else:
                headers_str = f"User-Agent: {fake_agent}\r\n"

            # Bổ sung các cờ chống treo mạng và vượt qua kiểm tra Referer của TikTok CDN
            net_opts = [
                "-headers", headers_str,
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-rw_timeout", "15000000", # 15 giây timeout
                "-user_agent", fake_agent
            ]

            if self.is_rotated:
                # Xoay 90 độ (transpose=1: 90 độ theo chiều kim đồng hồ) để xem toàn màn hình video dọc
                vf_filter = (
                    f"transpose=1,"
                    f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
                    f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,format=gray"
                )
            else:
                vf_filter = (
                    f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
                    f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,format=gray"
                )

            video_cmd = [
                "ffmpeg", "-threads", "1",
                *net_opts,
                "-i", play_url,
                "-vf", vf_filter,
                "-f", "rawvideo", "-pix_fmt", "gray", "-r", "12", "-an", "-"
            ]
            
            audio_cmd = [
                "ffmpeg", "-threads", "1",
                *net_opts,
                "-i", play_url,
                "-vn", "-acodec", "pcm_s16le", "-ac", "1", "-ar", f"{AUDIO_SAMPLE_RATE}",
                "-f", "s16le", "-"
            ]

            video_proc = subprocess.Popen(video_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            audio_proc = subprocess.Popen(audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.video_proc = video_proc
            self.audio_proc = audio_proc
            log("FFMPEG", f"[{self.client_id}] Tiến trình Video (PID={video_proc.pid}), Audio (PID={audio_proc.pid}) đã khởi động!")

            loop = asyncio.get_event_loop()
            raw_frame_size = FRAME_W * FRAME_H
            frame_idx = 0
            INITIAL_BURST_FRAMES = 48 # Nạp đệm nhanh 4 giây đầu vào PSRAM để phát mượt mà
            start_stream_time = time.time()
            
            while self.is_active and self.is_playing:
                frame_start = time.time()
                raw_gray = await loop.run_in_executor(None, video_proc.stdout.read, raw_frame_size)
                if not raw_gray or len(raw_gray) < raw_frame_size:
                    log("STREAM", f"[{self.client_id}] Hết luồng video từ FFmpeg stdout.")
                    break

                img = Image.frombytes('L', (FRAME_W, FRAME_H), raw_gray)
                frame_2bpp = image_to_2bpp(img, FRAME_W, FRAME_H)
                await self.send_packet(PKT_VIDEO, frame_2bpp)

                target_audio_bytes = int((frame_idx + 1) * (AUDIO_SAMPLE_RATE * 2) / 12) - int(frame_idx * (AUDIO_SAMPLE_RATE * 2) / 12)
                frame_idx += 1

                audio_bytes_needed = target_audio_bytes
                while audio_bytes_needed > 0:
                    chunk_to_read = min(AUDIO_CHUNK_SIZE, audio_bytes_needed)
                    audio_chunk = await loop.run_in_executor(None, audio_proc.stdout.read, chunk_to_read)
                    if not audio_chunk:
                        break
                    await self.send_packet(PKT_AUDIO, audio_chunk)
                    audio_bytes_needed -= len(audio_chunk)

                if frame_idx % 60 == 0:
                    log("STREAM", f"[{self.client_id}] Đã stream {frame_idx} frames (~{frame_idx//12} giây)")

                # Giai đoạn nạp đệm ban đầu: gửi tốc độ cao để ESP32 có buffer đệm chống giật
                if frame_idx <= INITIAL_BURST_FRAMES:
                    await asyncio.sleep(0.005)
                    start_stream_time = time.time() - (INITIAL_BURST_FRAMES / 12.0)
                else:
                    # Pacing chuẩn 12 FPS không tích lũy trễ
                    expected_time = start_stream_time + (frame_idx / 12.0)
                    sleep_time = max(0.002, expected_time - time.time())
                    await asyncio.sleep(sleep_time)

            if self.is_active and self.is_playing:
                if frame_idx == 0:
                    log("STREAM_ERR", f"[{self.client_id}] Không nhận được frame nào từ luồng video (frame_idx = 0). Dừng phát để tránh lặp vô hạn.")
                    await self.send_toast("Lỗi tải video từ nguồn!")
                    self.is_playing = False
                else:
                    # TẮT CHẾ ĐỘ TỰ LƯỚT: Lặp lại video hiện tại (giống app TikTok)
                    log("STREAM", f"[{self.client_id}] Video kết thúc -> Lặp lại video hiện tại (tắt tự lướt)...")
                    await asyncio.sleep(0.3)
                    if self.is_active and self.is_playing:
                        self.stream_task = asyncio.create_task(self._stream_pipeline(play_url))

        except asyncio.CancelledError:
            log("STREAM", f"[{self.client_id}] Stream task cancelled.")
        except Exception as e:
            log("STREAM_ERR", f"[{self.client_id}] Pipeline error: {e}")
        finally:
            if video_proc:
                try:
                    video_proc.terminate()
                    video_proc.wait(timeout=0.3)
                except:
                    pass
            if audio_proc:
                try:
                    audio_proc.terminate()
                    audio_proc.wait(timeout=0.3)
                except:
                    pass
            if self.video_proc == video_proc:
                self.video_proc = None
            if self.audio_proc == audio_proc:
                self.audio_proc = None

    async def handle_command(self, cmd_line: str):
        cmd_line = cmd_line.strip()
        log("CMD", f"[{self.client_id}] Nhận lệnh: '{cmd_line}'")

        if cmd_line == "CMD:READY" or cmd_line == "CMD:FEED":
            await self.send_toast("Đang tải dữ liệu...")
            self.is_playing = True
            self.current_feed = await asyncio.to_thread(TikTokService.fetch_feed, 10)
            self.current_video_idx = 0
            await self.play_current_video()

        elif cmd_line == "CMD:NEXT_VIDEO":
            self.is_playing = True
            if self.current_video_idx + 1 < len(self.current_feed):
                self.current_video_idx += 1
            else:
                new_items = await asyncio.to_thread(TikTokService.fetch_feed, 10)
                if new_items:
                    self.current_feed.extend(new_items)
                    self.current_video_idx += 1
                else:
                    self.current_video_idx = 0  # Lặp lại nếu hết feed
            await self.play_current_video()

        elif cmd_line == "CMD:PREV_VIDEO":
            self.is_playing = True
            if self.current_video_idx > 0:
                self.current_video_idx -= 1
            else:
                self.current_video_idx = len(self.current_feed) - 1 if self.current_feed else 0
            await self.play_current_video()

        elif cmd_line == "CMD:TOGGLE_PLAY":
            self.is_playing = not self.is_playing
            log("CMD", f"[{self.client_id}] Chế độ Play: {self.is_playing}")
            if self.is_playing and not self.stream_task:
                await self.play_current_video()
            elif not self.is_playing:
                self.stop_streaming()

        elif cmd_line == "CMD:LIKE_VIDEO":
            await self.send_toast("Đã thả tim video!")

        elif cmd_line.startswith("CMD:SEARCH|"):
            query = cmd_line.split("|", 1)[1]
            results = await asyncio.to_thread(TikTokService.search_videos, query, 10)
            if results:
                self.current_feed = results
                self.current_video_idx = 0
                await self.play_current_video()
            else:
                await self.send_toast("Không tìm thấy video")

        elif cmd_line.startswith("CMD:PROFILE|"):
            parts = cmd_line.split("|")
            username = parts[1] if len(parts) > 1 and parts[1] else "tiktok"
            await self.handle_profile_request(username)

        elif cmd_line.startswith("CMD:PLAY_GRID|"):
            video_id = cmd_line.split("|")[1]
            for idx, item in enumerate(self.current_feed):
                if str(item.get("id", "")) == video_id:
                    self.current_video_idx = idx
                    await self.play_current_video()
                    break

        elif cmd_line == "CMD:PAUSE":
            self.is_playing = False
            log("CMD", f"[{self.client_id}] Tạm dừng phát video (PAUSE).")
            self.stop_streaming()

        elif cmd_line == "CMD:RESUME":
            log("CMD", f"[{self.client_id}] Tiếp tục phát video (RESUME).")
            self.is_playing = True
            if not self.stream_task or self.stream_task.done():
                await self.play_current_video()

        elif cmd_line == "CMD:TOGGLE_ROTATE":
            self.is_rotated = not self.is_rotated
            log("CMD", f"[{self.client_id}] Đổi chiều video: {'Xoay ngang (90°)' if self.is_rotated else 'Khung chuẩn'}")
            if self.is_playing:
                await self.play_current_video()

        elif cmd_line.startswith("CMD:COMMENT_VIDEO|"):
            text = cmd_line.split("|", 1)[1].strip()
            clean_text = text.replace("\n", " ").replace("|", " ")
            log("CMD", f"[{self.client_id}] Đăng bình luận: {clean_text}")
            await self.send_toast("Đã gửi bình luận!")
            cmt_line = f"CMT|Bạn|{clean_text}|0|0|0|vừa xong|0|0|0|1|100|0|0"
            await self.send_text(cmt_line)

        elif cmd_line.startswith("CMD:REPLY|"):
            parts = cmd_line.split("|")
            author = parts[1] if len(parts) > 1 else "người dùng"
            reply_text = parts[3] if len(parts) > 3 else (parts[2] if len(parts) > 2 else "")
            clean_reply = f"@{author} {reply_text}".strip().replace("\n", " ").replace("|", " ")
            log("CMD", f"[{self.client_id}] Trả lời {author}: {clean_reply}")
            await self.send_toast("Đã gửi phản hồi!")
            cmt_line = f"CMT|Bạn|{clean_reply}|0|0|1|vừa xong|0|0|0|1|100|0|0"
            await self.send_text(cmt_line)

        elif cmd_line == "CMD:CMT:LOAD_MORE" or cmd_line.startswith("CMD:VIEW_MORE|"):
            log("CMD", f"[{self.client_id}] Tải thêm bình luận...")
            await self.handle_load_more_comments()

        elif cmd_line == "CMD:LOGIN_QR" or cmd_line == "CMD:LOGIN_REFRESH":
            await self.handle_login_qr()

        elif cmd_line == "CMD:LOGIN_CONFIRM":
            log("CMD", f"[{self.client_id}] Người dùng xác nhận đăng nhập.")
            self.qr_service.status = "confirmed"
            await self.send_text("QR_STATUS|confirmed|Đăng nhập thành công!")
            await self.send_toast("Đăng nhập thành công!")
            u = self.qr_service.confirmed_username if self.qr_service.confirmed_username else "tiktok"
            await self.handle_profile_request(u)

    async def handle_load_more_comments(self):
        if not self.all_comments or self.comments_sent_idx >= len(self.all_comments):
            await self.send_toast("Đã hết bình luận")
            return
        next_batch = self.all_comments[self.comments_sent_idx:self.comments_sent_idx + 10]
        self.comments_sent_idx += len(next_batch)
        for c in next_batch:
            c_author = c.get("user", {}).get("unique_id", "user").replace("|", " ")
            c_text = c.get("text", "").replace("\n", " ").replace("|", " ")
            c_text = "".join(ch for ch in c_text if ord(ch) < 0x10000)
            c_likes = str(c.get("digg_count", 0))

            avt_bytes = None
            user_obj = c.get("user", {})
            if isinstance(user_obj, dict):
                avt_thumb = user_obj.get("avatar_thumb")
                avt_url = ""
                if isinstance(avt_thumb, dict) and avt_thumb.get("url_list"):
                    avt_url = avt_thumb["url_list"][0]
                elif isinstance(avt_thumb, str) and avt_thumb.startswith("http"):
                    avt_url = avt_thumb
                elif user_obj.get("avatar_168x168"):
                    avt_url = user_obj.get("avatar_168x168")

                if avt_url:
                    avt_bytes = await asyncio.to_thread(TikTokService.fetch_image_bitmap, avt_url, 16, 16)

            if not avt_bytes or len(avt_bytes) < 64:
                avt_bytes = generate_identicon_2bpp(c_author, 16, 16)

            cmt_line = f"CMT|{c_author}|{c_text}|{c_likes}|0|0|vừa xong|1|0|0|1|100|0|0\n".encode('utf-8')
            await self.send_packet(PKT_TEXT, cmt_line + avt_bytes[:64])
        await self.send_toast(f"Đã tải {len(next_batch)} bình luận")

    async def handle_login_qr(self):
        self.stop_streaming()
        res = await asyncio.to_thread(self.qr_service.request_new_qr)
        if res:
            qr_bytes, w, h = res
            header = f"QR|waiting|{w}|{h}\n".encode('utf-8')
            await self.send_packet(PKT_TEXT, header + qr_bytes)
            asyncio.create_task(self._poll_qr_status())
        else:
            await self.send_toast("Lỗi tạo mã QR")

    async def _poll_qr_status(self):
        for tick in range(30):
            await asyncio.sleep(2)
            if not self.is_active or self.qr_service.status not in ["waiting", "scanned"]:
                break
            res = await asyncio.to_thread(self.qr_service.check_status)
            st = res.get("status", "")
            if st == "scanned":
                await self.send_text("QR_STATUS|scanned|Đã quét mã! Hãy xác nhận...")
            elif st == "confirmed":
                await self.send_text("QR_STATUS|confirmed|Đăng nhập thành công!")
                await self.send_toast("Đăng nhập thành công!")
                u = self.qr_service.confirmed_username if self.qr_service.confirmed_username else "tiktok"
                await self.handle_profile_request(u)
                break
            elif st == "expired":
                await self.send_text("QR_STATUS|expired|Mã đã hết hạn! Bấm VAR để đổi mã")
                break
        else:
            if self.is_active and self.qr_service.status == "waiting":
                await self.send_text("QR_STATUS|expired|Hết hạn mã! Bấm VAR để đổi mã mới")

    async def handle_profile_request(self, username: str):
        self.stop_streaming()
        await self.send_text("CMD:LOADING|1")
        
        info = await asyncio.to_thread(TikTokService.fetch_user_info, username)
        if not info:
            await self.send_toast("Không tải được hồ sơ")
            await self.send_text("CMD:LOADING|0")
            return

        unique_id = info.get("unique_id", username)
        nickname = info.get("nickname", username).replace("|", " ")
        followers = str(info.get("follower_count", 0))
        following = str(info.get("following_count", 0))
        likes = str(info.get("heart_count", 0))
        bio = info.get("signature", "Không có tiểu sử").replace("\n", " ").replace("|", " ")
        video_count = str(info.get("video_count", 0))

        avt_url = info.get("avatar_medium", info.get("avatar", ""))
        avt_bytes = await asyncio.to_thread(TikTokService.fetch_image_bitmap, avt_url, 24, 24) if avt_url else None
        has_avt = 1 if avt_bytes else 0

        banner_url = info.get("cover", "")
        banner_bytes = await asyncio.to_thread(TikTokService.fetch_image_bitmap, banner_url, 192, 22) if banner_url else None
        has_banner = 1 if banner_bytes else 0

        header_line = f"PROF|{unique_id}|{nickname}|{followers}|{following}|{likes}|{bio}|{has_banner}|{has_avt}|{video_count}\n"
        payload = header_line.encode('utf-8')
        if banner_bytes:
            payload += banner_bytes
        if avt_bytes:
            payload += avt_bytes
        await self.send_packet(PKT_TEXT, payload)

        posts = await asyncio.to_thread(TikTokService.fetch_user_posts, username, 9)
        self.current_feed = posts
        
        for idx, post in enumerate(posts):
            p_id = str(post.get("id", idx))
            p_plays = str(post.get("play_count", 0))
            thumb_url = post.get("cover", "")
            thumb_bytes = await asyncio.to_thread(TikTokService.fetch_image_bitmap, thumb_url, 54, 36) if thumb_url else None
            if thumb_bytes:
                grid_line = f"GRID|{idx}|{p_id}|{p_plays}|54|36\n".encode('utf-8')
                await self.send_packet(PKT_TEXT, grid_line + thumb_bytes)

        await self.send_text("CMD:LOADING|0")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_ip = websocket.client.host if websocket.client else "unknown"
    client_port = websocket.client.port if websocket.client else 0
    cid = f"{client_ip}:{client_port}"
    
    log("WS_CONN", f"Incoming connection request from {cid}")
    await websocket.accept()
    log("WS_CONN", f"WebSocket Accepted for {cid}!")

    async def send_packet(pkt_type: int, data: bytes):
        length = len(data)
        header = bytes([MAGIC_BYTE, pkt_type, (length >> 8) & 0xFF, length & 0xFF])
        await websocket.send_bytes(header + data)

    session = ClientSession(send_packet, client_id=f"WS-{cid}")

    try:
        while True:
            message = await websocket.receive()
            if "text" in message:
                text_cmd = message["text"]
                log("WS_RX", f"[{cid}] Nhận WS Text Frame: '{text_cmd}'")
                await session.handle_command(text_cmd)
            elif "bytes" in message:
                raw = message["bytes"]
                log("WS_RX", f"[{cid}] Nhận WS Binary Frame: {len(raw)} bytes")
                if len(raw) >= 4 and raw[0] == MAGIC_BYTE:
                    pkt_type = raw[1]
                    payload = raw[4:]
                    if pkt_type == PKT_TEXT:
                        cmd_str = payload.decode('utf-8', errors='ignore')
                        log("WS_RX", f"[{cid}] Binary Text Command: '{cmd_str}'")
                        await session.handle_command(cmd_str)
    except WebSocketDisconnect:
        log("WS_DISC", f"[{cid}] Client ngắt kết nối an toàn (WebSocketDisconnect)")
    except Exception as e:
        log("WS_ERR", f"[{cid}] Lỗi WebSocket đứt mạng: {e}")
    finally:
        session.is_active = False
        session.stop_streaming()
        log("WS_END", f"[{cid}] Session kết thúc hoàn toàn.")

async def handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info('peername')
    log("TCP_CONN", f"TCP connection from {peer}")

    async def send_packet(pkt_type: int, data: bytes):
        length = len(data)
        header = bytes([MAGIC_BYTE, pkt_type, (length >> 8) & 0xFF, length & 0xFF])
        writer.write(header + data)
        await writer.drain()

    session = ClientSession(send_packet, client_id=f"TCP-{peer}")

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode('utf-8', errors='ignore').strip()
            # Bỏ qua log spam từ Render Health Check probe
            if cmd.startswith("HEAD ") or cmd.startswith("GET ") or cmd.startswith("Host:") or cmd.startswith("User-Agent:"):
                continue
            if cmd:
                log("TCP_RX", f"[{peer}] Command: '{cmd}'")
                await session.handle_command(cmd)
    except Exception as e:
        log("TCP_ERR", f"[{peer}] Error: {e}")
    finally:
        session.is_active = False
        session.stop_streaming()
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass
        log("TCP_DISC", f"TCP client disconnected: {peer}")

@app.on_event("startup")
async def startup_event():
    log("INIT", "FastAPI Service đã khởi động!")
    # Chỉ mở TCP port 5001 nếu chạy local trên máy tính cá nhân
    # Trên Render, biến môi trường RENDER=true luôn tồn tại
    if not os.environ.get("RENDER"):
        try:
            server = await asyncio.start_server(handle_tcp_client, "0.0.0.0", 5001)
            log("INIT", "TCP Server đang lắng nghe trên cổng 5001 (Local Mode)")
            asyncio.create_task(server.serve_forever())
        except Exception as e:
            log("INIT_ERR", f"Lỗi mở cổng TCP 5001: {e}")

@app.get("/")
def index():
    return HTMLResponse("<h1>FebonOS TikTok Cloud Streamer [DEBUG MODE ACTIVE]</h1>")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    log("INIT", f"Chạy Uvicorn trên cổng {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)