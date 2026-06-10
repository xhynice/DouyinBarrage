"""网络服务层：HTTP 请求（带重试）、WebSocket URL 构建、房间 API。

职责划分：
    - http_get_with_retry: 通用 HTTP GET，指数退避 + DNS 重试。
    - fetch_ttwid: 获取 ttwid Cookie 并验证登录态。
    - enter_room_api: 调用 /webcast/room/web/enter/ 获取房间信息。
    - build_websocket_url / build_ws_cookie: 构建 WebSocket 连接参数。
"""

__all__ = [
    'RoomNotFoundError', 'http_get_with_retry', 'build_http_headers',
    'build_websocket_url', 'build_ws_cookie', 'resolve_live_id',
    'fetch_ttwid', 'download_image', 'enter_room_api', 'fetch_webcast_cursor',
]

import json
import logging
import random
import re
import time
import urllib.parse

import requests


class RoomNotFoundError(Exception):
    """房间不存在或已注销（永久失败，不应重试）。"""
    pass

from base.utils import (
    generate_ms_token, APP_ID, LIVE_ID, VERSION_CODE,
    WEBCAST_SDK_VERSION, DID_RULE, DEVICE_PLATFORM,
)

logger = logging.getLogger(__name__)


# ── HTTP 客户端 ──────────────────────────────────

def _retry_wait(attempt):
    """计算重试等待时间（指数退避 + 随机抖动）。"""
    import random
    wait = min(2 ** attempt, 10) + random.uniform(0, 1)
    return wait


