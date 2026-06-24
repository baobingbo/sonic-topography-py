import asyncio
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode

import httpx
from fastapi.responses import StreamingResponse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ==================== 配置 ====================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PLAYLISTS_PATH = DATA_DIR / "playlists.json"
DIST_DIR = BASE_DIR / "dist"  # 前端文件目录

# 确保数据目录存在
DATA_DIR.mkdir(exist_ok=True)

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局变量存储 Cookie
browser_netease_cookie = ""
playable_url_cache: Dict[str, Dict] = {}
search_cache: Dict[str, Dict] = {}

# 缓存 TTL
PLAYABLE_URL_CACHE_TTL = 10 * 60  # 10分钟
SEARCH_CACHE_TTL = 5 * 60  # 5分钟

# 网易云请求头
NETEASE_HEADERS = {
    "Referer": "https://music.163.com/",
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Connection": "close",
}

# ==================== 工具函数 ====================
def normalize_netease_cookie(value: str) -> str:
    if not value: return ""
    lines = [line.strip().rstrip(";") for line in value.split("\n") if line.strip()]
    return "; ".join(lines)

def read_netease_cookie(req: Request) -> str:
    header_cookie = req.headers.get("x-netease-cookie", "")
    raw_cookie = header_cookie or browser_netease_cookie
    return normalize_netease_cookie(raw_cookie)

def extract_uid_from_cookie(cookie_str: str) -> str:
    if not cookie_str: return ""
    for part in cookie_str.split(';'):
        kv = part.strip().split('=', 1)
        if len(kv) == 2 and kv[0].strip() == '__uid':
            return kv[1].strip()
    return ""

async def fetch_json_with_retry(url: str, headers: dict, retries: int = 2) -> dict:
    async with httpx.AsyncClient() as client:
        for attempt in range(retries + 1):
            try:
                response = await client.get(url, headers=headers, timeout=10.0)
                data = response.json()
                if response.status_code == 200 and data.get("code") != 400:
                    return data
            except Exception as e:
                print(f"Request failed: {e}")
            if attempt < retries:
                await asyncio.sleep(0.180 * (attempt + 1))
        return {}

