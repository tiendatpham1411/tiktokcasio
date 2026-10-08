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

def qr_to_1bpp(matrix: list, target_size: int = 48) -> bytes:
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
            if pixels[x, y] == 0:
                byte_idx = y * row_bytes + (x // 8)
                bit_idx = 7 - (x % 8)
                out[byte_idx] |= (1 << bit_idx)
                
    return bytes(out)

class TikTokService:
    @staticmethod
    def fetch_feed(count: int = 10) -> List[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/feed/list"
        params = {"region": "vn", "count": count}
        log("API", f"Calling TikWM feed API (timeout=10s): {url}")
        
        for attempt in range(2):
            try:
                t0 = time.time()
                r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=(4, 10))
                elapsed = time.time() - t0
                log("API", f"Attempt {attempt+1}: Status={r.status_code}, Time={elapsed:.2f}s")
                if r.status_code == 200:
                    data = r.json()
                    feed = data.get("data", [])
                    if data.get("code") == 0 and len(feed) > 0:
                        log("API", f"Fetch feed thành công! Lấy được {len(feed)} videos.")
                        return feed
                    else:
                        log("API", f"TikWM trả về mã lỗi code: {data.get('code')}, msg: {data.get('msg')}")
            except Exception as e:
                log("API_ERR", f"Attempt {attempt+1} fetch_feed error: {type(e).__name__} - {e}")
                time.sleep(0.5)

        log("API", "TikWM API không phản hồi! Kích hoạt video mẫu Fallback...")
        return [
            {
                "id": "sample_video_1",
                "title": "FebonOS Demo Stream (Fallback)",
                "play": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
                "author": {"unique_id": "casio_fx580"},
                "digg_count": 8888,
                "comment_count": 99,
                "music_info": {"title": "Casio Sound Synthesizer"}
            }
        ]

    @staticmethod
    def search_videos(query: str, count: int = 10) -> List[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/feed/search"
        params = {"keywords": query, "count": count}
        log("API", f"Searching videos for: '{query}'")
        try:
            r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=8)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                res = data["data"]
                items = res.get("videos", res) if isinstance(res, dict) else res
                log("API", f"Tìm thấy {len(items)} kết quả cho '{query}'")
                return items
        except Exception as e:
            log("API_ERR", f"Search error: {e}")
        return []

    @staticmethod
    def fetch_user_info(unique_id: str) -> Optional[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/user/info"
        params = {"unique_id": unique_id}
        log("API", f"Fetching user info: '{unique_id}'")
        try:
            r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=8)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                log("API", f"Lấy thành công user info: {unique_id}")
                return data["data"]
        except Exception as e:
            log("API_ERR", f"User info error: {e}")
        return None

    @staticmethod
    def fetch_user_posts(unique_id: str, count: int = 12) -> List[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/user/posts"
        params = {"unique_id": unique_id, "count": count, "cursor": 0}
        log("API", f"Fetching user posts: '{unique_id}'")
        try:
            r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=8)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                res = data["data"]
                return res.get("videos", []) if isinstance(res, dict) else res
        except Exception as e:
            log("API_ERR", f"User posts error: {e}")
        return []

    @staticmethod
    def fetch_comments(video_url: str, count: int = 30) -> List[Dict[str, Any]]:
        url = "https://www.tikwm.com/api/comment/list"
        params = {"url": video_url, "count": count, "cursor": 0}
        log("API", f"Fetching comments for: {video_url}")
        try:
            r = http_session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=6)
            data = r.json()
            if data.get("code") == 0 and "data" in data:
                res = data["data"]
                cmts = res.get("comments", []) if isinstance(res, dict) else res
                log("API", f"Lấy được {len(cmts)} bình luận.")
                return cmts
        except Exception as e:
            log("API_ERR", f"Comments error: {e}")
        return []

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

    def request_new_qr(self) -> Optional[bytes]:
        api_url = "https://www.tiktok.com/passport/web/get_qrcode/"
        params = {"aid": "1459", "language": "vi-VN"}
        log("QR", "Requesting new QR Token từ TikTok Web...")
        try:
            r = http_session.get(api_url, params=params, headers=DEFAULT_HEADERS, timeout=6)
            res = r.json()
            if res.get("data", {}).get("token"):
                self.token = res["data"]["token"]
                self.qr_url = res["data"].get("qrcode_index_url", f"https://www.tiktok.com/login/qr?token={self.token}")
                self.status = "waiting"
                log("QR", f"Token: {self.token}, URL: {self.qr_url}")
                qr = qrcode.QRCode(version=1, box_size=1, border=1)
                qr.add_data(self.qr_url)
                qr.make(fit=True)
                return qr_to_1bpp(qr.get_matrix(), 48)
        except Exception as e:
            log("QR_ERR", f"Request QR error: {e}")
        return None

    def check_status(self) -> Dict[str, Any]:
        if not self.token:
            return {"status": "idle"}
        api_url = "https://www.tiktok.com/passport/web/check_qrconnect/"
        params = {"aid": "1459", "token": self.token}
        try:
            r = http_session.get(api_url, params=params, headers=DEFAULT_HEADERS, timeout=5)
            data = r.json().get("data", {})
            st = data.get("status", "")
            if st in ["confirmed", "scanned", "expired"]:
                self.status = st
                log("QR", f"Status changed: {st}")
                return {"status": st, "data": data}
        except Exception as e:
            log("QR_ERR", f"Check status error: {e}")
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

            video_cmd = [
                "ffmpeg", "-threads", "1",
                "-user_agent", fake_agent,
                "-i", play_url,
                "-vf", f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease:flags=fast_bilinear,pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,format=gray",
                "-f", "rawvideo", "-pix_fmt", "gray", "-r", "12", "-an", "-"
            ]
            
            audio_cmd = [
                "ffmpeg", "-threads", "1",
                "-user_agent", fake_agent,
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
            self.current_feed = await asyncio.to_thread(TikTokService.fetch_feed, 10)
            self.current_video_idx = 0
            await self.play_current_video()

        elif cmd_line == "CMD:NEXT_VIDEO":
            if self.current_video_idx + 1 < len(self.current_feed):
                self.current_video_idx += 1
            else:
                new_items = await asyncio.to_thread(TikTokService.fetch_feed, 10)
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
            log("CMD", f"[{self.client_id}] Chế độ Play: {self.is_playing}")
            if self.is_playing and not self.stream_task:
                await self.play_current_video()

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

        elif cmd_line == "CMD:LOGIN_QR":
            await self.handle_login_qr()

    async def handle_login_qr(self):
        self.stop_streaming()
        qr_bytes = await asyncio.to_thread(self.qr_service.request_new_qr)
        if qr_bytes:
            header = f"QR|waiting|48|48\n".encode('utf-8')
            await self.send_packet(PKT_TEXT, header + qr_bytes)
            asyncio.create_task(self._poll_qr_status())
        else:
            await self.send_toast("Lỗi tạo mã QR")

    async def _poll_qr_status(self):
        for _ in range(30):
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
                await self.handle_profile_request("tiktok")
                break
            elif st == "expired":
                await self.send_text("QR_STATUS|expired|Mã QR đã hết hạn!")
                break

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
    log("INIT", "Khởi động Server...")
    try:
        server = await asyncio.start_server(handle_tcp_client, "0.0.0.0", 5001)
        log("INIT", "TCP Server đang lắng nghe trên cổng 5001")
        asyncio.create_task(server.serve_forever())
    except Exception as e:
        log("INIT_ERR", f"Không thể lắng nghe cổng TCP 5001 (có thể do môi trường Cloud hạn chế đa port): {e}")

@app.get("/")
def index():
    return HTMLResponse("<h1>FebonOS TikTok Cloud Streamer [DEBUG MODE ACTIVE]</h1>")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    log("INIT", f"Chạy Uvicorn trên cổng {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)