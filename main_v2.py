# -*- coding: utf-8 -*-
# main.py
import os
import json
import asyncio
import traceback
from pathlib import Path
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Header, Query, Body, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ==================== 配置 ====================
BASE_DIR = Path(__file__).parent
from services_v2 import *

DIST_DIR = BASE_DIR / "dist"  # 前端文件目录

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== 配置与常量 ====================
NETEASE_HEADERS = {
    "Referer": "https://music.163.com/",
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Connection": "close",
}


# ==================== 全局异常处理中间件 ====================
@app.middleware("http")
async def global_exception_handler(request: Request, call_next):
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        # 1. 仅在控制台打印详细堆栈（方便后端排查）
        print(f"\n❌ [Global Exception] {type(e).__name__}: {e}")
        print(traceback.format_exc())
        
        # 2. 向前端只返回安全的错误信息
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal Server Error",
                "detail": str(e)  # 仅返回异常信息，不再包含 traceback
            }
        )

# 初始化核心服务
music_service = NeteaseMusicService()

# ==================== 工具函数 ====================
def normalize_query_value(value: str) -> str:
    """安全地处理查询参数"""
    return (value or '').strip()

# ==================== API 路由 ====================

@app.get("/api/playlists")
async def get_playlists():
    """获取本地存储的播放列表 (Favorites, Visual Set)"""
    
    # 这里假设 readPlaylistsFile 是 services.py 中的方法或在此处实现
    # 由于上一轮未完整生成文件读写，此处简化处理
    if os.path.exists(PLAYLISTS_PATH):
        with open(PLAYLISTS_PATH, 'r', encoding='utf-8') as f:
            playlists = json.load(f)
    else:
        playlists = []
    return {"playlists": playlists}

@app.put("/api/playlists")
async def update_playlists(data: Dict):
    """保存本地播放列表"""
    playlists = data.get("playlists", [])
    # 模拟 writePlaylistsFile 逻辑
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PLAYLISTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(playlists, f, indent=2, ensure_ascii=False)
    return {"playlists": playlists}

@app.get("/api/netease/cookie")
async def check_cookie():
    """检查当前内存中的 Cookie 状态"""
    account = await music_service.get_netease_account(music_service.browser_cookie)
    return {
        "hasCookie": bool(music_service.browser_cookie),
        "valid": account["valid"],
        "userId": account["userId"],
        "nickname": account["nickname"],
    }

@app.put("/api/netease/cookie")
async def update_cookie(data: Dict):
    """更新全局 Cookie 并清空相关缓存"""

    new_cookie = data.get("cookie", "")
    music_service.set_cookie(new_cookie)

    # 清空缓存
    playable_url_cache._store.clear()
    search_cache._store.clear()

    account = await music_service.get_netease_account(music_service.browser_cookie)
    return {
        "hasCookie": bool(music_service.browser_cookie),
        "valid": account["valid"],
        "userId": account["userId"],
        "nickname": account["nickname"],
    }

@app.get("/api/netease/search")
async def search_songs(
    request: Request,
    keywords: str = Query(..., alias="keywords"),
    limit: int = Query(30, ge=1, le=100),
    debug: str = Query(None, alias="debug")
):
    """搜索歌曲"""
    # 处理 Headers 中的 Cookie
    header_cookie = request.headers.get(NETEASE_COOKIE_HEADER, "")
    cookie = header_cookie or music_service.browser_cookie

    # 限制结果数量 (有登录态限制少，无登录态限制多)
    result_limit = min(limit, 40) if cookie else min(limit, 20)

    include_debug = debug == "1"

    # 缓存 Key 生成逻辑
    search_mode = f"cookie::{cookie}" if cookie else "anonymous-baseline"
    cache_key = f"{keywords.lower()}::{result_limit}::{search_mode}"
    
    # 尝试读取缓存
    cached = search_cache.get(cache_key)
    if cached:
        payload = cached["payload"]
        payload["cached"] = True
        return payload
    search_result = await music_service.fetch_netease_search_songs(keywords, result_limit)
    raw_songs = [await music_service.map_netease_song(song) for song in search_result["songs"]]
    songs = await music_service.filter_playable_songs(raw_songs, result_limit)

    payload = {
        "songs": songs,
        "rawCount": len(raw_songs),
        "filteredCount": len(songs)
    }
    
    if include_debug:
        payload["debug"] = search_result.get("debug", {})

    # 写入缓存
    search_cache.set(cache_key, {
        "payload": payload,
        "expiresAt": asyncio.get_event_loop().time() + (5 * 60) # 5分钟
    })

    return payload