async def get_netease_account(cookie: str):
    if not cookie: return {"valid": False, "userId": None, "nickname": ""}
    url = "https://music.163.com/api/nuser/account/get"
    headers = {
        "Referer": "https://music.163.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            data = response.json()
            user_id = data.get("profile", {}).get("userId") or data.get("account", {}).get("id")
            nickname = data.get("profile", {}).get("nickname", "")
            return {
                "valid": bool(user_id),
                "userId": user_id,
                "nickname": nickname
            }
    except Exception as e:
        print(f"Error fetching Netease account: {e}")
        return {"valid": False, "userId": None, "nickname": ""}

# ==========================================
# 1. 所有的 API 路由定义 (放在最前面)
# ==========================================

# --------------------
# 本地歌单 CRUD
# --------------------
def create_default_playlists():
    return [
        {"id": "favorites", "name": "Favorites", "songs": []},
        {"id": "visual-set", "name": "Visual Set", "songs": []},
    ]

@app.get("/api/playlists")
async def get_playlists():
    if PLAYLISTS_PATH.exists():
        with open(PLAYLISTS_PATH, 'r', encoding='utf-8') as f:
            try:
                data = json.load(f)
                return {"playlists": data}
            except:
                pass
    return {"playlists": create_default_playlists()}

@app.put("/api/playlists")
async def save_playlists(request: Request):
    data = await request.json()
    playlists = data.get("playlists", [])
    normalized = []
    for pl in playlists:
        normalized.append({
            "id": str(pl.get("id", f"playlist-{asyncio.get_event_loop().time()}")),
            "name": str(pl.get("name", "Playlist")),
            "songs": pl.get("songs", [])
        })
    with open(PLAYLISTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(normalized, f, indent=2, ensure_ascii=False)
    return {"playlists": normalized}

# --------------------
# 音频流代理 (核心修复)
# --------------------
@app.get("/api/netease/audio")
async def proxy_netease_audio(id: str, request: Request):
    """
    对应 Node.js 原始逻辑: /api/netease/audio?id=xxx
    """
    if not id:
        raise HTTPException(status_code=400, detail="Missing id")
    
    # 1. 获取 Cookie
    cookie = read_netease_cookie(request)
    
    # 2. 根据 ID 获取播放地址 (复用缓存逻辑)
    cache_key = f"{id}::{cookie}"
    
    # 检查缓存
    if cache_key in playable_url_cache:
        cached = playable_url_cache[cache_key]
        if cached["expires_at"] > asyncio.get_event_loop().time():
            playable_url = cached["url"]
        else:
            del playable_url_cache[cache_key]
            playable_url = None
    else:
        playable_url = None

    # 如果缓存没有，请求网易云
    if not playable_url:
        url = f"https://music.163.com/api/song/enhance/player/url?id={id}&ids=[{id}]&br=320000"
        headers = {**NETEASE_HEADERS, **({"Cookie": cookie} if cookie else {})}
        data = await fetch_json_with_retry(url, headers)
        playable_url = data.get("data", [{}])[0].get("url")
        
        # 更新缓存
        if playable_url:
            playable_url_cache[cache_key] = { 
                "url": playable_url, 
                "expires_at": asyncio.get_event_loop().time() + PLAYABLE_URL_CACHE_TTL 
            }

    if not playable_url:
        raise HTTPException(status_code=404, detail="No playable url for this song")

    # 3. 代理流 (支持 Range 请求)
    headers = {**NETEASE_HEADERS}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(playable_url, headers=headers, timeout=30.0)
            
            # 处理 200 或 206
            if response.status_code not in (200, 206):
                response.raise_for_status()

            content_type = response.headers.get("Content-Type", "audio/mpeg")
            proxy_headers = {
                "Cache-Control": "public, max-age=86400",
                "Accept-Ranges": "bytes",
                "Content-Disposition": "inline",
            }
            if "content-range" in response.headers:
                proxy_headers["Content-Range"] = response.headers["content-range"]

            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                media_type=content_type,
                headers=proxy_headers
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Audio proxy failed: {str(e)}")

# --------------------
# 其他网易云 API 代理
# --------------------
@app.get("/api/netease/url")
async def get_netease_url(id: str, request: Request):
    cookie = read_netease_cookie(request)
    cache_key = f"{id}::{cookie}"
    
    if cache_key in playable_url_cache:
        cached = playable_url_cache[cache_key]
        if cached["expires_at"] > asyncio.get_event_loop().time():
            return {"url": cached["url"]}

    url = f"https://music.163.com/api/song/enhance/player/url?id={id}&ids=[{id}]&br=320000"
    headers = {**NETEASE_HEADERS, **({"Cookie": cookie} if cookie else {})}
    data = await fetch_json_with_retry(url, headers)
    playable_url = data.get("data", [{}])[0].get("url")

    if playable_url:
        playable_url_cache[cache_key] = {
            "url": playable_url,
            "expires_at": asyncio.get_event_loop().time() + PLAYABLE_URL_CACHE_TTL
        }
    return {"url": playable_url}

@app.get("/api/netease/lyric")
async def get_netease_lyric(id: str, request: Request):
    cookie = read_netease_cookie(request)
    url = f"https://music.163.com/api/song/lyric?id={id}&lv=-1&kv=-1&tv=-1"
    headers = {**NETEASE_HEADERS, **({"Cookie": cookie} if cookie else {})}
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, timeout=10.0)
        data = response.json()
    return {
        "lyric": data.get("lrc", {}).get("lyric", ""),
        "translatedLyric": data.get("tlyric", {}).get("lyric", "")
    }

@app.get("/api/netease/search")
async def search_netease(keywords: str, limit: int = 50, request: Request = None):
    if not keywords:
        raise HTTPException(status_code=400, detail="Missing keywords")
    cookie = read_netease_cookie(request)
    has_cookie = bool(cookie)
    result_limit = min(max(1, limit), 40 if has_cookie else 20)
    
    search_url = "https://music.163.com/api/search/get/web"
    body_data = {
        "s": keywords,
        "type": "1",
        "offset": "0",
        "total": "true",
        "limit": str(min(result_limit * 5, 80))
    }
    headers = {
        **NETEASE_HEADERS,
        **({"Cookie": cookie} if cookie else {}),
        "Content-Type": "application/x-www-form-urlencoded"
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(search_url, data=body_data, headers=headers, timeout=10.0)
        data = response.json()
    
    songs_data = data.get("result", {}).get("songs", [])
    songs = []
    for song in songs_data:
        artists = [ar["name"] for ar in song.get("artists", [])]
        songs.append({
            "id": song["id"],
            "name": song["name"],
            "artist": " / ".join(artists),
            "album": song.get("album", {}).get("name", ""),
            "duration": song.get("duration", 0)
        })
    return {"songs": songs}

@app.get("/api/netease/liked")
async def get_liked_songs(request: Request):
    cookie = read_netease_cookie(request)
    uid = extract_uid_from_cookie(cookie)
    if not uid:
        raise HTTPException(status_code=400, detail="Cannot find UID in Cookie")
    url = f"https://music.163.com/api/user/playlist?uid={uid}&limit=100&offset=0"
    headers = {**NETEASE_HEADERS, "Cookie": cookie}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=10.0)
        return resp.json()

@app.get("/api/netease/playlists")
async def get_user_playlists(request: Request):
    cookie = read_netease_cookie(request)
    uid = extract_uid_from_cookie(cookie)
    if not uid:
        raise HTTPException(status_code=400, detail="Cannot find UID in Cookie")
    url = f"https://music.163.com/api/user/playlist?uid={uid}&limit=50&offset=0"
    headers = {**NETEASE_HEADERS, **({"Cookie": cookie} if cookie else {})}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=10.0)
        return resp.json()

