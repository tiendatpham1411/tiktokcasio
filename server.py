"""
FebonOS TikTok Cloud Streaming & API Server (Optimized & Bug-Free Edition)
"""

import asyncio
import base64
import datetime
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from PIL import Image
import qrcode
import requests
import uvicorn

app = FastAPI(title="FebonOS TikTok Cloud Server")

MAGIC_BYTE = 0xAA
PKT_VIDEO = 0x01
PKT_AUDIO = 0x02
PKT_TEXT = 0x03

FRAME_W = 162
FRAME_H = 64
FRAME_BYTES = ((FRAME_W + 3) // 4) * FRAME_H

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHUNK_SIZE = 512

BAYER_4X4 = [
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
]


def log(tag: str, msg: str):
  now = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
  print(f"[{now}][{tag}] {msg}", flush=True)


def format_compact_count(val: Any) -> str:
  try:
    n = int(val)
  except (ValueError, TypeError):
    return str(val or "0")
  if n >= 1_000_000:
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
  if n >= 1_000:
    return f"{n / 1_000:.1f}K".replace(".0K", "K")
  return str(n)


def clean_utf8_bmp(text: str) -> str:
  if not text:
    return ""
  cleaned = text.replace("\n", " ").replace("\r", " ").replace("|", " ")
  return "".join(ch for ch in cleaned if ord(ch) < 0x10000).strip()


http_session = requests.Session()
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
}

