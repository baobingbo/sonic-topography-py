# -*- coding: utf-8 -*-
# services.py
import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional
import httpx
from fastapi import HTTPException

# 引入已有的 SDK
from MusicLibrary.neteaseCloudMusicApi import NeteaseCloudMusicApi, NcmProcessEnv, Response


# ==================== 配置 ====================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PLAYLISTS_PATH = DATA_DIR / "playlists.json"
DIST_DIR = BASE_DIR / "dist"

# 确保数据目录存在
DATA_DIR.mkdir(exist_ok=True)





# ==================== 核心服务逻辑 ====================
class NeteaseMusicService:
    def __init__(self):
        # 初始化 SDK 实例
        self.browser_cookie = {}
        
        # 限制并发数，模拟 JS 的 batchSize
        self._semaphore = asyncio.Semaphore(8) 
        
        # 简易缓存 (保持原有逻辑)
        self.playable_url_cache = {}
        self.search_cache = {}

    @property
    def api(self):
        music_service = NeteaseCloudMusicApi(NcmProcessEnv())
        music_service.set_cookie(self.browser_cookie) if self.browser_cookie else None
        return music_service

    def set_cookie(self, value: str):
        """设置 Cookie 字串"""
        
        def parse_cookies(cookieStr):
            cookie = {}
            for item in cookieStr.split("; "):
                # 按照第一个等号分割，获取键值对
                k, v = item.split("=", 1)
                cookie[k.strip()] = v.strip()
            return cookie
        cookie = self.normalize_cookie(value)
        self.browser_cookie = parse_cookies(cookie)

    def normalize_cookie(self, value: str) -> str:
        """清洗 Cookie 字串"""
        if not value:
            return ""
        lines = [line.strip().rstrip(";") for line in value.split("\n") if line.strip()]
        return "; ".join(lines)

    def _handle_response(self, resp: Response) -> Dict:
        """
        处理 SDK 返回的 Response 对象。
        注意：SDK 可能不会设置 status="success"，或者错误信息在 body 的 code 中。
        """
        if not resp:
            raise HTTPException(status_code=500, detail="API Response is None")

        # 尝试解析 body
        try:
            if isinstance(resp.body, str):
                data = json.loads(resp.body)
            else:
                data = resp.body
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Parse Error: {e}")

        # 检查网易云音乐 API 的业务状态码 (通常是 code: 200 表示成功)
        # 如果 data 是字典，检查 code；如果不是，说明结构异常，直接返回
        if isinstance(data, dict):
            code = data.get('code')
            # 常见的成功状态码：200, 201, 301(重定向通常也算成功获取数据)
            # 如果 code 不存在，也认为是成功的（兼容没有 code 字段的接口，如 /song/url）
            if code is not None and code != 200:
                # 如果有具体的错误信息，尝试提取
                message = data.get('message', data.get('msg', 'Unknown API Error'))
                raise HTTPException(status_code=500, detail=f"Netease API Error [{code}]: {message}")
        else:
            # 如果 data 不是字典，说明结构异常，但数据存在，可以尝试返回
            pass

        return data
    
    # --- 以下是适配原有 main_v1.py 接口的方法 ---
    # 重构完成
    async def get_netease_playable_url(self, song_id: str) -> Optional[str]:
        """获取歌曲播放地址"""
        cache_key = f"{song_id}::{self.browser_cookie}"
        if cache_key in self.playable_url_cache:
            return self.playable_url_cache[cache_key]

        # 使用 SDK 调用 /song/url 接口
        async with self._semaphore:
            result = self.api.song_url(id=song_id, br="320000")
            data = self._handle_response(result)
            
            playable_url = None
            if data.get("data") and len(data["data"]) > 0:
                playable_url = data["data"][0].get("url")
                
            if playable_url:
                self.playable_url_cache[cache_key] = playable_url
            return playable_url

    async def get_lyric(self, song_id: str) -> Dict:
        """获取歌词信息"""
        # 使用 SDK 调用 lyric 接口
        result = self.api.lyric(id=song_id)
        data = self._handle_response(result)
        
        # 提取原始歌词和翻译歌词
        lrc = data.get("lrc", {}).get("lyric", "")
        tlyric = data.get("tlyric", {}).get("lyric", "")
        
        return {
            "lyric": lrc,
            "translatedLyric": tlyric
        }


    # 重构完成
    async def fetch_netease_search_songs(self, keywords: str, limit: int) -> Dict:
        """搜索歌曲，使用 SDK 的 search 接口"""
        # 使用 SDK 的 search 方法
        result = self.api.search(keywords=keywords, limit=limit, type=1)
        data = self._handle_response(result)
        
        songs = data.get("result", {}).get("songs", [])
        return {'songs': songs, 'debug_info': {'primaryCount': len(songs)}}

    # 重构完成
    async def get_daily_recommend_songs(self, limit: int = 20) -> Dict:
        """获取每日推荐"""
        if not self.browser_cookie:
            return {"valid": False, "songs": []}

        # 验证 Cookie 有效性
        account = await self.get_netease_account()
        if not account["valid"]:
            return {"valid": False, "songs": []}

        # 调用 SDK 接口
        result = self.api.recommend_songs()
        data = self._handle_response(result)
        
        raw_songs = data.get("data", []).get("dailySongs", []) or data.get("recommend", [])
        songs = await self.filter_playable_songs(raw_songs, limit)
        
        return {
            "valid": True,
            "songs": songs
        }

    # 重构完成
    async def get_user_playlists(self) -> Dict:
        """获取用户歌单列表"""
        account = await self.get_netease_account()
        if not account["valid"] or not account["userId"]:
            return {"valid": False, "playlists": []}

        result = self.api.user_playlist(uid=account["userId"])
        data = self._handle_response(result)
        
        playlists = [
            {
                "id": p["id"],
                "name": p["name"],
                "trackCount": p.get("trackCount", 0)
            }
            for p in data.get("playlist", [])
        ]
        return {"valid": True, "playlists": playlists}

    # 重构完成
    async def get_playlist_detail(self, playlist_id: str, limit: int = 20) -> List[dict]:
        """获取歌单详情"""
        # 先获取歌单 tracks
        result = self.api.playlist_track_all(id=playlist_id, limit=limit*2)
        data = self._handle_response(result)
        
        tracks = data.get("songs", [])
        return await self.filter_playable_songs(tracks, limit)

    # 重构完成
    async def get_netease_account(self) -> Dict:
        """获取账户信息"""
        if not self.browser_cookie:
            return {"valid": False, "userId": None, "nickname": ""}
            
        result = self.api.user_account()
        data = self._handle_response(result)
        
        profile = data.get("profile", {})
        user_id = profile.get("userId")
        
        return {
            "valid": bool(user_id),
            "userId": user_id,
            "nickname": profile.get("nickname", "")
        }

    # --- 辅助方法 ---
    async def map_netease_song(self, song: dict) -> dict:
        """将网易云原始歌曲数据映射为标准化格式"""
        artists = song.get("artists") or song.get("ar") or []
        if not isinstance(artists, list):
            artists = []
        artist_name = " / ".join([artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")])

        album = song.get("album") or song.get("al") or {}
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

    async def check_music(self, song_id: str) -> Optional[str]:
        result = self.api.check_music(song_id)
        data = self._handle_response(result)
        return data.get("success", False)

    async def filter_playable_songs(self, raw_songs: List[dict], limit: int = 20) -> List[dict]:
        """并发检查歌曲是否可播放"""
        playable_songs = []
        
        async def _check_song(song):
            async with self._semaphore:
                url = await self.check_music(str(song["id"]))
                return song if url else None

        for i in range(0, len(raw_songs), 8):
            if len(playable_songs) >= limit:
                break
            batch = raw_songs[i:i+8]
            tasks = [_check_song(song) for song in batch]
            results = await asyncio.gather(*tasks)
            
            for result in results:
                if result and len(playable_songs) < limit:
                    mapped = await self.map_netease_song(result)
                    playable_songs.append(mapped)

        return playable_songs

if __name__ == "__main__":
    service = NeteaseMusicService()
    cookie_str = "_iuqxldmzr_=32; _ntes_nnid=3e768d0aef1ca30c85b34b41e90e1ede,1772175354727; _ntes_nuid=3e768d0aef1ca30c85b34b41e90e1ede; NMTID=00OAR0Iu9TbUHr2_UcEiOrb916siC8AAAGcneHpdg; WEVNSM=1.0.0; WNMCID=idxdgm.1772175358481.01.0; ntes_utid=tid._.bGRefEpasUBFVgRUAUaXtJP3rA0UFWLS._.0; sDeviceId=YD-r8JngtJgDQREExBEFQOC5Yb2%2FAwUQHbX; __snaker__id=QjU49eibElDooVTA; ntes_kaola_ad=1; WM_TID=bVYD%2FwCjrRNBVFEFRBfWsMKm6A1RfL2l; Hm_lvt_1483fb4774c02a30ffa6f0e2945e9b70=1780565583; HMACCOUNT=A3664B3499536F42; __csrf=25194e16fc03684a6a1aa7dbe6732958; playerid=61113329; gdxidpyhxdE=iHmNX7QDu2baE%5C0%2BPyGENn3czh5xc8Hx7UeKV4XkxGW%2FopOOwXgdDLpSCJ%5C0j8r9A1oaUICGVr%2BDUz%2F1nGU146HPCbHMU0X0YJEpw%5CdlH7kkw9G1Y6cAhjrTXhcA9cjt9GWfgQK1b1LcaBxY4tsMbaXstkuBk%2BUIR6YcEef0%5C%2BVqB9%2F%5C%3A1782292624523; MUSIC_U=0026246B62E6DF2DC33694FB8BEC16EE9B19BCBFBBF72012D5B3727E634586D4BB60A84ACD60DE880449DF77D06BB944E6CEE28266FDCBCA47D9D74F1F575A41DD9B8E764D200B56979E2F15AB04D074AD29C9C96AA17B6106C44270E785D5FCBC86632176C8543705C781A15C0DD041AC8F937327E560882E5059DF64C12F666E75403FE7CEABF973FFAB549A4D60586B2DB65D1D01D26BC9CB8C49FA341B2333328FBF26414837C9CD2662C820B60545D920568D7327915506690234F2EF7758455171CC906BB2F61E4E2567757391674B1A54E95333515D9DD55449EB4083DE564733AD6343FF20E7FAB216696E529663155785C58162421EA97C4CD4C829DD7ABC66B0C80306ECF82DB7F662D1D307DAFF02E68E638EC3AA1368DACF2E6B63086961D429F8867F659EE2D7EC12414E29FB787973B21E605259AC98D0933FEC909C7C5B0F58CA0FD2C8FB8111E6071B7615F58FF91770A9DFA39E31A5871E22FE4C2999EA7A3262E4E01D787C22080E0F2A13DFD37B279F2EADA58FBBD2FCAFD0DECCF50F497136D687DB84BC3D7BEFD2183B3B01A3526ED4C19F0D4A6224ED; __csrf=710afb7bafdc058efba7aedf89446eb6; Hm_lpvt_1483fb4774c02a30ffa6f0e2945e9b70=1782376548; WM_NI=HoY34IvVI%2BEAJ2ClNDoQ%2BRnl6IUFC2lkKMkvNAPIq1A4EZrD8RLTtF%2BZfG%2BhC1vt54Eaphl6D1cocTh649WghAsndb0KoDS%2FjuTpubxDRSiHfn2kagStQahff0J7NoGfbXc%3D; WM_NIKE=9ca17ae2e6ffcda170e2e6ee89f772a7ef8886c947b6b48bb3c44f969a8b82c77fb58f99afe97c8bb1f9b2dc2af0fea7c3b92a8cb197d4b743b68a87ccc84b8aee8d8dcd50ad8fe5b3ed68f6979ba7c23ab0b585aeea7e8e9fa0aaf43b83ec8bbbf150fc8e84b5f74ee986aaa8aa6481efba95b84087bfc089ef3e88efad90e260aceafcdad66dac9fbd8fe76af495a1a5d225aaab9c91f86abc9c9eb4ec44a6b2fe8ce46d91ac9f82eb6ea6f086d8cd6585baaeb9e237e2a3; JSESSIONID-WYYY=y6WOBSUTdGdgwMp%5CSMp3I0Mix0aUSI0MEFH4%2F%5CYm13SxauiEqMefw03ZArx1Bk%2FPjrCKH54Trfc1925b8u5zDqw%2FP5ghYDkRHyg9DxZu7lDGGuIB3lSNmTyWAQtIv2%5CBmgyzW6KOUhX3ejc61lbeuDFChZWHiBJDiwkblzJnIoc3YaQQ%3A1782456493814"
    service.set_cookie(cookie_str)
    
    # resp = asyncio.run(
    #     service.api.check_music("408332757")
    # )   
    
    
    # resp = asyncio.run(
    #     service.fetch_netease_search_songs(keywords="骄傲的少年", result_limit=1)
    #     )
    
    
    resp = asyncio.run(
        service.get_playlist_detail('4980073595')
        )
    
    
    # resp = asyncio.run(
    #     service.get_daily_recommend_songs()
    #     )
    
    print(resp)


