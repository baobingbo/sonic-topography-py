# -*- coding: utf-8 -*-
# services.py
import asyncio
import json
import os
from pathlib import Path

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import httpx

# ==================== 配置 ====================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PLAYLISTS_PATH = DATA_DIR / "playlists.json"
DIST_DIR = BASE_DIR / "dist"  # 前端文件目录

# 确保数据目录存在
DATA_DIR.mkdir(exist_ok=True)


# ==================== 配置与常量 ====================
NETEASE_HEADERS = {
    "Referer": "https://music.163.com/",
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Connection": "close",
}

NETEASE_COOKIE_HEADER = "x-netease-cookie"

# 缓存 TTL
PLAYABLE_URL_CACHE_TTL = 1000 * 60 * 10  # 10分钟
SEARCH_CACHE_TTL = 1000 * 60 * 5        # 5分钟

# ==================== 工具类与缓存 ====================
class SimpleCache:
    """简易缓存类，模拟 JS 的 Map"""
    def __init__(self):
        self._store: Dict[str, Tuple[object, float]] = {}
    
    def get(self, key: str) -> Optional[object]:
        if key in self._store:
            value, expires_at = self._store[key]
            if expires_at > asyncio.get_event_loop().time():
                return value
            else:
                self.delete(key)
        return None
    
    def set(self, key: str, value: object, ttl: int = 300):
        expires_at = asyncio.get_event_loop().time() + ttl / 1000
        self._store[key] = (value, expires_at)
    
    def delete(self, key: str):
        self._store.pop(key, None)

playable_url_cache = SimpleCache()
search_cache = SimpleCache()

