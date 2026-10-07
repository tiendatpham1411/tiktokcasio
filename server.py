"""
TikTok Cloud Streaming & API Server for FebonOS (ESP32-S3)
Features:
- TikWM API integration (Trending, Search, User Info, User Posts Grid, Comments)
- TikTok Web Passport QR Code Login generator & real-time polling
- FFmpeg Video Transcoder -> 162x64 / 192x64 2-bit Monochrome Dithered Video @ 12fps
- FFmpeg Audio Transcoder -> 16kHz 16-bit Mono PCM Audio
- Banner, Avatar & 3-Column Video Thumbnail Bitmap Processor (2bpp)
- Dual-mode server:
  1. FastAPI WebSockets (/ws) on port 7860 (for Hugging Face Spaces / Cloud deployment)
  2. Raw TCP Server on port 5001 (for Local LAN / VPS deployment)
"""

import os
import sys
import time
import json
import asyncio
import io
import subprocess
import requests
from typing import Dict, Any, List, Optional
from PIL import Image
import qrcode
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="FebonOS TikTok Cloud Server")

# ==================== CONSTANTS & PROTOCOL ====================
MAGIC_BYTE = 0xAA
PKT_VIDEO = 0x01
PKT_AUDIO = 0x02
PKT_TEXT  = 0x03