@app.get("/api/netease/playlist")
async def get_playlist_detail(id: str, request: Request):
    cookie = read_netease_cookie(request)
    url = f"https://music.163.com/api/v6/playlist/detail?id={id}&n=1000"
    headers = {**NETEASE_HEADERS, **({"Cookie": cookie} if cookie else {})}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=10.0)
        return resp.json()

@app.get("/api/netease/daily-recommend")
async def get_daily_recommend(request: Request):
    cookie = read_netease_cookie(request)
    if not cookie:
        raise HTTPException(status_code=401, detail="Missing Netease Cookie")
    url = "https://music.163.com/api/v3/discovery/recommend/songs"
    headers = {**NETEASE_HEADERS, "Cookie": cookie}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=10.0)
        return resp.json()

# --------------------
# Cookie 管理
# --------------------
@app.put("/api/netease/cookie")
async def set_netease_cookie(request: Request):
    global browser_netease_cookie
    try:
        body = await request.json()
        cookie_value = body.get("cookie", "")
        normalized_cookie = normalize_netease_cookie(cookie_value)
        browser_netease_cookie = normalized_cookie
        account = await get_netease_account(normalized_cookie)
        return {
            "hasCookie": bool(normalized_cookie),
            "valid": account["valid"],
            "userId": account["userId"],
            "nickname": account["nickname"]
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/netease/cookie")
async def get_netease_cookie():
    global browser_netease_cookie
    account = await get_netease_account(browser_netease_cookie)
    return {
        "hasCookie": bool(browser_netease_cookie),
        "valid": account["valid"],
        "userId": account["userId"],
        "nickname": account["nickname"]
    }

# ==========================================
# 2. 静态文件挂载 (放在 API 路由之后)
# ==========================================
app.mount("/static", StaticFiles(directory=DIST_DIR), name="static")

# ==========================================
# 3. 通配符兜底路由 (必须放在最后)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def read_root():
    return FileResponse(DIST_DIR / "index.html")

@app.get("/{full_path:path}")
async def serve_static(full_path: str):
    file_path = DIST_DIR / full_path
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)
    else:
        return FileResponse(DIST_DIR / "index.html")

# ==================== 启动 ====================
if __name__ == "__main__":
    import uvicorn
    PORT = int(os.getenv("PORT", 4173))
    print(f"Sonic Topography is running at http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, reload=False)