# ==================== 核心服务逻辑 ====================
class NeteaseMusicService:
    def __init__(self):
        self.browser_cookie = ""
        self._semaphore = asyncio.Semaphore(8)  # 限制并发数，模拟 JS 的 batchSize

    def normalize_cookie(self, value: str) -> str:
        """清洗 Cookie 字符串"""
        if not value:
            return ""
        lines = [line.strip().rstrip(";") for line in value.split("\n") if line.strip()]
        return "; ".join(lines)

    def create_headers(self, cookie: str, extra_headers: dict = None) -> dict:
        """创建请求头"""
        headers = NETEASE_HEADERS.copy()
        normalized_cookie = self.normalize_cookie(cookie)
        if normalized_cookie:
            headers["Cookie"] = normalized_cookie
        if extra_headers:
            headers.update(extra_headers)
        return headers

    async def fetch_json_with_retry(self, url: str, kwargs: dict, retries: int = 2) -> dict:
        """带重试机制的请求"""
        last_data = None
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.request(url=url, **kwargs)
                    data = response.json()
                    last_data = data
                    # 模拟 JS 逻辑：状态码 OK 且 code 不等于 400
                    if response.is_success and data.get("code") != 400:
                        return data
            except Exception as e:
                print(f"Request failed: {e}")
            
            if attempt < retries:
                await asyncio.sleep(0.180 * (attempt + 1))  # 指数退避
        
        return last_data or {}

    async def get_netease_playable_url(self, song_id: str, cookie: str = "") -> Optional[str]:
        """获取歌曲播放地址 (核心缓存逻辑)"""
        cache_key = f"{song_id}::{self.normalize_cookie(cookie)}"
        cached = playable_url_cache.get(cache_key)
        if cached:
            return cached
        
        url = f"https://music.163.com/api/song/enhance/player/url"
        params = {
            "id": song_id,
            "ids": f"[{song_id}]",
            "br": "320000"
        }
        
        data = await self.fetch_json_with_retry(
            url, 
            {"method": "GET", "params": params, "headers": self.create_headers(cookie)}
        )
        
        playable_url = data.get("data", [{}])[0].get("url")
        
        if playable_url:
            playable_url_cache.set(cache_key, playable_url, PLAYABLE_URL_CACHE_TTL)
        
        return playable_url

    async def map_netease_song(self, song: dict) -> dict:
        """
        将网易云原始歌曲数据映射为标准化格式
        增加了类型安全检查，防止 API 返回异常数据导致崩溃
        """
        # 安全提取 artists (兼容 artists 和 ar)
        artists = song.get("artists") or song.get("ar") or []
        if not isinstance(artists, list):
            artists = []
        artist_name = " / ".join(
            [artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")]
        )

        # 安全提取 album (兼容 album 和 al)
        album = song.get("album") or song.get("al") or {}
        # ✅ 核心修复：确保 album 是字典类型，否则默认为空字典
        if not isinstance(album, dict):
            album = {}

        return {
            "id": song.get("id"),
            "name": song.get("name", ""),
            "artist": artist_name,
            "album": album.get("name", ""),
            "duration": song.get("duration") or song.get("dt") or 0,
            "fee": song.get("fee"),
        }

    async def fetch_netease_search_songs(self, keywords: str, result_limit: int, cookie: str) -> Tuple[List[dict], dict]:
        """搜索歌曲，包含主搜索和备选搜索逻辑"""
        upstream_limit = min(result_limit * 5, 80)
        
        # 主搜索: POST /api/search/get/web
        body = {
            "s": keywords,
            "type": "1",
            "offset": "0",
            "total": "true",
            "limit": str(upstream_limit),
            "_": str(int(asyncio.get_event_loop().time() * 1000))
        }
        
        primary_data = await self.fetch_json_with_retry(
            "https://music.163.com/api/search/get/web",
            {
                "method": "POST",
                "data": body,
                "headers": self.create_headers(cookie, {"Content-Type": "application/x-www-form-urlencoded"})
            }
        )
        primary_songs = primary_data.get("result", {}).get("songs", [])
        
        # 备选搜索: GET /api/cloudsearch/pc
        fallback_url = "https://music.163.com/api/cloudsearch/pc"
        fallback_params = {
            "s": keywords,
            "type": "1",
            "offset": "0",
            "total": "true",
            "limit": str(upstream_limit),
            "_": str(int(asyncio.get_event_loop().time() * 1000))
        }
        
        fallback_data = await self.fetch_json_with_retry(
            fallback_url,
            {"method": "GET", "params": fallback_params, "headers": self.create_headers(cookie)}
        )
        fallback_songs = fallback_data.get("result", {}).get("songs", [])
        
        # 合并去重
        songs_by_id = {}
        for song in primary_songs + fallback_songs:
            if song.get("id") and song["id"] not in songs_by_id:
                songs_by_id[song["id"]] = song
        
        debug_info = {
            "primaryCode": primary_data.get("code"),
            "primaryCount": len(primary_songs),
            "fallbackCode": fallback_data.get("code"),
            "fallbackCount": len(fallback_songs),
        }
        
        return {'songs': list(songs_by_id.values()), 'debug_info': debug_info}

    async def fetch_anonymous_netease_search_songs(self, keywords: str, result_limit: int) -> List[dict]:
        """
        匿名搜索歌曲 (无需 Cookie)
        对应原 JS 脚本中的 fetchAnonymousNeteaseSearchSongs 函数
        """
        # 限制请求数量
        limit = min(result_limit * 3, 60)
        
        body = {
            "s": keywords,
            "type": "1",
            "offset": "0",
            "total": "true",
            "limit": str(limit),
        }
        
        # 使用默认的请求头（不带 Cookie）
        headers = self.create_headers("") 
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        
        data = await self.fetch_json_with_retry(
            "https://music.163.com/api/search/get/web",
            {
                "method": "POST",
                "data": body,
                "headers": headers
            }
        )
        
        return data.get("result", {}).get("songs", [])

    async def filter_playable_songs(self, raw_songs: List[dict], result_limit: int, cookie: str) -> List[dict]:
        """并发检查歌曲是否可播放 (模拟 JS 的 batch 逻辑)"""
        playable_songs = []
        
        # 使用 Semaphore 限制并发协程数，防止被封
        async def _check_song(song):
            async with self._semaphore:
                url = await self.get_netease_playable_url(str(song["id"]), cookie)
                return song if url else {}

        # 分批处理
        for i in range(0, len(raw_songs), 8):
            if len(playable_songs) >= result_limit:
                break

            batch = raw_songs[i:i+8]
            tasks = [_check_song(song) for song in batch]
            results = await asyncio.gather(*tasks)
            
            for result in results:
                if result and len(playable_songs) < result_limit:
                    playable_songs.append(await self.map_netease_song(result))
        
        return playable_songs

    # ==================== 业务 API 封装 ====================
    
    async def get_daily_recommend_songs(self, cookie: str, result_limit: int = 20) -> dict:
        """获取每日推荐"""
        normalized_cookie = self.normalize_cookie(cookie)
        if not normalized_cookie:
            return {"valid": False, "songs": []}
            
        # 验证 Cookie 有效性
        account = await self.get_netease_account(normalized_cookie)
        if not account["valid"]:
            return {"valid": False, "songs": []}

        data = await self.fetch_json_with_retry(
            "https://music.163.com/api/v3/discovery/recommend/songs",
            {"method": "GET", "headers": self.create_headers(normalized_cookie)}
        )
        
        raw_songs = data.get("data", {}).get("dailySongs", []) or data.get("recommend", [])
        songs = await self.filter_playable_songs(raw_songs, result_limit, normalized_cookie)
        
        return {
            "valid": bool(data.get("data", {}).get("dailySongs") or data.get("recommend")),
            "songs": songs
        }

    async def get_user_playlists(self, cookie: str) -> dict:
        """获取用户歌单列表"""
        account = await self.get_netease_account(cookie)
        if not account["valid"] or not account["userId"]:
            return {"valid": False, "playlists": []}

        params = {
            "uid": account["userId"],
            "limit": 100,
            "offset": 0
        }
        
        data = await self.fetch_json_with_retry(
            "https://music.163.com/api/user/playlist",
            {"method": "GET", "params": params, "headers": self.create_headers(cookie)}
        )
        
        playlists = [
            {
                "id": p["id"],
                "name": p["name"],
                "trackCount": p.get("trackCount", 0)
            } 
            for p in data.get("playlist", [])
        ]
        
        return {"valid": True, "playlists": playlists}

    async def get_playlist_detail(self, playlist_id: str, cookie: str, result_limit: int = 50) -> List[dict]:
        """获取歌单详情"""
        params = {
            "id": playlist_id,
            "n": result_limit * 2
        }
        
        data = await self.fetch_json_with_retry(
            "https://music.163.com/api/v6/playlist/detail",
            {"method": "GET", "params": params, "headers": self.create_headers(cookie)}
        )
        
        tracks = data.get("playlist", {}).get("tracks", [])
        return await self.filter_playable_songs(tracks, result_limit, cookie)

    async def get_netease_account(self, cookie: str) -> dict:
        """获取账户信息"""
        normalized_cookie = self.normalize_cookie(cookie)
        if not normalized_cookie:
            return {"valid": False, "userId": None, "nickname": ""}
            
        data = await self.fetch_json_with_retry(
            "https://music.163.com/api/nuser/account/get",
            {"method": "GET", "headers": self.create_headers(normalized_cookie)}
        )
        
        user_id = data.get("profile", {}).get("userId") or data.get("account", {}).get("id")
        return {
            "valid": bool(user_id),
            "userId": user_id,
            "nickname": data.get("profile", {}).get("nickname", "")
        }




    # async def create_qr_key(self) -> dict:
    #     """步骤1：获取二维码唯一标识 (unikey)"""
    #     url = "https://music.163.com/api/login/qrcode/unikey"
    #     headers = self.create_headers("")
    #     try:
    #         async with httpx.AsyncClient() as client:
    #             resp = await client.post(url, data={"type": 1}, headers=headers)
    #             data = resp.json()
    #             if data.get("code") == 200:
    #                 return {"code": 200, "key": data.get("unikey")}
    #             return {"code": 500, "msg": "获取二维码Key失败"}
    #     except Exception as e:
    #         return {"code": 500, "msg": str(e)}

    # async def create_qr_img(self, key: str) -> dict:
    #     """步骤2：根据 Key 生成二维码图片 (Base64)"""
    #     try:
    #         import qrcode
    #         import io
    #         import base64
            
    #         qr_url = f"https://music.163.com/login?codekey={key}"
    #         qr = qrcode.QRCode(version=1, box_size=10, border=4)
    #         qr.add_data(qr_url)
    #         qr.make(fit=True)
    #         img = qr.make_image(fill_color="black", back_color="white")
            
    #         buffer = io.BytesIO()
    #         img.save(buffer, format="PNG")
    #         b64_img = base64.b64encode(buffer.getvalue()).decode("utf-8")
    #         return {"code": 200, "qrimg": f"data:image/png;base64,{b64_img}"}
    #     except ImportError:
    #         return {"code": 500, "msg": "Missing qrcode library. Run: pip install qrcode[pil]"}
    #     except Exception as e:
    #         return {"code": 500, "msg": str(e)}

    # async def check_qr_status(self, key: str) -> dict:
    #     """
    #     步骤3：轮询扫码状态
    #     状态码说明：800 过期, 801 等待扫码, 802 待确认, 803 授权成功
    #     """
    #     import time
    #     timestamp = int(time.time() * 1000)
        
    #     url = "https://music.163.com/api/login/check"
    #     headers = {
    #         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    #         "Referer": "https://music.163.com/",
    #         "Host": "music.163.com",
    #         "Accept": "*/*",
    #         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    #         "Connection": "keep-alive",
    #         "Content-Type": "application/x-www-form-urlencoded"
    #     }
        
    #     form_data = {
    #         "key": key,
    #         "type": 1,
    #         "csrf_token": "",
    #         "timestamp": timestamp
    #     }

    #     async with httpx.AsyncClient() as client:
    #         resp = await client.post(url, data=form_data, headers=headers)
    #         print(resp.text)
    #         # ✅ 核心修复：防御性 JSON 解析
    #         # 1. 检查响应状态码是否为成功
    #         if resp.status_code != 200:
    #             print(f"❌ [QR Check] HTTP Error: {resp.status_code}, Body: {resp.text[:200]}")
    #             return {"code": 500, "msg": f"网易云接口返回异常: {resp.status_code}"}
            
    #         # 2. 尝试解析 JSON，捕获解析失败的情况
    #         try:
    #             data = resp.json()
    #         except Exception as json_err:
    #             print(f"❌ [QR Check] JSON Parse Error: {json_err}")
    #             print(f"❌ [QR Check] Raw Response: {resp.text}")
    #             return {"code": 500, "msg": "网易云返回了非JSON数据"}
            
    #         code = data.get("code", 800)
            
    #         # 如果授权成功 (803)，提取响应头中的 cookie
    #         if code == 803:
    #             set_cookie = resp.headers.get("set-cookie", "")
    #             return {"code": 803, "cookie": set_cookie, "msg": "授权登录成功"}
            
    #         return {"code": code, "msg": data.get("message", "")}

    # async def get_qr_base64(self) -> dict:
    #     """合并步骤：获取 Key 并直接生成 Base64 二维码"""
    #     # 1. 获取 unikey
    #     url = "https://music.163.com/api/login/qrcode/unikey"
    #     headers = self.create_headers("")
    #     async with httpx.AsyncClient() as client:
    #         resp = await client.post(url, data={"type": 1}, headers=headers)
    #         data = resp.json()
    #         if data.get("code") != 200:
    #             return {"code": 500, "msg": "获取二维码Key失败"}
    #         key = data.get("unikey")
    #         print(key)
    #         print(key)
    #         print(key)
    #         print(key)
            
    #         # 2. 生成二维码 Base64
    #         import qrcode, io, base64
    #         qr_url = f"https://music.163.com/login?codekey={key}"
    #         qr = qrcode.QRCode(version=1, box_size=10, border=4)
    #         qr.add_data(qr_url)
    #         qr.make(fit=True)
    #         img = qr.make_image(fill_color="black", back_color="white")
            
    #         buffer = io.BytesIO()
    #         img.save(buffer, format="PNG")
    #         b64_img = base64.b64encode(buffer.getvalue()).decode("utf-8")
            
    #         return {"code": 200, "key": key, "qrimg": f"data:image/png;base64,{b64_img}"}