@app.get("/api/netease/liked")
async def get_liked_songs(request: Request, limit: int = Query(50, ge=1, le=100)):
    """获取用户喜欢的音乐 (通常为第一个歌单)"""

    user_playlists = await music_service.get_user_playlists()
    if not user_playlists["valid"]:
        raise HTTPException(status_code=401, detail="Netease cookie is invalid or expired")
        
    if not user_playlists["playlists"]:
        return {"songs": [], "playlist": {}}
        
    liked_playlist = user_playlists["playlists"][0]
    songs = await music_service.get_playlist_playable_songs(str(liked_playlist["id"]), cookie, limit)
    
    return {"songs": songs, "playlist": liked_playlist}
        

@app.get("/api/netease/playlists")
async def get_user_playlists_api(request: Request):
    """获取用户歌单列表 (排除第一个，通常是喜欢的音乐)"""
    user_playlists = await music_service.get_user_playlists()
    if not user_playlists["valid"]:
        raise HTTPException(status_code=401, detail="Netease cookie is invalid or expired")
        
    # 排除第一个歌单 (通常是喜欢的音乐，由 /liked 接口处理)
    return {"playlists": user_playlists["playlists"][1:]}

@app.get("/api/netease/playlist")
async def get_playlist_detail(request: Request, id: str = Query(...), limit: int = Query(50, ge=1, le=100)):
    """获取指定 ID 歌单的歌曲详情"""
    if not id:
        raise HTTPException(status_code=400, detail="Missing id")
    account = await music_service.get_netease_account()
    if not account["valid"]:
        raise HTTPException(status_code=401, detail="Netease cookie is invalid or expired")
        
    songs = await music_service.get_playlist_detail(id, limit)
    return {"songs": songs}


@app.get("/api/netease/daily-recommend")
async def get_daily_recommend(request: Request, limit: int = Query(30, ge=1, le=50)):
    """获取每日推荐歌曲"""
    
    result = await music_service.get_daily_recommend_songs(limit)
    if not result["valid"]:
        raise HTTPException(status_code=401, detail="Netease cookie is invalid or expired")
        
    return {"songs": result["songs"]}

@app.get("/api/netease/lyric")
async def get_lyric(request: Request, id: str = Query(...)):
    """获取歌词"""
    if not id:
        raise HTTPException(status_code=400, detail="Missing id")
    return await music_service.get_lyric(id)
 
@app.get("/api/netease/url")
async def get_playable_url(request: Request, id: str = Query(...)):
    """获取单曲的播放地址"""
    if not id:
        raise HTTPException(status_code=400, detail="Missing id")
    url = await music_service.get_netease_playable_url(id)
    return {"url": url}

@app.get("/api/netease/audio")
async def proxy_audio(request: Request, id: str = Query(...)):
    """
    音频代理接口
    这是一个流式接口，用于解决跨域播放 MP3 的问题
    """
    if not id:
        raise HTTPException(status_code=400, detail="Missing id")
    
    header_cookie = request.headers.get(NETEASE_COOKIE_HEADER, "")
    cookie = header_cookie or music_service.browser_cookie
    
    playable_url = await music_service.get_netease_playable_url(id)
    if not playable_url:
        raise HTTPException(status_code=404, detail="No playable url for this song")
        
    # 转发音频流
    headers = NETEASE_HEADERS.copy()
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
        
    async def audio_streamer():
        async with httpx.AsyncClient() as client:
            # 流式请求
            stream_response = await client.get(playable_url, headers=headers, )
            
            # 转发状态码
            if stream_response.status_code != 200:
                # 如果是流式响应，FastAPI 需要特殊处理非 200，这里简化直接抛出
                # 实际生产中可能需要更复杂的逻辑来处理 206 Partial Content
                pass
                
            async for chunk in stream_response.aiter_bytes():
                yield chunk

    # 尝试获取内容类型
    # 注意：这里简化处理，实际可能需要先发一个 HEAD 请求或解析 URL 后缀
    content_type = "audio/mpeg" 
    
    return StreamingResponse(audio_streamer(), media_type=content_type, headers=dict(headers))

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
    uvicorn.run(app, host="0.0.0.0", port=4173)