def http_get_with_retry(session, url, max_retries=3, timeout=15, **kwargs):
    """带指数退避 + 超时自增的 HTTP GET 请求。

    DNS 连接失败和超时会自动重试，其他异常直接抛出。
    每次超时后 timeout ×1.5（封顶 60s）。

    Args:
        session: requests.Session 实例。
        url: 请求 URL。
        max_retries: 最大重试次数（含首次）。
        timeout: 初始超时秒数。

    Returns:
        requests.Response 对象。

    Raises:
        requests.RequestException: 所有重试耗尽后抛出最后一次异常。
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            wait = _retry_wait(attempt)
            logger.warning(f"[网络] 连接失败（尝试 {attempt+1}/{max_retries}）: {e}，{wait:.1f}s 后重试")
            time.sleep(wait)
        except requests.exceptions.Timeout as e:
            last_exc = e
            timeout = min(timeout * 1.5, 60)
            wait = _retry_wait(attempt)
            logger.warning(f"[网络] 请求超时（尝试 {attempt+1}/{max_retries}），下次超时 {timeout:.0f}s，{wait:.1f}s 后重试")
            time.sleep(wait)
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 10))
            else:
                raise
    raise last_exc


def build_http_headers(ua, ua_version):
    """构建模拟现代 Chrome 浏览器的 HTTP 请求头。

    根据 UA 中是否包含 Chrome/Safari 自动添加 Sec-Ch-Ua 系列头。

    Args:
        ua: User-Agent 字符串。
        ua_version: 'Chrome/x.x.x.x' 格式的版本字符串。

    Returns:
        请求头字典。
    """
    headers = {
        'User-Agent': ua,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Encoding': 'gzip, deflate',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'no-cache',
        'Referer': 'https://live.douyin.com/',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
    }
    if 'Chrome' in ua and 'Safari' in ua:
        ver = ua_version.split('/')[1].split('.')[0]
        headers.update({
            'Sec-Ch-Ua': f'"Chromium";v="{ver}", "Not_A Brand";v="24", "Google Chrome";v="{ver}"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"' if 'Windows' in ua else '"MacOS"',
        })
    return headers


# ── WebSocket 客户端 ──────────────────────────────

def build_websocket_url(room_id, uid, ua_version, cursor=None, internal_ext=None):
    """构建 WebSocket 长连接 URL，包含所有查询参数。

    cursor 格式为 't-{毫秒时间戳}_r-{随机数}_d-1_u-1_h-{随机数}'，
    internal_ext 包含 room_id、did、时间戳等连接标识。

    Args:
        room_id: 直播间真实 room_id。
        uid: 用户唯一 ID（18~19 位随机数字）。
        ua_version: 'Chrome/x.x.x.x' 格式的版本字符串。
        cursor: 可选的 cursor 字符串（由 fetch_webcast_cursor 获取）。
        internal_ext: 可选的 internal_ext 字符串（由 fetch_webcast_cursor 获取）。

    Returns:
        完整的 wss:// URL 字符串。
    """
    ts = int(time.time() * 1000)
    if cursor is None:
        cursor = f't-{ts}_r-{random.randint(10**18, 10**19 - 1)}_d-1_u-1_h-{random.randint(10**18, 10**19 - 1)}'
    if internal_ext is None:
        internal_ext = (
            f'internal_src:dim|wss_push_room_id:{room_id}'
            f'|wss_push_did:{uid}'
            f'|first_req_ms:{ts}|fetch_time:{ts}'
            f'|seq:1|wss_info:0-{ts}-0-0'
            f'|wrds_v:{random.randint(10**18, 10**19 - 1)}'
        )
    return (
        f"wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/"
        f"?app_name=douyin_web"
        f"&version_code={VERSION_CODE}"
        f"&webcast_sdk_version={WEBCAST_SDK_VERSION}"
        f"&update_version_code={WEBCAST_SDK_VERSION}"
        f"&compress=gzip"
        f"&device_platform={DEVICE_PLATFORM}"
        f"&cookie_enabled=true"
        f"&screen_width=1920&screen_height=1080"
        f"&browser_language=zh-CN&browser_platform=Win32"
        f"&browser_name=Mozilla"
        f"&browser_version={urllib.parse.quote(ua_version, safe='')}"
        f"&browser_online=true&tz_name=Asia/Shanghai"
        f"&cursor={cursor}"
        f"&internal_ext={internal_ext}"
        f"&host=https://live.douyin.com"
        f"&aid={APP_ID}&live_id={LIVE_ID}&did_rule={DID_RULE}"
        f"&endpoint=live_pc&support_wrds=1"
        f"&user_unique_id={uid}"
        f"&im_path=/webcast/im/fetch/"
        f"&identity=audience"
        f"&need_persist_msg_count=15"
        f"&insert_task_id=&live_reason="
        f"&room_id={room_id}"
        f"&heartbeatDuration=0"
    )


def build_ws_cookie(ttwid, login_cookies):
    """构建 WebSocket 握手的 Cookie 字符串。

    始终包含 ttwid 和随机 msToken，登录态相关 Cookie 按白名单选取。

    Args:
        ttwid: ttwid Cookie 值。
        login_cookies: {name: value} 字典（来自 load_cookies）。

    Returns:
        'name1=value1; name2=value2' 格式的 Cookie 字符串。
    """
    cookie_parts = [
        f"ttwid={ttwid}",
        f"msToken={generate_ms_token()}",
    ]
    ws_cookie_keys = (
        'sessionid', 'sessionid_ss',
        'sid_tt', 'sid_guard',
        'uid_tt', 'uid_tt_ss',
        'passport_csrf_token',
        'odin_tt',
        'is_staff_user',
    )
    for key in ws_cookie_keys:
        value = login_cookies.get(key)
        if value:
            cookie_parts.append(f"{key}={value}")
    return "; ".join(cookie_parts)


# ── 直播间地址解析 ────────────────────────────────

_SUPPORTED_DOMAINS = (
    'live.douyin.com', 'www.douyin.com', 'douyin.com',
    'v.douyin.com',
)


def resolve_live_id(input_str, session, http_timeout=15):
    """解析各种输入格式为统一的 web_rid（直播间短 ID）。

    支持格式：
        - 纯数字 web_rid: "536863152858"
        - 完整直播间 URL: "https://live.douyin.com/536863152858"
        - 抖音号 URL: "https://v.douyin.com/iQLgksj/"
        - 用户主页 URL: "https://www.douyin.com/user/MS4wLjAB..."

    Args:
        input_str: 用户输入的直播间地址或 ID。
        session: requests.Session 实例。
        http_timeout: HTTP 请求超时秒数。

    Returns:
        解析后的 web_rid 字符串。

    Raises:
        ValueError: 无法解析时抛出。
    """
    input_str = input_str.strip()

    # 纯数字 / 非 URL 格式 → 直接作为 web_rid
    if not any(domain in input_str for domain in _SUPPORTED_DOMAINS):
        return input_str

    # 统一提取 host + path
    from urllib.parse import urlparse
    parsed = urlparse(input_str if '://' in input_str else f'https://{input_str}')
    host = parsed.netloc or parsed.hostname or ''
    path = parsed.path.rstrip('/')

    # live.douyin.com/xxx → 直接取最后一段
    if 'live.douyin.com' in host:
        web_rid = path.rsplit('/', 1)[-1] if path else ''
        if web_rid:
            logger.info(f"[解析] 从直播间 URL 提取 web_rid: {web_rid}")
            return web_rid
        raise ValueError(f"无法从 URL 中提取 web_rid: {input_str}")

    # 抖音短链 / 用户主页 → 跟随重定向后提取
    logger.info(f"[解析] 正在解析短链: {input_str}")
    ua = session.headers.get('User-Agent',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36')

    try:
        resp = session.get(input_str, timeout=http_timeout, allow_redirects=True,
                           headers={'User-Agent': ua})
        final_url = str(resp.url)
        logger.info(f"[解析] 重定向至: {final_url}")
    except Exception as e:
        raise ValueError(f"短链访问失败: {e}")

    # 从最终 URL 提取
    final_parsed = urlparse(final_url)
    final_path = final_parsed.path.rstrip('/')

    # live.douyin.com/xxx 或 douyin.com/xxx
    if '/live.douyin.com/' in final_url or final_parsed.hostname in ('live.douyin.com',):
        web_rid = final_path.rsplit('/', 1)[-1]
        if web_rid:
            logger.info(f"[解析] 提取 web_rid: {web_rid}")
            return web_rid

    # /user/xxxx 或 /webcast/reflow/xxx
    if '/user/' in final_path:
        sec_uid = final_path.rsplit('/user/', 1)[-1].split('?')[0]
        # 尝试从 reflow 参数中找 room_id
        from urllib.parse import parse_qs
        qs = parse_qs(final_parsed.query)
        room_id = qs.get('room_id', [None])[0]
        if room_id:
            logger.info(f"[解析] 从用户主页提取 room_id: {room_id}")
            return room_id
        logger.info(f"[解析] 用户主页，sec_uid={sec_uid[:20]}...")

    # 回退：取路径最后一段
    web_rid = final_path.rsplit('/', 1)[-1] if final_path else ''
    if web_rid and web_rid != 'user':
        logger.info(f"[解析] 提取 web_rid: {web_rid}")
        return web_rid

    raise ValueError(f"无法解析直播间地址: {input_str}")


# ── 房间 API ──────────────────────────────────────

def fetch_ttwid(session, live_id, login_cookies, http_timeout=15):
    """获取 ttwid Cookie 并验证登录态。

    优先访问直播间页面获取（服务端 SSR 页面最可靠），
    失败则回退到 cookie.txt 中的 ttwid。

    使用独立最小 headers 请求，确保服务端返回 SSR 版本（内嵌登录信息）。
    完整浏览器头（Sec-Ch-Ua 等）会导致返回 SPA 版本，登录信息不在 HTML 中。

    Args:
        session: 已配置 Cookie 的 requests.Session。
        live_id: 直播间 ID（web_rid）。
        login_cookies: 登录 Cookie 字典。
        http_timeout: HTTP 请求超时秒数。

    Returns:
        (ttwid: str, login_info: dict) 元组。
        login_info 包含 is_login、nickname、uid 三个字段。

    Raises:
        RuntimeError: 无法获取 ttwid 时抛出。
    """
    login_info = {'is_login': False, 'nickname': '', 'uid': ''}
    try:
        room_url = f"https://live.douyin.com/{live_id}"

        # 使用独立 session + 最小 headers，确保拿到 SSR 页面（内嵌 defaultHeaderUserInfo）
        # requests 的 headers 参数是合并而非替换，必须用独立 session 才能去掉 Sec-* 头
        ua = session.headers.get('User-Agent',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36')
        ssr_session = requests.Session()
        try:
            ssr_session.headers.update({
                'User-Agent': ua,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Referer': 'https://live.douyin.com/',
            })
            # 复制 cookies
            for cookie in session.cookies:
                ssr_session.cookies.set(cookie.name, cookie.value, domain=cookie.domain)

            resp = http_get_with_retry(ssr_session, room_url, timeout=http_timeout)
        finally:
            ssr_session.close()

        # 从 HTML 内嵌数据中提取登录状态
        resp_body = resp.text
        m = re.search(
            r'defaultHeaderUserInfo.*?isLogin.*?(true|false).*?nickname\\?"[,:]\\?"([^"\\]+)',
            resp_body, re.DOTALL
        )
        if m:
            login_info['is_login'] = m.group(1) == 'true'
            login_info['nickname'] = m.group(2)

        # 提取 uid
        m_uid = re.search(r'defaultHeaderUserInfo.*?uid\\?"[,:]\\?"(\d+)', resp_body, re.DOTALL)
        if m_uid:
            login_info['uid'] = m_uid.group(1)

        ttwid = resp.cookies.get('ttwid')
        if ttwid:
            session.cookies.set('ttwid', ttwid, domain='.douyin.com')
            logger.debug(f"[房间] ttwid 自动获取成功: {ttwid[:50]}...")
            return ttwid, login_info
    except Exception as e:
        logger.debug(f"[房间] ttwid 自动获取失败: {e}")

    if login_cookies.get('ttwid'):
        logger.info("[房间] ttwid 自动获取失败，使用 cookie.txt 中的 ttwid")
        return login_cookies['ttwid'], login_info

    raise RuntimeError("无法获取 ttwid Cookie，抖音可能更新了认证流程，请检查网络或更新 cookie.txt")


def download_image(session, url, save_path, timeout=15):
    """下载图片并保存到指定路径。

    Args:
        session: requests.Session 实例。
        url: 图片 URL。
        save_path: 保存路径（完整文件路径）。
        timeout: 请求超时秒数。

    Returns:
        bool: 下载成功返回 True，失败返回 False。
    """
    if not url:
        return False
    try:
        resp = session.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        resp.close()
        logger.debug(f"[下载] 图片保存成功: {save_path}")
        return True
    except Exception as e:
        logger.warning(f"[下载] 图片下载失败: {e}")
        return False


def enter_room_api(ttwid, ua, ua_version, live_id, http_timeout=15, session=None):
    """调用 /webcast/room/web/enter/ API 获取房间信息。

    Args:
        ttwid: ttwid Cookie 值。
        ua: User-Agent 字符串。
        ua_version: 'Chrome/x.x.x.x' 格式的版本字符串。
        live_id: 直播间 ID（web_rid）。
        http_timeout: HTTP 请求超时秒数。
        session: 可选的 requests.Session，传入时复用连接池和 headers。
                 未传入时创建临时 Session（向后兼容）。

    Returns:
        dict 包含以下字段:
        - room_id: 直播间真实 room_id
        - status: 状态码（2=直播中，4=未开播）
        - anchor_name: 主播昵称
        - anchor_avatar: 主播头像 URL
        - room_title: 直播间标题
        - room_cover: 直播间封面 URL
        - sec_uid: 主播 sec_uid

    Raises:
        ValueError: API 返回非 JSON 或房间数据为空时抛出。
    """
    logger.debug(f"[房间] ttwid: {ttwid[:50] if ttwid else 'None'}...")

    owns_session = session is None
    if owns_session:
        session = requests.Session()

    original_cookies = {c.name: c.value for c in session.cookies if c.domain == '.douyin.com'}
    session.cookies.set('ttwid', ttwid, domain='.douyin.com')

    browser_ver = ua_version.split('/')[1] if '/' in ua_version else '134.0.0.0'
    params = {
        'aid': APP_ID,
        'app_name': 'douyin_web',
        'live_id': '1',
        'device_platform': 'web',
        'language': 'zh-CN',
        'browser_language': 'zh-CN',
        'browser_platform': 'Win32',
        'browser_name': 'Chrome',
        'browser_version': browser_ver,
        'web_rid': live_id,
        'msToken': '',
    }
    url = f'https://live.douyin.com/webcast/room/web/enter/?{urllib.parse.urlencode(params)}'

    try:
        resp = http_get_with_retry(
            session, url,
            headers={
                'Referer': f'https://live.douyin.com/{live_id}',
                'Accept': 'application/json, text/plain, */*',
                'User-Agent': ua,
            },
            timeout=http_timeout,
        )
    finally:
        if owns_session:
            session.close()
        else:
            session.cookies.clear(domain='.douyin.com')
            for name, value in original_cookies.items():
                session.cookies.set(name, value, domain='.douyin.com')

    logger.debug(f"[网络] API 响应状态码: {resp.status_code}, Content-Type: {resp.headers.get('Content-Type', 'N/A')}")
    logger.debug(f"[网络] API 响应内容长度: {len(resp.content)}")

    resp_text = resp.text

    logger.debug(f"[网络] API 响应内容前 500 字符: {resp_text[:500]}")

    try:
        resp_data = json.loads(resp_text)
    except (ValueError, json.JSONDecodeError):
        logger.error(f"[网络] API 响应非 JSON，前 200 字符: {resp_text[:200]}")
        raise ValueError(f'API 响应非 JSON (status_code={resp.status_code})')

    data_block = resp_data.get('data')
    if not isinstance(data_block, dict):
        raise RoomNotFoundError(f'API 返回异常 (status_code={resp_data.get("status_code", "N/A")}, data类型={type(data_block).__name__})')

    logger.debug(f"[网络] API 返回 JSON 结构: status_code={resp_data.get('status_code', 'N/A')}, "
                 f"data.data length={len(data_block.get('data', []))}")

    room_list = data_block.get('data', [])
    if not room_list:
        raise RoomNotFoundError(f'房间不存在或未开播 (status_code={resp_data.get("status_code", "N/A")})')

    room = room_list[0]
    room_id = str(room.get('room_id_str', '') or room.get('room_id', '') or
                   room.get('id_str', '') or room.get('id', ''))
    status = room.get('status', 0)
    
    user = data_block.get('user', {})
    if not isinstance(user, dict):
        user = {}
    anchor_name = user.get('nickname', '')
    sec_uid = user.get('sec_uid', '')
    
    avatar_list = user.get('avatar_thumb', {}).get('url_list', [])
    anchor_avatar = avatar_list[0] if avatar_list else ''
    
    room_title = room.get('title', '')
    
    cover_list = room.get('cover', {}).get('url_list', [])
    room_cover = cover_list[0] if cover_list else ''

    stream_url = room.get('stream_url', {})

    if not room_id:
        raise ValueError('API 返回的 room_id 为空')

    return {
        'room_id': room_id,
        'status': status,
        'anchor_name': anchor_name,
        'anchor_avatar': anchor_avatar,
        'room_title': room_title,
        'room_cover': room_cover,
        'sec_uid': sec_uid,
        'stream_url': stream_url,
    }


def fetch_webcast_cursor(session, room_id, uid, ttwid, ua, login_cookies=None, http_timeout=15):
    """预请求 /webcast/im/fetch/ 获取服务端 cursor 和 internalExt。

    按照 DouYin_Spider 的 get_webcast_detail 实现，使用完整参数集。
    响应为 protobuf 格式的 Response 消息。

    Args:
        session: requests.Session 实例。
        room_id: 直播间真实 room_id。
        uid: 用户唯一 ID。
        ttwid: ttwid Cookie 值。
        ua: User-Agent 字符串。
        login_cookies: 登录 Cookie 字典（可选，用于 CSRF token）。
        http_timeout: HTTP 请求超时秒数。

    Returns:
        (cursor, internal_ext) 元组，失败时返回 (None, None)。
    """
    from base.messages import Response as ResponseProto
    from base.parser import parse_proto
    try:
        params = {
            'resp_content_type': 'protobuf',
            'did_rule': '3',
            'device_id': '',
            'app_name': 'douyin_web',
            'endpoint': 'live_pc',
            'support_wrds': '1',
            'user_unique_id': str(uid),
            'identity': 'audience',
            'need_persist_msg_count': '15',
            'insert_task_id': '',
            'live_reason': '',
            'room_id': room_id,
            'version_code': VERSION_CODE,
            'last_rtt': '0',
            'live_id': LIVE_ID,
            'aid': APP_ID,
            'fetch_rule': '1',
            'cursor': '',
            'internal_ext': '',
            'device_platform': DEVICE_PLATFORM,
            'cookie_enabled': 'true',
            'screen_width': '1920',
            'screen_height': '1080',
            'browser_language': 'zh-CN',
            'browser_platform': 'Win32',
            'browser_name': 'Mozilla',
            'browser_version': ua.split('Mozilla/')[-1] if 'Mozilla' in ua else ua,
            'browser_online': 'true',
            'tz_name': 'Asia/Shanghai',
        }
        url = f'https://live.douyin.com/webcast/im/fetch/'

        cookies = {'ttwid': ttwid}
        if login_cookies:
            for k in ('sessionid', 'passport_csrf_token', 'msToken'):
                if k in login_cookies:
                    cookies[k] = login_cookies[k]

        headers = {
            'User-Agent': ua,
            'Referer': f'https://live.douyin.com/',
            'Origin': 'https://live.douyin.com',
        }

        resp = session.get(url, params=params, cookies=cookies,
                           headers=headers, timeout=http_timeout)
        resp.raise_for_status()

        # 优先尝试 protobuf 解析
        if resp.content and len(resp.content) > 5:
            try:
                msg = parse_proto(ResponseProto, resp.content)
                cursor = msg.cursor if msg.cursor else None
                internal_ext = msg.internal_ext if msg.internal_ext else None
                if cursor or internal_ext:
                    logger.debug(f"[网络] fetch_webcast_cursor 成功: cursor={str(cursor)[:50]}...")
                    return cursor, internal_ext
            except Exception:
                pass

        # 回退：尝试 JSON 解析
        try:
            data = resp.json()
            cursor = data.get('cursor', '')
            internal_ext = data.get('internal_ext', '')
            if cursor or internal_ext:
                logger.debug(f"[网络] fetch_webcast_cursor 成功(JSON): cursor={str(cursor)[:50]}...")
                return cursor, internal_ext
        except Exception:
            pass

        logger.debug("[网络] fetch_webcast_cursor 响应中无 cursor/internalExt")
        return None, None
    except Exception as e:
        logger.debug(f"[网络] fetch_webcast_cursor 失败: {e}")
        return None, None
