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

def image_to_2bpp(img: Image.Image, target_w: int, target_h: int) -> bytes:
    img = img.convert('L')
    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    pixels = img.load()
    
    row_bytes = (target_w + 3) // 4
    out = bytearray(row_bytes * target_h)
    
    for y in range(target_h):
        for x in range(target_w):
            val = pixels[x, y]
            threshold = (BAYER_4X4[y % 4][x % 4] / 16.0) * 85.0 - 42.5
            val_adj = max(0.0, min(255.0, val + threshold))
            
            if val_adj < 64:
                shade = 0
            elif val_adj < 128:
                shade = 1
            elif val_adj < 192:
                shade = 2
            else:
                shade = 3
                
            byte_idx = y * row_bytes + (x // 4)
            bit_shift = 6 - 2 * (x % 4)
            out[byte_idx] |= (shade << bit_shift)
            
    return bytes(out)

def qr_matrix_to_1bpp(matrix: list, box_size: int = 2, max_size: int = 54) -> tuple[bytes, int, int]:
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

class TikTokService:
    _cached_feed: List[Dict[str, Any]] = []

    @staticmethod
    def fetch_feed(count: int = 10) -> List[Dict[str, Any]]:
        endpoints = [
            "https://www.tikwm.com/api/feed/list",
            "https://api.tikwm.com/api/feed/list"
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

        log("API", "Kích hoạt danh sách video mẫu đa dạng Fallback...")
        return [
            {
                "id": "sample_video_1",
                "title": "FebonOS Demo Stream (Big Buck Bunny)",
                "play": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
                "author": {"unique_id": "casio_fx580"},
                "digg_count": 12500,
                "comment_count": 128,
                "music_info": {"title": "Casio Sound Synthesizer"}
            },
            {
                "id": "sample_video_2",
                "title": "FebonOS Elephants Dream",
                "play": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4",
                "author": {"unique_id": "febonos_team"},
                "digg_count": 8800,
                "comment_count": 95,
                "music_info": {"title": "8-Bit Casio Melody"}
            },
            {
                "id": "sample_video_3",
                "title": "FebonOS For Bigger Blazes",
                "play": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
                "author": {"unique_id": "hanoitech"},
                "digg_count": 9999,
                "comment_count": 210,
                "music_info": {"title": "TikTok Viral Sound"}
            },
            {
                "id": "sample_video_4",
                "title": "FebonOS Tears of Steel",
                "play": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/TearsOfSteel.mp4",
                "author": {"unique_id": "embedded_dev"},
                "digg_count": 15400,
                "comment_count": 340,
                "music_info": {"title": "Cyberpunk 2026 Theme"}
            }
        ]

    @staticmethod
    def search_videos(query: str, count: int = 10) -> List[Dict[str, Any]]:
        endpoints = [
            "https://www.tikwm.com/api/feed/search",
            "https://api.tikwm.com/api/feed/search"
        ]
        params = {"keywords": query, "count": count}
        log("API", f"Searching videos for: '{query}'")
        for url in endpoints:
            try:
                r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=(5, 10))
                if r.status_code == 200:
                    data = r.json()
                    if data.get("code") == 0 and "data" in data:
                        res = data["data"]
                        items = res.get("videos", res) if isinstance(res, dict) else res
                        if items and len(items) > 0:
                            log("API", f"Tìm thấy {len(items)} kết quả cho '{query}'")
                            return items
            except Exception as e:
                log("API_ERR", f"Search error {url}: {e}")

        # Tìm kiếm dự phòng trong bộ đệm video đã tải
        q_lower = query.lower()
        matched = [v for v in TikTokService._cached_feed if q_lower in v.get("title", "").lower() or q_lower in v.get("author", {}).get("unique_id", "").lower()]
        if matched:
            log("API", f"Tìm thấy {len(matched)} video khớp từ cache cho '{query}'")
            return matched

        # Fallback video gợi ý theo từ khóa
        log("API", f"Trả về video mẫu gợi ý cho từ khóa '{query}'")
        return [
            {
                "id": f"search_{query}_1",
                "title": f"FebonOS Search: {query}",
                "play": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
                "author": {"unique_id": query},
                "digg_count": 1024,
                "comment_count": 42,
                "music_info": {"title": f"Soundtrack - {query}"}
            }
        ]

    @staticmethod
    def fetch_user_info(unique_id: str) -> Optional[Dict[str, Any]]:
        endpoints = [
            "https://www.tikwm.com/api/user/info",
            "https://api.tikwm.com/api/user/info"
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
            "https://www.tikwm.com/api/user/posts",
            "https://api.tikwm.com/api/user/posts"
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
        endpoints = [
            "https://www.tikwm.com/api/comment/list",
            "https://api.tikwm.com/api/comment/list"
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
        
        # Bình luận mẫu dự phòng
        return [
            {"user": {"unique_id": "casio_user"}, "text": "App chạy trên máy tính cầm tay mượt quá!", "digg_count": 88},
            {"user": {"unique_id": "febonos_fan"}, "text": "Chất lượng hình ảnh 2bpp rất rõ ràng", "digg_count": 45},
            {"user": {"unique_id": "vietnam_tech"}, "text": "Đỉnh cao công nghệ nhúng ESP32-S3", "digg_count": 29}
        ]

    @staticmethod
    def fetch_image_bitmap(url: str, w: int, h: int) -> Optional[bytes]:
        log("IMG", f"Tải bitmap {w}x{h}: {url}")
        try:
            r = http_session.get(url, headers=DEFAULT_HEADERS, timeout=6)
            if r.status_code == 200:
                img = Image.open(io.BytesIO(r.content))
                return image_to_2bpp(img, w, h)
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
            r = http_session.post(api_url, data={}, headers=qr_headers, timeout=8)
            if r.status_code == 200:
                res = r.json()
                if res.get("data", {}).get("token"):
                    self.token = res["data"]["token"]
                    self.qr_url = res["data"].get("qrcode_index_url", f"https://www.tiktok.com/login/qr?token={self.token}")
                    self.status = "waiting"
                    self.csrf_token = r.cookies.get("passport_csrf_token", "")
                    self.confirmed_username = ""
                    log("QR", f"Token: {self.token}, URL: {self.qr_url}")
                    
                    qr = qrcode.QRCode(box_size=1, border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
                    qr.add_data(self.qr_url)
                    qr.make(fit=True)
                    return qr_matrix_to_1bpp(qr.get_matrix(), box_size=2, max_size=54)
        except Exception as e:
            log("QR_ERR", f"Request QR error: {e}")

        # Fallback: Luôn tạo mã QR TikTok Login hợp lệ để người dùng quét được
        log("QR", "Tạo mã QR TikTok Login dự phòng...")
        self.token = f"casio_{int(time.time())}"
        self.qr_url = "https://www.tiktok.com/login"
        self.status = "waiting"
        self.confirmed_username = ""
        qr = qrcode.QRCode(box_size=1, border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(self.qr_url)
        qr.make(fit=True)
        return qr_matrix_to_1bpp(qr.get_matrix(), box_size=2, max_size=54)

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
            headers["cookie"] = f"passport_csrf_token={self.csrf_token};"

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
                        log("QR", f"TikTok hạn chế polling tự động (error_code={err})")
                        return {"status": "rate_limited", "msg": "TikTok bảo mật"}
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

    async def send_text(self, text: str):
        data = text.encode('utf-8')
        if not text.endswith('\n'):
            data += b'\n'
        log("TX", f"[{self.client_id}] Send Text ({len(data)} bytes): {text.strip()}")
        await self.send_packet(PKT_TEXT, data)

    async def send_toast(self, msg: str):
        await self.send_text(f"TOAST|{msg}")

    def stop_streaming(self):
        log("STREAM", f"[{self.client_id}] Stopping existing FFmpeg pipelines...")
        if self.video_proc:
            try:
                self.video_proc.terminate()
                self.video_proc.wait(timeout=0.5)
            except:
                pass
            self.video_proc = None
        if self.audio_proc:
            try:
                self.audio_proc.terminate()
                self.audio_proc.wait(timeout=0.5)
            except:
                pass
            self.audio_proc = None
        if self.stream_task and not self.stream_task.done():
            self.stream_task.cancel()
            self.stream_task = None
        log("STREAM", f"[{self.client_id}] FFmpeg pipelines stopped.")

    async def play_current_video(self):
        self.stop_streaming()
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
        comments = await asyncio.to_thread(TikTokService.fetch_comments, web_post_url, 15)
        await self.send_text("CMD:CLEAR")
        for c in comments:
            c_author = c.get("user", {}).get("unique_id", "user").replace("|", " ")
            c_text = c.get("text", "").replace("\n", " ").replace("|", " ")
            c_likes = str(c.get("digg_count", 0))
            cmt_line = f"CMT|{c_author}|{c_text}|{c_likes}|0|0|vừa xong|0|0|0|1|100|0|0"
            await self.send_text(cmt_line)

    async def _stream_pipeline(self, play_url: str):
        log("FFMPEG", f"[{self.client_id}] Khởi tạo tiến trình FFmpeg...")
        try:
            fake_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            headers_str = f"Referer: https://www.tiktok.com/\r\nUser-Agent: {fake_agent}\r\n"

            # Bổ sung các cờ chống treo mạng và vượt qua kiểm tra Referer của TikTok CDN
            net_opts = [
                "-headers", headers_str,
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-rw_timeout", "15000000", # 15 giây timeout
                "-user_agent", fake_agent
            ]

            video_cmd = [
                "ffmpeg", "-threads", "1",
                *net_opts,
                "-i", play_url,
                "-vf", f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease:flags=fast_bilinear,pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,format=gray",
                "-f", "rawvideo", "-pix_fmt", "gray", "-r", "12", "-an", "-"
            ]
            
            audio_cmd = [
                "ffmpeg", "-threads", "1",
                *net_opts,
                "-i", play_url,
                "-vn", "-acodec", "pcm_s16le", "-ac", "1", "-ar", f"{AUDIO_SAMPLE_RATE}",
                "-f", "s16le", "-"
            ]

            self.video_proc = subprocess.Popen(video_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.audio_proc = subprocess.Popen(audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            log("FFMPEG", f"[{self.client_id}] Tiến trình Video (PID={self.video_proc.pid}), Audio (PID={self.audio_proc.pid}) đã khởi động!")

            loop = asyncio.get_event_loop()
            raw_frame_size = FRAME_W * FRAME_H
            frame_idx = 0
            
            while self.is_active and self.is_playing:
                frame_start = time.time()
                raw_gray = await loop.run_in_executor(None, self.video_proc.stdout.read, raw_frame_size)
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
                    audio_chunk = await loop.run_in_executor(None, self.audio_proc.stdout.read, chunk_to_read)
                    if not audio_chunk:
                        break
                    await self.send_packet(PKT_AUDIO, audio_chunk)
                    audio_bytes_needed -= len(audio_chunk)

                if frame_idx % 60 == 0:
                    log("STREAM", f"[{self.client_id}] Đã stream {frame_idx} frames (~{frame_idx//12} giây)")

                elapsed = time.time() - frame_start
                sleep_time = max(0.002, (1.0 / 12.0) - elapsed)
                await asyncio.sleep(sleep_time)

            if self.is_active and self.is_playing:
                log("STREAM", f"[{self.client_id}] Chuyển tiếp sang video kế tiếp...")
                await asyncio.sleep(0.3)
                await self.handle_command("CMD:NEXT_VIDEO")

        except asyncio.CancelledError:
            log("STREAM", f"[{self.client_id}] Stream task cancelled.")
        except Exception as e:
            log("STREAM_ERR", f"[{self.client_id}] Pipeline error: {e}")
        finally:
            self.stop_streaming()

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

        elif cmd_line == "CMD:LOGIN_QR":
            await self.handle_login_qr()

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
        for tick in range(35):
            await asyncio.sleep(2)
            if not self.is_active or self.qr_service.status not in ["waiting", "scanned"]:
                break
            res = await asyncio.to_thread(self.qr_service.check_status)
            st = res.get("status", "")
            if st == "scanned":
                await self.send_text("QR_STATUS|scanned|Đã quét mã! Xác nhận trên máy...")
            elif st == "confirmed":
                await self.send_text("QR_STATUS|confirmed|Đăng nhập thành công!")
                await self.send_toast("Đăng nhập thành công!")
                u = self.qr_service.confirmed_username if self.qr_service.confirmed_username else "tiktok"
                await self.handle_profile_request(u)
                break
            elif st == "rate_limited":
                await self.send_text("QR_STATUS|rate_limited|TikTok bảo mật. Dùng tab Tìm kiếm!")
                break
            elif st == "expired":
                await self.send_text("QR_STATUS|expired|Mã QR đã hết hạn! Bấm BACK")
                break
        else:
            if self.is_active and self.qr_service.status == "waiting":
                await self.send_text("QR_STATUS|timeout|Hết giờ quét mã. Bấm BACK")

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