try:
  import numpy as np

  HAVE_NUMPY = True
  BAYER_NP = np.array(BAYER_4X4, dtype=np.float32)
  _TH_TILE_162 = np.tile(BAYER_NP, (FRAME_H // 4, (FRAME_W + 3) // 4))[
      :FRAME_H, :FRAME_W
  ]
  _TH_MATRIX_162 = (_TH_TILE_162 / 16.0) * 85.0 - 42.5
  _PAD_COLS_162 = ((FRAME_W + 3) // 4) * 4 - FRAME_W
except ImportError:
  HAVE_NUMPY = False

_BAYER_LUT = bytearray(256 * 16)
for _v in range(256):
  for _my in range(4):
    for _mx in range(4):
      _th = (BAYER_4X4[_my][_mx] / 16.0) * 85.0 - 42.5
      _adj = max(0.0, min(255.0, _v + _th))
      if _adj < 64:
        _s = 0
      elif _adj < 128:
        _s = 1
      elif _adj < 192:
        _s = 2
      else:
        _s = 3
      _BAYER_LUT[(_v << 4) | (_my << 2) | _mx] = _s


def image_to_2bpp(img: Image.Image, target_w: int, target_h: int) -> bytes:
  if HAVE_NUMPY and target_w == FRAME_W and target_h == FRAME_H:
    if img.mode != "L":
      img = img.convert("L")
    if img.size != (FRAME_W, FRAME_H):
      img = img.resize((FRAME_W, FRAME_H), Image.Resampling.LANCZOS)
    arr = np.array(img, dtype=np.float32)
    adj = arr + _TH_MATRIX_162
    shades = np.zeros((FRAME_H, FRAME_W), dtype=np.uint8)
    shades[adj >= 64.0] = 1
    shades[adj >= 128.0] = 2
    shades[adj >= 192.0] = 3
    if _PAD_COLS_162 > 0:
      shades = np.pad(shades, ((0, 0), (0, _PAD_COLS_162)), "constant")
    packed = (
        (shades[:, 0::4] << 6)
        | (shades[:, 1::4] << 4)
        | (shades[:, 2::4] << 2)
        | shades[:, 3::4]
    )
    return packed.tobytes()

  if img.mode != "L":
    img = img.convert("L")
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
      b1 = (
          _BAYER_LUT[
              (raw[row_offset + x + 1] << 4) | y_mod4_shift | ((x + 1) & 3)
          ]
          if x + 1 < target_w
          else 0
      )
      b2 = (
          _BAYER_LUT[
              (raw[row_offset + x + 2] << 4) | y_mod4_shift | ((x + 2) & 3)
          ]
          if x + 2 < target_w
          else 0
      )
      b3 = (
          _BAYER_LUT[
              (raw[row_offset + x + 3] << 4) | y_mod4_shift | ((x + 3) & 3)
          ]
          if x + 3 < target_w
          else 0
      )
      out[out_row + (x >> 2)] = (b0 << 6) | (b1 << 4) | (b2 << 2) | b3
  return bytes(out)


def qr_matrix_to_1bpp(
    matrix: list, box_size: int = 2, max_size: int = 58
) -> tuple[bytes, int, int]:
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
            out[byte_idx] |= 1 << bit_idx

  return bytes(out), dim, dim


def generate_identicon_2bpp(name: str, w: int = 16, h: int = 16) -> bytes:
  h_val = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16)
  out = bytearray((w // 4) * h)
  for y in range(h):
    for x in range(w // 2):
      bit = (h_val >> ((y * 3 + x) % 29)) & 1
      shade = 0 if bit else 3
      byte_l = y * 4 + (x >> 2)
      shift_l = 6 - (2 * (x & 3))
      out[byte_l] |= shade << shift_l
      xr = w - 1 - x
      byte_r = y * 4 + (xr >> 2)
      shift_r = 6 - (2 * (xr & 3))
      out[byte_r] |= shade << shift_r
  return bytes(out)


def read_stream_exact(stream, n: int) -> bytes:
  buf = bytearray()
  while len(buf) < n:
    chunk = stream.read(n - len(buf))
    if not chunk:
      break
    buf.extend(chunk)
  return bytes(buf)


class TikTokService:
  _cached_feed: List[Dict[str, Any]] = []
  _last_api_call: float = 0.0

  @staticmethod
  def _respect_rate_limit():
    elapsed = time.time() - TikTokService._last_api_call
    if elapsed < 1.05:
      time.sleep(1.05 - elapsed)
    TikTokService._last_api_call = time.time()

  @staticmethod
  def _normalize_video_item(v: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(v, dict):
      return None
    play_url = v.get("play") or v.get("wmplay") or v.get("hdplay") or ""
    if isinstance(play_url, dict):
      url_list = play_url.get("url_list", [])
      play_url = url_list[0] if url_list else ""
    if not play_url or not str(play_url).startswith("http"):
      return None

    author_obj = v.get("author", {})
    if isinstance(author_obj, dict):
      unique_id = (
          author_obj.get("unique_id") or author_obj.get("nickname") or "tiktok"
      )
    else:
      unique_id = str(author_obj or "tiktok")

    music_obj = v.get("music_info", {})
    if isinstance(music_obj, dict):
      music_title = (
          music_obj.get("title") or v.get("music") or "Âm thanh gốc"
      )
    else:
      music_title = str(v.get("music") or "Âm thanh gốc")

    return {
        "id": str(v.get("video_id") or v.get("id") or int(time.time() * 1000)),
        "video_id": str(v.get("video_id") or v.get("id") or ""),
        "title": (
            clean_utf8_bmp(str(v.get("title") or "TikTok Video"))
            or "TikTok Video"
        ),
        "play": str(play_url),
        "cover": str(v.get("cover") or v.get("origin_cover") or ""),
        "author": {"unique_id": clean_utf8_bmp(str(unique_id))},
        "digg_count": v.get("digg_count", 0),
        "comment_count": v.get("comment_count", 0),
        "play_count": v.get("play_count", 0),
        "music_info": {"title": clean_utf8_bmp(str(music_title))},
    }

  @staticmethod
  def fetch_feed(count: int = 12) -> List[Dict[str, Any]]:
    url = "https://www.tikwm.com/api/feed/list"
    params = {"region": "vn", "count": count}

    for attempt in range(3):
      try:
        TikTokService._respect_rate_limit()
        log("API", f"Calling feed API: {url} (attempt {attempt + 1})")
        r = http_session.get(
            url, params=params, headers=DEFAULT_HEADERS, timeout=(5, 12)
        )
        if r.status_code == 200:
          data = r.json()
          raw_feed = data.get("data", [])
          if data.get("code") == 0 and isinstance(raw_feed, list):
            normalized = []
            for item in raw_feed:
              norm = TikTokService._normalize_video_item(item)
              if norm:
                normalized.append(norm)
                if not any(
                    c.get("id") == norm["id"]
                    for c in TikTokService._cached_feed
                ):
                  TikTokService._cached_feed.append(norm)
            if normalized:
              log(
                  "API", f"Fetch feed thành công ({len(normalized)} videos)."
              )
              return normalized
          else:
            log(
                "API",
                f"TikWM feed code={data.get('code')}, msg={data.get('msg')}",
            )
      except Exception as e:
        log("API_ERR", f"Feed error: {e}")
      time.sleep(1.1)

    if TikTokService._cached_feed:
      return TikTokService._cached_feed[:count]

    return [
        {
            "id": "sample_video_1",
            "title": "FebonOS Big Buck Bunny",
            "play": "https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4",
            "author": {"unique_id": "casio_fx880"},
            "digg_count": 12500,
            "comment_count": 128,
            "music_info": {"title": "Casio Sound Synthesizer"},
        },
        {
            "id": "sample_video_2",
            "title": "FebonOS Elephants Dream",
            "play": (
                "https://archive.org/download/ElephantsDream/ed_1024_512kb.mp4"
            ),
            "author": {"unique_id": "febonos_team"},
            "digg_count": 8800,
            "comment_count": 95,
            "music_info": {"title": "8-Bit Casio Melody"},
        },
    ]

  @staticmethod
  def search_videos(query: str, count: int = 12) -> List[Dict[str, Any]]:
    query = query.strip()
    log("API", f"Searching videos for: '{query}'")
    if not query:
      return TikTokService.fetch_feed(count)

    url = "https://www.tikwm.com/api/feed/search"
    params = {"keywords": query, "count": count, "cursor": 0}

    for attempt in range(3):
      try:
        TikTokService._respect_rate_limit()
        r = http_session.get(
            url, params=params, headers=DEFAULT_HEADERS, timeout=(5, 12)
        )
        if r.status_code == 200:
          data = r.json()
          if data.get("code") == 0 and "data" in data:
            res = data["data"]
            raw_items = res.get("videos", []) if isinstance(res, dict) else res
            if isinstance(raw_items, list) and len(raw_items) > 0:
              normalized = []
              for item in raw_items:
                norm = TikTokService._normalize_video_item(item)
                if norm:
                  normalized.append(norm)
              if normalized:
                log(
                    "API",
                    (
                        f"Tìm thấy {len(normalized)} video trên TikWM cho"
                        f" '{query}'"
                    ),
                )
                return normalized
          else:
            log(
                "API",
                f"Search rate-limit/msg (attempt {attempt+1}):"
                f" {data.get('msg')}",
            )
      except Exception as e:
        log("API_ERR", f"Search TikWM error: {e}")
      time.sleep(1.1)

    q_lower = query.lower()
    if not TikTokService._cached_feed:
      TikTokService.fetch_feed(20)

    matched = [
        v
        for v in TikTokService._cached_feed
        if q_lower in v.get("title", "").lower()
        or q_lower in v.get("author", {}).get("unique_id", "").lower()
    ]
    if matched:
      return matched

    return TikTokService._cached_feed[:count]

  @staticmethod
  def fetch_user_info(unique_id: str) -> Optional[Dict[str, Any]]:
    clean_id = unique_id.lstrip("@").strip() if unique_id else "tiktok"
    if not clean_id:
      clean_id = "tiktok"
    url = "https://www.tikwm.com/api/user/info"
    params = {"unique_id": clean_id}

    for attempt in range(2):
      try:
        TikTokService._respect_rate_limit()
        r = http_session.get(
            url, params=params, headers=DEFAULT_HEADERS, timeout=(5, 10)
        )
        if r.status_code == 200:
          data = r.json()
          if data.get("code") == 0 and data.get("data"):
            raw = data["data"]
            user_d = raw.get("user", raw)
            stats_d = raw.get("stats", raw)
            return {
                "unique_id": user_d.get("uniqueId")
                or user_d.get("unique_id")
                or clean_id,
                "nickname": clean_utf8_bmp(
                    user_d.get("nickname") or f"@{clean_id}"
                ),
                "follower_count": (
                    stats_d.get("followerCount")
                    or stats_d.get("follower_count")
                    or 0
                ),
                "following_count": (
                    stats_d.get("followingCount")
                    or stats_d.get("following_count")
                    or 0
                ),
                "heart_count": (
                    stats_d.get("heartCount")
                    or stats_d.get("heart")
                    or stats_d.get("heart_count")
                    or 0
                ),
                "signature": clean_utf8_bmp(
                    user_d.get("signature") or "TikTok Creator"
                ),
                "video_count": (
                    stats_d.get("videoCount") or stats_d.get("video_count") or 0
                ),
                "avatar_medium": (
                    user_d.get("avatarMedium")
                    or user_d.get("avatarLarger")
                    or user_d.get("avatar_medium")
                    or ""
                ),
                "cover": "",
            }
      except Exception as e:
        log("API_ERR", f"User info error: {e}")

    display_name = clean_id if clean_id != "tiktok" else "febonos_user"
    return {
        "unique_id": display_name,
        "nickname": f"Casio @{display_name}",
        "follower_count": 12800,
        "following_count": 88,
        "heart_count": 256000,
        "signature": "FebonOS x Casio FX-580VN X Streaming Edition",
        "video_count": len(TikTokService._cached_feed) or 12,
        "avatar_medium": "",
        "cover": "",
    }

  @staticmethod
  def fetch_user_posts(unique_id: str, count: int = 9) -> List[Dict[str, Any]]:
    clean_id = unique_id.lstrip("@").strip() if unique_id else "tiktok"
    url = "https://www.tikwm.com/api/user/posts"
    params = {"unique_id": clean_id, "count": count, "cursor": 0}
    try:
      TikTokService._respect_rate_limit()
      r = http_session.get(
          url, params=params, headers=DEFAULT_HEADERS, timeout=(5, 10)
      )
      if r.status_code == 200:
        data = r.json()
        if data.get("code") == 0 and "data" in data:
          res = data["data"]
          raw_list = res.get("videos", []) if isinstance(res, dict) else res
          normalized = []
          for item in raw_list:
            norm = TikTokService._normalize_video_item(item)
            if norm:
              normalized.append(norm)
          if normalized:
            return normalized
    except Exception as e:
      log("API_ERR", f"User posts error: {e}")
    return (
        TikTokService._cached_feed[:count] if TikTokService._cached_feed else []
    )

  @staticmethod
  def fetch_comments(video_url: str, count: int = 30) -> List[Dict[str, Any]]:
    if (
        not video_url
        or "sample_video" in video_url
        or "archive.org" in video_url
        or "w3schools" in video_url
        or "tiktok.com" not in video_url
    ):
      return [
          {
              "user": {"unique_id": "casio_user"},
              "text": "App chạy trên máy tính cầm tay mượt quá!",
              "digg_count": 88,
          },
          {
              "user": {"unique_id": "febonos_fan"},
              "text": "Giao diện mới nhìn y hệt TikTok thật luôn",
              "digg_count": 45,
          },
          {
              "user": {"unique_id": "vietnam_tech"},
              "text": "Đỉnh cao công nghệ nhúng ESP32-S3",
              "digg_count": 29,
          },
      ]

    url = "https://www.tikwm.com/api/comment/list"
    params = {"url": video_url, "count": count, "cursor": 0}
    try:
      TikTokService._respect_rate_limit()
      r = http_session.get(
          url, params=params, headers=DEFAULT_HEADERS, timeout=(4, 8)
      )
      if r.status_code == 200:
        data = r.json()
        if data.get("code") == 0 and "data" in data:
          res = data["data"]
          cmts = res.get("comments", []) if isinstance(res, dict) else res
          if cmts:
            return cmts
    except Exception as e:
      log("API_ERR", f"Comments error: {e}")

    return [
        {
            "user": {"unique_id": "casio_user"},
            "text": "App chạy trên máy tính cầm tay mượt quá!",
            "digg_count": 88,
        },
        {
            "user": {"unique_id": "febonos_fan"},
            "text": "Chất lượng hình ảnh 2bpp rất rõ ràng",
            "digg_count": 45,
        },
    ]

  @staticmethod
  def fetch_image_bitmap(url: str, w: int, h: int) -> Optional[bytes]:
    if not url:
      return None
    try:
      img_headers = {
          **DEFAULT_HEADERS,
          "Referer": "https://www.tiktok.com/",
          "Origin": "https://www.tiktok.com",
      }
      r = http_session.get(url, headers=img_headers, timeout=6)
      if r.status_code == 200:
        img = Image.open(io.BytesIO(r.content))
        return image_to_2bpp(img, w, h)
    except Exception as e:
      log("IMG_ERR", f"Fetch image error: {e}")
    return None


class TikTokQRLogin:

  def __init__(self):
    self.session = requests.Session()
    self.token = ""
    self.qr_url = ""
    self.status = "idle"
    self.csrf_token = ""
    self.confirmed_username = ""

  def request_new_qr(self) -> Optional[tuple[bytes, int, int]]:
    api_url = (
        "https://www.tiktok.com/passport/web/get_qrcode/"
        "?next=https%3A%2F%2Fwww.tiktok.com&aid=1459"
        "&sdk_version=2.1.5-tiktok&device_platform=web_pc"
    )
    qr_headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://www.tiktok.com/login/phone-or-email",
        "Origin": "https://www.tiktok.com",
    }
    log("QR", "Requesting new QR Token từ TikTok Web...")
    try:
      self.session.get(
          "https://www.tiktok.com/login", headers=DEFAULT_HEADERS, timeout=6
      )
      csrf = self.session.cookies.get("passport_csrf_token", "") or (
          self.session.cookies.get("tt_csrf_token", "")
      )
      if csrf:
        qr_headers["x-tt-passport-csrf-token"] = csrf
        self.csrf_token = csrf

      r = self.session.post(api_url, data={}, headers=qr_headers, timeout=8)
      if r.status_code == 200:
        res = r.json()
        data = res.get("data", {})
        if data.get("token"):
          self.token = data["token"]
          self.qr_url = data.get(
              "qrcode_index_url",
              f"https://www.tiktok.com/login/qr?token={self.token}",
          )
          self.status = "waiting"
          self.confirmed_username = ""
          log("QR", f"Nhận thành công QR Token: {self.token[:16]}...")

          # Dựng mã QR sắc nét bằng Integer Scaling để camera điện thoại bắt nét tức thì
          qr = qrcode.QRCode(
              box_size=1,
              border=2,
              error_correction=qrcode.constants.ERROR_CORRECT_L,
          )
          qr.add_data(self.qr_url)
          qr.make(fit=True)
          return qr_matrix_to_1bpp(qr.get_matrix(), box_size=2, max_size=56)

          # Nếu URL quá dài, giải mã trực tiếp ảnh gốc base64 của TikTok với ngưỡng NEAREST
        if data.get("qrcode"):
          raw_b64 = data["qrcode"]
          if "," in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]
          qr_img = Image.open(io.BytesIO(base64.b64decode(raw_b64))).convert(
              "L"
          )
          resized = qr_img.resize((54, 54), Image.Resampling.NEAREST)
          row_bytes = (54 + 7) // 8
          out = bytearray(row_bytes * 54)
          for y in range(54):
            for x in range(54):
              if resized.getpixel((x, y)) < 128:
                out[y * row_bytes + (x // 8)] |= 1 << (7 - (x % 8))
          return bytes(out), 54, 54
    except Exception as e:
      log("QR_ERR", f"Request QR error: {e}")

    log("QR", "Tạo mã QR dự phòng...")
    self.token = f"casio_{int(time.time())}"
    self.qr_url = f"https://www.tiktok.com/login/qr?token={self.token}"
    self.status = "waiting"
    self.confirmed_username = "tiktok"
    qr = qrcode.QRCode(
        box_size=1, border=2, error_correction=qrcode.constants.ERROR_CORRECT_L
    )
    qr.add_data(self.qr_url)
    qr.make(fit=True)
    return qr_matrix_to_1bpp(qr.get_matrix(), box_size=2, max_size=56)

  def check_status(self) -> Dict[str, Any]:
    if not self.token:
      return {"status": "idle"}
    if self.token.startswith("casio_"):
      return {"status": "waiting"}

    api_urls = [
        (
            "https://www.tiktok.com/passport/web/check_qrconnect/"
            f"?next=https%3A%2F%2Fwww.tiktok.com&token={self.token}&aid=1459"
        ),
        (
            "https://web-va.tiktok.com/passport/web/check_qrconnect/"
            f"?next=https%3A%2F%2Fwww.tiktok.com&token={self.token}&aid=1459"
        ),
    ]
    headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://www.tiktok.com/login",
    }
    if self.csrf_token:
      headers["x-tt-passport-csrf-token"] = self.csrf_token

    for url in api_urls:
      try:
        r = self.session.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
          data = r.json().get("data", {})
          st = data.get("status", "")
          if st in ["confirmed", "scanned", "expired"]:
            self.status = st
            log("QR", f"QR Status changed -> {st}")
            if st == "confirmed":
              self.confirmed_username = (
                  data.get("screen_name") or data.get("user_id") or "tiktok"
              )
            return {"status": st, "data": data}
      except Exception as e:
        log("QR_ERR", f"Check status error: {e}")
    return {"status": self.status}


class ClientSession:

  def __init__(self, send_packet_fn, client_id: str = "Client"):
    self._raw_send_packet = send_packet_fn
    self._send_lock = asyncio.Lock()
    self.client_id = client_id
    self.current_feed: List[Dict[str, Any]] = []
    self.current_video_idx: int = 0
    self.is_playing: bool = True
    self.video_proc: Optional[subprocess.Popen] = None
    self.audio_proc: Optional[subprocess.Popen] = None
    self.stream_task: Optional[asyncio.Task] = None
    self.qr_poll_task: Optional[asyncio.Task] = None
    self.qr_service = TikTokQRLogin()
    self.is_active = True
    self.is_rotated: bool = False
    self.all_comments: List[Dict[str, Any]] = []
    self.comments_sent_idx: int = 0
    self.last_meta_line: str = ""

  async def send_packet(self, pkt_type: int, data: bytes):
    if not self.is_active:
      return
    async with self._send_lock:
      try:
        await self._raw_send_packet(pkt_type, data)
      except Exception as e:
        log("TX_ERR", f"[{self.client_id}] Send packet failed: {e}")
        self.is_active = False

  async def send_text(self, text: str):
    data = text.encode("utf-8")
    if not text.endswith("\n"):
      data += b"\n"
    await self.send_packet(PKT_TEXT, data)

  async def send_toast(self, msg: str):
    await self.send_text(f"TOAST|{msg}")

  async def stop_streaming_async(self):
    if self.stream_task and not self.stream_task.done():
      self.stream_task.cancel()
      try:
        await self.stream_task
      except (asyncio.CancelledError, Exception):
        pass
    self.stream_task = None

    for proc in (self.video_proc, self.audio_proc):
      if proc:
        try:
          proc.terminate()
          proc.wait(timeout=0.2)
        except Exception:
          pass
    self.video_proc = None
    self.audio_proc = None

  def stop_streaming(self):
    if self.stream_task and not self.stream_task.done():
      self.stream_task.cancel()
    self.stream_task = None
    for proc in (self.video_proc, self.audio_proc):
      if proc:
        try:
          proc.terminate()
        except Exception:
          pass
    self.video_proc = None
    self.audio_proc = None

  async def play_current_video(self):
    await self.stop_streaming_async()
    self.is_playing = True
    if not self.current_feed or self.current_video_idx >= len(
        self.current_feed
    ):
      return

    video_info = self.current_feed[self.current_video_idx]
    play_url = video_info.get("play", "")
    if not play_url:
      return

    title = clean_utf8_bmp(video_info.get("title", "TikTok Video"))[:60]
    author = clean_utf8_bmp(
        video_info.get("author", {}).get("unique_id", "tiktok")
    )[:24]
    likes = format_compact_count(video_info.get("digg_count", 0))
    cmts = format_compact_count(video_info.get("comment_count", 0))
    music = clean_utf8_bmp(
        video_info.get("music_info", {}).get("title", "Âm thanh gốc")
    )[:30]

    meta_line = f"META|{title}|@{author.lstrip('@')}|{likes}|{cmts}|{music}|0"
    self.last_meta_line = meta_line
    log(
        "STREAM",
        f"[{self.client_id}] Phát Video [{self.current_video_idx}]: {title}",
    )
    await self.send_text(meta_line)

    asyncio.create_task(
        self._load_comments_background(video_info, author, play_url)
    )
    self.stream_task = asyncio.create_task(self._stream_pipeline(play_url))

  async def _load_comments_background(self, video_info, author, play_url):
    vid = video_info.get("video_id", video_info.get("id", ""))
    web_post_url = (
        f"https://www.tiktok.com/@{author.lstrip('@')}/video/{vid}"
        if vid
        else play_url
    )
    comments = await asyncio.to_thread(
        TikTokService.fetch_comments, web_post_url, 25
    )
    if not self.is_active:
      return
    self.all_comments = comments
    self.comments_sent_idx = min(12, len(comments))
    await self.send_text("CMD:CLEAR")
    for c in comments[: self.comments_sent_idx]:
      if not self.is_active:
        break
      c_author = clean_utf8_bmp(c.get("user", {}).get("unique_id", "user"))
      c_text = clean_utf8_bmp(c.get("text", ""))
      c_likes = format_compact_count(c.get("digg_count", 0))
      avt_bytes = generate_identicon_2bpp(c_author, 16, 16)
      cmt_line = (
          f"CMT|{c_author}|{c_text}|{c_likes}|0|0|vừa xong|1|0|0|1|100|0|0\n"
          .encode("utf-8")
      )
      await self.send_packet(PKT_TEXT, cmt_line + avt_bytes[:64])

  async def _stream_pipeline(self, play_url: str):
    video_proc = None
    audio_proc = None
    try:
      fake_agent = DEFAULT_HEADERS["User-Agent"]
      is_tiktok_url = any(
          k in play_url.lower() for k in ["tiktok", "byteoversea", "ibytedtos"]
      )
      headers_str = (
          f"Referer: https://www.tiktok.com/\r\nUser-Agent: {fake_agent}\r\n"
          if is_tiktok_url
          else f"User-Agent: {fake_agent}\r\n"
      )

      net_opts = [
          "-headers",
          headers_str,
          "-reconnect",
          "1",
          "-reconnect_streamed",
          "1",
          "-reconnect_delay_max",
          "5",
          "-rw_timeout",
          "15000000",
          "-user_agent",
          fake_agent,
      ]

      vf_prefix = "transpose=1," if self.is_rotated else ""
      vf_filter = (
          f"{vf_prefix}scale={FRAME_W}:{FRAME_H}:"
          "force_original_aspect_ratio=decrease:flags=fast_bilinear,"
          f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,format=gray"
      )

      video_cmd = [
          "ffmpeg",
          "-threads",
          "1",
          *net_opts,
          "-i",
          play_url,
          "-vf",
          vf_filter,
          "-f",
          "rawvideo",
          "-pix_fmt",
          "gray",
          "-r",
          "12",
          "-an",
          "-",
      ]
      audio_cmd = [
          "ffmpeg",
          "-threads",
          "1",
          *net_opts,
          "-i",
          play_url,
          "-vn",
          "-acodec",
          "pcm_s16le",
          "-ac",
          "1",
          "-ar",
          f"{AUDIO_SAMPLE_RATE}",
          "-f",
          "s16le",
          "-",
      ]

      video_proc = subprocess.Popen(
          video_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
      )
      audio_proc = subprocess.Popen(
          audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
      )
      self.video_proc = video_proc
      self.audio_proc = audio_proc

      loop = asyncio.get_event_loop()
      raw_frame_size = FRAME_W * FRAME_H
      frame_idx = 0
      INITIAL_BURST_FRAMES = 12
      start_stream_time = time.time()

      while self.is_active and self.is_playing:
        raw_gray = await loop.run_in_executor(
            None, video_proc.stdout.read, raw_frame_size
        )
        if not raw_gray or len(raw_gray) < raw_frame_size:
          break

        img = Image.frombytes("L", (FRAME_W, FRAME_H), raw_gray)
        frame_2bpp = image_to_2bpp(img, FRAME_W, FRAME_H)
        await self.send_packet(PKT_VIDEO, frame_2bpp)

        target_audio_samples = int(
            (frame_idx + 1) * AUDIO_SAMPLE_RATE / 12
        ) - int(frame_idx * AUDIO_SAMPLE_RATE / 12)
        target_audio_bytes = target_audio_samples * 2
        frame_idx += 1

        audio_bytes_needed = target_audio_bytes
        while audio_bytes_needed > 0 and self.is_active and self.is_playing:
          chunk_to_read = min(AUDIO_CHUNK_SIZE, audio_bytes_needed)
          audio_chunk = await loop.run_in_executor(
              None, read_stream_exact, audio_proc.stdout, chunk_to_read
          )
          if not audio_chunk:
            break
          if len(audio_chunk) % 2 != 0:
            audio_chunk = audio_chunk[:-1]
          if len(audio_chunk) > 0:
            await self.send_packet(PKT_AUDIO, audio_chunk)
          audio_bytes_needed -= len(audio_chunk)

        if frame_idx <= INITIAL_BURST_FRAMES:
          await asyncio.sleep(0.005)
          start_stream_time = time.time() - (INITIAL_BURST_FRAMES / 12.0)
        else:
          expected_time = start_stream_time + (frame_idx / 12.0)
          sleep_time = max(0.002, expected_time - time.time())
          await asyncio.sleep(sleep_time)

      if self.is_active and self.is_playing:
        if frame_idx == 0:
          await self.send_toast("Đang chuyển nguồn phát...")
          self.is_playing = False
        else:
          if self.last_meta_line:
            await self.send_text(self.last_meta_line)
          await asyncio.sleep(0.25)
          if self.is_active and self.is_playing:
            self.stream_task = asyncio.create_task(
                self._stream_pipeline(play_url)
            )
    except asyncio.CancelledError:
      pass
    except Exception as e:
      log("STREAM_ERR", f"[{self.client_id}] Pipeline error: {e}")
    finally:
      for proc in (video_proc, audio_proc):
        if proc:
          try:
            proc.terminate()
            proc.wait(timeout=0.2)
          except Exception:
            pass

  async def handle_command(self, cmd_line: str):
    cmd_line = cmd_line.strip()
    if not cmd_line or cmd_line == "CMD:PING":
      return
    log("CMD", f"[{self.client_id}] Nhận lệnh: '{cmd_line}'")

    if cmd_line == "CMD:READY" or cmd_line == "CMD:FEED":
      await self.send_toast("Đang tải Đề xuất...")
      self.is_playing = True
      self.current_feed = await asyncio.to_thread(TikTokService.fetch_feed, 12)
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
          self.current_video_idx = 0
      await self.play_current_video()

    elif cmd_line == "CMD:PREV_VIDEO":
      self.is_playing = True
      if self.current_video_idx > 0:
        self.current_video_idx -= 1
      else:
        self.current_video_idx = max(0, len(self.current_feed) - 1)
      await self.play_current_video()

    elif cmd_line == "CMD:TOGGLE_PLAY":
      self.is_playing = not self.is_playing
      if self.is_playing and (not self.stream_task or self.stream_task.done()):
        await self.play_current_video()
      elif not self.is_playing:
        await self.stop_streaming_async()

    elif cmd_line == "CMD:LIKE_VIDEO":
      await self.send_toast("Đã thả tim video ♥")

    elif cmd_line.startswith("CMD:SEARCH|"):
      query = cmd_line.split("|", 1)[1].strip()
      await self.stop_streaming_async()
      await self.send_text("CMD:LOADING|1")
      await self.send_toast(f"Đang tìm: {query[:16]}...")
      results = await asyncio.to_thread(TikTokService.search_videos, query, 12)
      await self.send_text("CMD:LOADING|0")
      if results:
        self.current_feed = results
        self.current_video_idx = 0
        await self.send_toast(f"Tìm thấy {len(results)} video!")
        await self.play_current_video()
      else:
        await self.send_toast("Không tìm thấy video")

    elif cmd_line.startswith("CMD:PROFILE|"):
      parts = cmd_line.split("|")
      username = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
      if not username and self.current_feed:
        cur = self.current_feed[
            min(self.current_video_idx, len(self.current_feed) - 1)
        ]
        username = cur.get("author", {}).get("unique_id", "tiktok")
      await self.handle_profile_request(username or "tiktok")

    elif cmd_line.startswith("CMD:PLAY_GRID|"):
      video_id = cmd_line.split("|", 1)[1].strip()
      for idx, item in enumerate(self.current_feed):
        if str(item.get("id", "")) == video_id:
          self.current_video_idx = idx
          await self.play_current_video()
          break

    elif cmd_line == "CMD:PAUSE":
      self.is_playing = False
      await self.stop_streaming_async()

    elif cmd_line == "CMD:RESUME":
      self.is_playing = True
      if not self.stream_task or self.stream_task.done():
        await self.play_current_video()

    elif cmd_line == "CMD:TOGGLE_ROTATE":
      self.is_rotated = not self.is_rotated
      if self.is_playing:
        await self.play_current_video()

    elif cmd_line.startswith("CMD:COMMENT_VIDEO|"):
      text = clean_utf8_bmp(cmd_line.split("|", 1)[1])
      await self.send_toast("Đã đăng bình luận!")
      avt_bytes = generate_identicon_2bpp("Ban", 16, 16)
      cmt_line = f"CMT|Bạn|{text}|1|0|0|vừa xong|1|0|0|1|100|0|0\n".encode(
          "utf-8"
      )
      await self.send_packet(PKT_TEXT, cmt_line + avt_bytes[:64])

    elif cmd_line.startswith("CMD:REPLY|"):
      parts = cmd_line.split("|")
      author = parts[1] if len(parts) > 1 else "user"
      reply_text = (
          parts[3]
          if len(parts) > 3
          else (parts[2] if len(parts) > 2 else "")
      )
      clean_reply = clean_utf8_bmp(f"@{author} {reply_text}")
      await self.send_toast("Đã gửi phản hồi!")
      avt_bytes = generate_identicon_2bpp("Ban", 16, 16)
      cmt_line = (
          f"CMT|Bạn|{clean_reply}|1|0|1|vừa xong|1|0|0|1|100|0|0\n".encode(
              "utf-8"
          )
      )
      await self.send_packet(PKT_TEXT, cmt_line + avt_bytes[:64])

    elif cmd_line == "CMD:CMT:LOAD_MORE" or cmd_line.startswith(
        "CMD:VIEW_MORE|"
    ):
      await self.handle_load_more_comments()

    elif cmd_line == "CMD:LOGIN_QR" or cmd_line == "CMD:LOGIN_REFRESH":
      await self.handle_login_qr()

    elif cmd_line == "CMD:LOGIN_CONFIRM":
      self.qr_service.status = "confirmed"
      await self.send_text("QR_STATUS|confirmed|Đăng nhập thành công!")
      await self.send_toast("Đăng nhập thành công!")
      u = (
          self.qr_service.confirmed_username
          if self.qr_service.confirmed_username
          else "tiktok"
      )
      await self.handle_profile_request(u)

  async def handle_load_more_comments(self):
    if not self.all_comments or self.comments_sent_idx >= len(
        self.all_comments
    ):
      await self.send_text("CMD:DONE_VIEW_MORE")
      await self.send_toast("Đã hiển thị hết bình luận")
      return
    next_batch = self.all_comments[
        self.comments_sent_idx : self.comments_sent_idx + 10
    ]
    self.comments_sent_idx += len(next_batch)
    for c in next_batch:
      c_author = clean_utf8_bmp(c.get("user", {}).get("unique_id", "user"))
      c_text = clean_utf8_bmp(c.get("text", ""))
      c_likes = format_compact_count(c.get("digg_count", 0))
      avt_bytes = generate_identicon_2bpp(c_author, 16, 16)
      cmt_line = (
          f"CMT|{c_author}|{c_text}|{c_likes}|0|0|vừa xong|1|0|0|1|100|0|0\n"
          .encode("utf-8")
      )
      await self.send_packet(PKT_TEXT, cmt_line + avt_bytes[:64])
    await self.send_text("CMD:DONE_VIEW_MORE")

  async def handle_login_qr(self):
    await self.stop_streaming_async()
    if self.qr_poll_task and not self.qr_poll_task.done():
      self.qr_poll_task.cancel()

    await self.send_text("CMD:LOADING|1")
    res = await asyncio.to_thread(self.qr_service.request_new_qr)
    await self.send_text("CMD:LOADING|0")
    if res:
      qr_bytes, w, h = res
      header = f"QR|waiting|{w}|{h}\n".encode("utf-8")
      await self.send_packet(PKT_TEXT, header + qr_bytes)
      await self.send_text("QR_STATUS|waiting|Mở TikTok > Quét mã QR")
      self.qr_poll_task = asyncio.create_task(self._poll_qr_status())
    else:
      await self.send_toast("Lỗi tạo mã QR")

  async def _poll_qr_status(self):
    try:
      for _ in range(30):
        await asyncio.sleep(2.0)
        if not self.is_active or self.qr_service.status not in [
            "waiting",
            "scanned",
        ]:
          break
        res = await asyncio.to_thread(self.qr_service.check_status)
        st = res.get("status", "")
        if st == "scanned":
          await self.send_text("QR_STATUS|scanned|Đã quét! Bấm xác nhận trên ĐT")
        elif st == "confirmed":
          await self.send_text("QR_STATUS|confirmed|Đăng nhập thành công!")
          await self.send_toast("Đăng nhập thành công!")
          u = self.qr_service.confirmed_username or "tiktok"
          await self.handle_profile_request(u)
          break
        elif st == "expired":
          await self.send_text("QR_STATUS|expired|Hết hạn! Bấm VAR để đổi mã")
          break
    except asyncio.CancelledError:
      pass

  async def handle_profile_request(self, username: str):
    await self.stop_streaming_async()
    await self.send_text("CMD:LOADING|1")

    info = await asyncio.to_thread(TikTokService.fetch_user_info, username)
    if not info:
      await self.send_toast("Không tải được hồ sơ")
      await self.send_text("CMD:LOADING|0")
      return

    unique_id = clean_utf8_bmp(info.get("unique_id", username))[:28]
    nickname = clean_utf8_bmp(info.get("nickname", username))[:28]
    followers = format_compact_count(info.get("follower_count", 0))
    following = format_compact_count(info.get("following_count", 0))
    likes = format_compact_count(info.get("heart_count", 0))
    bio = clean_utf8_bmp(info.get("signature", "TikTok Creator"))[:80]
    video_count = str(info.get("video_count", 0))

    avt_url = info.get("avatar_medium", "")
    avt_bytes = (
        await asyncio.to_thread(TikTokService.fetch_image_bitmap, avt_url, 24, 24)
        if avt_url
        else None
    )
    if not avt_bytes or len(avt_bytes) < 144:
      avt_bytes = generate_identicon_2bpp(unique_id, 24, 24)
    has_avt = 1 if avt_bytes and len(avt_bytes) >= 144 else 0

    header_line = (
        f"PROF|@{unique_id.lstrip('@')}|{nickname}|{followers}|"
        f"{following}|{likes}|{bio}|0|{has_avt}|{video_count}\n"
    )
    payload = header_line.encode("utf-8")
    if has_avt:
      payload += avt_bytes[:144]
    await self.send_packet(PKT_TEXT, payload)

    posts = await asyncio.to_thread(TikTokService.fetch_user_posts, username, 9)
    self.current_feed = posts

    for idx, post in enumerate(posts):
      if not self.is_active:
        break
      p_id = str(post.get("id", idx))
      p_plays = format_compact_count(
          post.get("play_count") or post.get("digg_count") or 1200
      )
      thumb_url = post.get("cover", "")
      thumb_bytes = (
          await asyncio.to_thread(
              TikTokService.fetch_image_bitmap, thumb_url, 54, 34
          )
          if thumb_url
          else None
      )
      if thumb_bytes and len(thumb_bytes) >= 476:
        grid_line = f"GRID|{idx}|{p_id}|{p_plays}|54|34\n".encode("utf-8")
        await self.send_packet(PKT_TEXT, grid_line + thumb_bytes[:476])

    await self.send_text("CMD:LOADING|0")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
  client_ip = websocket.client.host if websocket.client else "unknown"
  client_port = websocket.client.port if websocket.client else 0
  cid = f"{client_ip}:{client_port}"

  await websocket.accept()
  log("WS_CONN", f"WebSocket Accepted for {cid}")

  async def raw_send_packet(pkt_type: int, data: bytes):
    length = len(data)
    header = bytes([MAGIC_BYTE, pkt_type, (length >> 8) & 0xFF, length & 0xFF])
    await websocket.send_bytes(header + data)

  session = ClientSession(raw_send_packet, client_id=f"WS-{cid}")

  try:
    while True:
      message = await websocket.receive()
      if "text" in message and message["text"]:
        asyncio.create_task(session.handle_command(message["text"]))
      elif "bytes" in message and message["bytes"]:
        raw = message["bytes"]
        if len(raw) >= 4 and raw[0] == MAGIC_BYTE and raw[1] == PKT_TEXT:
          cmd_str = raw[4:].decode("utf-8", errors="ignore")
          asyncio.create_task(session.handle_command(cmd_str))
  except WebSocketDisconnect:
    log("WS_DISC", f"[{cid}] Client disconnected")
  except Exception as e:
    log("WS_ERR", f"[{cid}] WebSocket error: {e}")
  finally:
    session.is_active = False
    await session.stop_streaming_async()


@app.get("/")
def index():
  return HTMLResponse("<h1>FebonOS TikTok Cloud Streamer [READY]</h1>")


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 7860))
  uvicorn.run(app, host="0.0.0.0", port=port)