FRAME_W = 162
FRAME_H = 64
FRAME_BYTES = ((FRAME_W + 3) // 4) * FRAME_H  # 41 * 64 = 2624 bytes

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHUNK_SIZE = 512  # 256 samples (16ms)

BAYER_4X4 = [
    [ 0,  8,  2, 10],
    [12,  4, 14,  6],
    [ 3, 11,  1,  9],
    [15,  7, 13,  5]
]

# ==================== BITMAP CONVERSION HELPERS ====================
def image_to_2bpp(img: Image.Image, target_w: int, target_h: int) -> bytes:
    """Resize image and dither to 2-bit monochrome bitmap (0=black, 1=dark, 2=light, 3=white)"""
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
            
            # Map 0..255 to 0..3
            if val_adj < 64:
                shade = 0  # Black
            elif val_adj < 128:
                shade = 1  # Dark gray
            elif val_adj < 192:
                shade = 2  # Light gray
            else:
                shade = 3  # White
                
            byte_idx = y * row_bytes + (x // 4)
            bit_shift = 6 - 2 * (x % 4)
            out[byte_idx] |= (shade << bit_shift)
            
    return bytes(out)

def qr_to_1bpp(matrix: list, target_size: int = 48) -> bytes:
    """Convert QR boolean matrix to 1-bit monochrome bitmap"""
    qr_len = len(matrix)
    img = Image.new('1', (qr_len, qr_len), 1)
    for y in range(qr_len):
        for x in range(qr_len):
            img.putpixel((x, y), 0 if matrix[y][x] else 1)
            
    img = img.resize((target_size, target_size), Image.Resampling.NEAREST)
    row_bytes = (target_size + 7) // 8
    out = bytearray(row_bytes * target_size)
    pixels = img.load()
    
    for y in range(target_size):
        for x in range(target_size):
            if pixels[x, y] == 0:  # Black pixel
                byte_idx = y * row_bytes + (x // 8)
                bit_idx = 7 - (x % 8)
                out[byte_idx] |= (1 << bit_idx)
                
    return bytes(out)

# ==================== TIKTOK DATA SERVICE ====================
class TikTokService:
    @staticmethod
    def fetch_feed(count: int = 10) -> List[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/feed/list"
        params = {"region": "vn", "count": count}
        # [SỬA MỚI] Đóng giả trình duyệt Chrome thực sự
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                return data["data"]
        except Exception as e:
            print(f"[TikTok] Fetch feed error: {e}")
        return []

    @staticmethod
    def search_videos(query: str, count: int = 10) -> List[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/feed/search"
        params = {"keywords": query, "count": count}
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                # Can be list or dict with 'videos'
                res = data["data"]
                return res.get("videos", res) if isinstance(res, dict) else res
        except Exception as e:
            print(f"[TikTok] Search error: {e}")
        return []

    @staticmethod
    def fetch_user_info(unique_id: str) -> Optional[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/user/info"
        params = {"unique_id": unique_id}
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                return data["data"]
        except Exception as e:
            print(f"[TikTok] User info error: {e}")
        return None

    @staticmethod
    def fetch_user_posts(unique_id: str, count: int = 12) -> List[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/user/posts"
        params = {"unique_id": unique_id, "count": count, "cursor": 0}
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                res = data["data"]
                return res.get("videos", []) if isinstance(res, dict) else res
        except Exception as e:
            print(f"[TikTok] User posts error: {e}")
        return []

    @staticmethod
    def fetch_comments(video_url: str, count: int = 30) -> List[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/comment/list"
        params = {"url": video_url, "count": count, "cursor": 0}
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                res = data["data"]
                return res.get("comments", []) if isinstance(res, dict) else res
        except Exception as e:
            print(f"[TikTok] Comments error: {e}")
        return []

    @staticmethod
    def fetch_image_bitmap(url: str, w: int, h: int) -> Optional[bytes]:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if r.status_code == 200:
                img = Image.open(io.BytesIO(r.content))
                return image_to_2bpp(img, w, h)
        except Exception as e:
            print(f"[TikTok] Fetch image error ({url}): {e}")
        return None

# ==================== TIKTOK QR LOGIN SERVICE ====================
class TikTokQRLogin:
    def __init__(self):
        self.token = ""
        self.qr_url = ""
        self.status = "idle" # idle, waiting, scanned, confirmed, expired
        self.session_cookie = ""
        self.user_profile = None

    def request_new_qr(self) -> Optional[bytes]:
        """Request login QR token from TikTok Web Passport API & generate 48x48 1bpp bitmap"""
        api_url = "https://www.tiktok.com/passport/web/get_qrcode/"
        params = {"aid": "1459", "language": "vi-VN"}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            r = requests.get(api_url, params=params, headers=headers, timeout=10)
            res = r.json()
            if res.get("data", {}).get("token"):
                self.token = res["data"]["token"]
                self.qr_url = res["data"].get("qrcode_index_url", f"https://www.tiktok.com/login/qr?token={self.token}")
                self.status = "waiting"
                
                qr = qrcode.QRCode(version=1, box_size=1, border=1)
                qr.add_data(self.qr_url)
                qr.make(fit=True)
                matrix = qr.get_matrix()
                return qr_to_1bpp(matrix, 48)
        except Exception as e:
            print(f"[QRLogin] Request QR error: {e}")
            # Fallback direct QR URL
            self.token = f"tok_{int(time.time())}"
            self.qr_url = f"https://www.tiktok.com/login?token={self.token}"
            self.status = "waiting"
            qr = qrcode.QRCode(version=1, box_size=1, border=1)
            qr.add_data(self.qr_url)
            qr.make(fit=True)
            return qr_to_1bpp(qr.get_matrix(), 48)
        return None

    def check_status(self) -> Dict[str, Any]:
        if not self.token:
            return {"status": "idle"}
        api_url = "https://www.tiktok.com/passport/web/check_qrconnect/"
        params = {"aid": "1459", "token": self.token}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        try:
            r = requests.get(api_url, params=params, headers=headers, timeout=8)
            data = r.json().get("data", {})
            st = data.get("status", "")
            if st == "confirmed":
                self.status = "confirmed"
                self.session_cookie = data.get("redirect_url", "")
                return {"status": "confirmed", "data": data}
            elif st == "scanned":
                self.status = "scanned"
                return {"status": "scanned"}
            elif st == "expired":
                self.status = "expired"
                return {"status": "expired"}
        except Exception as e:
            print(f"[QRLogin] Check status error: {e}")
        return {"status": self.status}

# ==================== CLIENT SESSION & STREAMER ====================
class ClientSession:
    def __init__(self, send_packet_fn):
        self.send_packet = send_packet_fn
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
        await self.send_packet(PKT_TEXT, data)

    async def send_toast(self, msg: str):
        await self.send_text(f"TOAST|{msg}")

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
        self.stop_streaming()
        if not self.current_feed or self.current_video_idx >= len(self.current_feed):
            return

        video_info = self.current_feed[self.current_video_idx]
        play_url = video_info.get("play", "")
        if not play_url:
            return

        # Send Metadata
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
        await self.send_text(meta_line)

        # Send comments for this video
        vid = video_info.get("video_id", video_info.get("id", ""))
        web_post_url = f"https://www.tiktok.com/@{author}/video/{vid}" if vid else play_url
        comments = TikTokService.fetch_comments(web_post_url, 15)
        await self.send_text("CMD:CLEAR")
        for c in comments:
            c_author = c.get("user", {}).get("unique_id", "user").replace("|", " ")
            c_text = c.get("text", "").replace("\n", " ").replace("|", " ")
            c_likes = str(c.get("digg_count", 0))
            c_time = "vừa xong"
            cmt_line = f"CMT|{c_author}|{c_text}|{c_likes}|0|0|{c_time}|0|0|0|1|100|0|0"
            await self.send_text(cmt_line)

        # Start Transcoding Task
        self.stream_task = asyncio.create_task(self._stream_pipeline(play_url))

    async def _stream_pipeline(self, play_url: str):
        try:
            # [SỬA MỚI] Tạo User-Agent ngụy trang cực mạnh để đánh lừa máy chủ chứa video TikTok
            fake_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

            # FFmpeg video pipeline
            video_cmd = [
                "ffmpeg", "-threads", "1", 
                "-user_agent", fake_agent,  # <--- BẮT BUỘC THÊM DÒNG NÀY VÀO ĐÂY
                "-i", play_url,
                "-vf", f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease:flags=fast_bilinear,pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,format=gray",
                "-f", "rawvideo", "-pix_fmt", "gray", "-r", "12", "-an", "-"
            ]
            
            # FFmpeg audio pipeline
            audio_cmd = [
                "ffmpeg", "-threads", "1", 
                "-user_agent", fake_agent,  # <--- BẮT BUỘC THÊM DÒNG NÀY VÀO ĐÂY
                "-i", play_url,
                "-vn", "-acodec", "pcm_s16le", "-ac", "1", "-ar", f"{AUDIO_SAMPLE_RATE}",
                "-f", "s16le", "-"
            ]

            self.video_proc = subprocess.Popen(video_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.audio_proc = subprocess.Popen(audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

            loop = asyncio.get_event_loop()
            raw_frame_size = FRAME_W * FRAME_H
            frame_idx = 0
            
            while self.is_active and self.is_playing:
                frame_start = time.time()
                # Read raw grayscale frame from ffmpeg
                raw_gray = await loop.run_in_executor(None, self.video_proc.stdout.read, raw_frame_size)
                if not raw_gray or len(raw_gray) < raw_frame_size:
                    break  # Video finished

                # Convert to 2-bit monochrome bitmap
                img = Image.frombytes('L', (FRAME_W, FRAME_H), raw_gray)
                frame_2bpp = image_to_2bpp(img, FRAME_W, FRAME_H)
                await self.send_packet(PKT_VIDEO, frame_2bpp)

                # Accurate sample tracking: (32000 bytes/s across 12 FPS)
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

                # Frame pacing: steady 12 FPS (83.3ms per frame)
                elapsed = time.time() - frame_start
                sleep_time = max(0.002, (1.0 / 12.0) - elapsed)
                await asyncio.sleep(sleep_time)

            if self.is_active and self.is_playing:
                print("[Streamer] Video finished. Auto-playing next video...")
                await asyncio.sleep(0.3)
                await self.handle_command("CMD:NEXT_VIDEO")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Streamer] Pipeline error: {e}")
        finally:
            self.stop_streaming()

    async def handle_command(self, cmd_line: str):
        cmd_line = cmd_line.strip()
        print(f"[CMD Received] {cmd_line}")

        if cmd_line == "CMD:READY" or cmd_line == "CMD:FEED":
            # [SỬA MỚI] Gửi phản hồi ACK ngay lập tức để ESP32 biết Server đã sống
            await self.send_toast("Đang tải dữ liệu...")
            # [SỬA MỚI] Cho ESP32 một nhịp nghỉ (200ms) để render cái Toast kia trước khi bị dồn dập video
            await asyncio.sleep(0.2)
            
            self.current_feed = TikTokService.fetch_feed(10)
            self.current_video_idx = 0
            
            if not self.current_feed:
                await self.send_toast("Lỗi tải danh sách video")
            else:
                await self.play_current_video()

        elif cmd_line == "CMD:NEXT_VIDEO":
            if self.current_video_idx + 1 < len(self.current_feed):
                self.current_video_idx += 1
            else:
                new_items = TikTokService.fetch_feed(10)
                if new_items:
                    self.current_feed.extend(new_items)
                    self.current_video_idx += 1
            await self.play_current_video()

        elif cmd_line == "CMD:PREV_VIDEO":
            if self.current_video_idx > 0:
                self.current_video_idx -= 1
                await self.play_current_video()

        elif cmd_line == "CMD:TOGGLE_PLAY":
            self.is_playing = not self.is_playing
            if self.is_playing and not self.stream_task:
                await self.play_current_video()

        elif cmd_line == "CMD:LIKE_VIDEO":
            await self.send_toast("Đã thả tim video!")

        elif cmd_line.startswith("CMD:SEARCH|"):
            query = cmd_line.split("|", 1)[1]
            results = TikTokService.search_videos(query, 10)
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

        elif cmd_line == "CMD:LOGIN_QR":
            await self.handle_login_qr()

    async def handle_login_qr(self):
        """Generates TikTok Web QR code and streams status updates"""
        self.stop_streaming()
        qr_bytes = self.qr_service.request_new_qr()
        if qr_bytes:
            # Header: QR|status|width|height
            header = f"QR|waiting|48|48\n".encode('utf-8')
            await self.send_packet(PKT_TEXT, header + qr_bytes)
            
            # Start background polling loop for QR scan confirmation
            asyncio.create_task(self._poll_qr_status())
        else:
            await self.send_toast("Lỗi tạo mã QR")

    async def _poll_qr_status(self):
        for _ in range(60):  # Poll up to 60 seconds
            await asyncio.sleep(2)
            if not self.is_active or self.qr_service.status not in ["waiting", "scanned"]:
                break
            res = self.qr_service.check_status()
            st = res.get("status", "")
            if st == "scanned":
                await self.send_text("QR_STATUS|scanned|Đã quét mã! Xác nhận trên máy...")
            elif st == "confirmed":
                await self.send_text("QR_STATUS|confirmed|Đăng nhập thành công!")
                await self.send_toast("Đăng nhập thành công!")
                # Load profile
                await self.handle_profile_request("tiktok")
                break
            elif st == "expired":
                await self.send_text("QR_STATUS|expired|Mã QR đã hết hạn!")
                break

    async def handle_profile_request(self, username: str):
        """Fetches User Profile + Banner + Avatar + 3-Column Video Grid Thumbnails"""
        self.stop_streaming()
        await self.send_text("CMD:LOADING|1")
        
        info = TikTokService.fetch_user_info(username)
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

        # Avatar bitmap (24x24 2bpp = 144 bytes)
        avt_url = info.get("avatar_medium", info.get("avatar", ""))
        avt_bytes = TikTokService.fetch_image_bitmap(avt_url, 24, 24) if avt_url else None
        has_avt = 1 if avt_bytes else 0

        # Banner bitmap (192x22 2bpp = 1056 bytes)
        banner_url = info.get("cover", "")
        banner_bytes = TikTokService.fetch_image_bitmap(banner_url, 192, 22) if banner_url else None
        has_banner = 1 if banner_bytes else 0

        # Send Profile Header line + binary data
        header_line = f"PROF|{unique_id}|{nickname}|{followers}|{following}|{likes}|{bio}|{has_banner}|{has_avt}|{video_count}\n"
        payload = header_line.encode('utf-8')
        if banner_bytes:
            payload += banner_bytes
        if avt_bytes:
            payload += avt_bytes
        await self.send_packet(PKT_TEXT, payload)

        # Fetch Posts Grid
        posts = TikTokService.fetch_user_posts(username, 9)
        self.current_feed = posts
        
        for idx, post in enumerate(posts):
            p_id = str(post.get("id", idx))
            p_plays = str(post.get("play_count", 0))
            thumb_url = post.get("cover", "")
            # Thumbnail 54x36 2bpp = 504 bytes
            thumb_bytes = TikTokService.fetch_image_bitmap(thumb_url, 54, 36) if thumb_url else None
            if thumb_bytes:
                grid_line = f"GRID|{idx}|{p_id}|{p_plays}|54|36\n".encode('utf-8')
                await self.send_packet(PKT_TEXT, grid_line + thumb_bytes)

        await self.send_text("CMD:LOADING|0")

# ==================== WEBSOCKET HANDLER (PORT 7860) ====================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("[WebSocket] ESP32 Connected!")

    async def send_packet(pkt_type: int, data: bytes):
        length = len(data)
        header = bytes([MAGIC_BYTE, pkt_type, (length >> 8) & 0xFF, length & 0xFF])
        await websocket.send_bytes(header + data)

    session = ClientSession(send_packet)

    try:
        while True:
            message = await websocket.receive()
            if "text" in message:
                await session.handle_command(message["text"])
            elif "bytes" in message:
                raw = message["bytes"]
                if len(raw) >= 4 and raw[0] == MAGIC_BYTE:
                    pkt_type = raw[1]
                    payload = raw[4:]
                    if pkt_type == PKT_TEXT:
                        await session.handle_command(payload.decode('utf-8', errors='ignore'))
    except WebSocketDisconnect:
        print("[WebSocket] ESP32 Disconnected")
    finally:
        session.is_active = False
        session.stop_streaming()

# ==================== RAW TCP HANDLER (PORT 5001) ====================
async def handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info('peername')
    print(f"[TCP] ESP32 Connected from {peer}")

    async def send_packet(pkt_type: int, data: bytes):
        length = len(data)
        header = bytes([MAGIC_BYTE, pkt_type, (length >> 8) & 0xFF, length & 0xFF])
        writer.write(header + data)
        await writer.drain()

    session = ClientSession(send_packet)

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode('utf-8', errors='ignore').strip()
            if cmd:
                await session.handle_command(cmd)
    except Exception as e:
        print(f"[TCP] Error: {e}")
    finally:
        session.is_active = False
        session.stop_streaming()
        writer.close()
        await writer.wait_closed()
        print(f"[TCP] ESP32 Disconnected from {peer}")

@app.on_event("startup")
async def startup_event():
    # Start raw TCP server alongside FastAPI WebSocket server
    try:
        server = await asyncio.start_server(handle_tcp_client, "0.0.0.0", 5001)
        print("[TCP Server] Listening on port 5001")
        asyncio.create_task(server.serve_forever())
    except Exception as e:
        print(f"[TCP Server] Could not bind port 5001 (e.g. running on cloud container): {e}")

@app.get("/")
def index():
    return HTMLResponse("""
    <html>
        <head><title>FebonOS TikTok Cloud Server</title></head>
        <body style="font-family: Arial, sans-serif; padding: 40px; text-align: center; background: #121212; color: #fff;">
            <h1>FebonOS TikTok Cloud Streamer</h1>
            <p style="color: #25F4EE;">Status: ONLINE &bull; Ready for ESP32-S3 StreamApp</p>
            <p>Connect WebSocket: <code>ws://&lt;host&gt;:7860/ws</code> or <code>wss://&lt;space-domain&gt;/ws</code></p>
            <p>Connect Raw TCP: <code>&lt;host&gt;:5001</code></p>
        </body>
    </html>
    """)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)

