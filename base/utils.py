1|"""基础工具：配置加载、Cookie 解析、常量定义、格式化、ID 生成。
2|
3|本模块是项目的共享基础层，被 service/ 和 base/ 其他模块共同依赖。
4|抖音 API 参数（APP_ID、VERSION_CODE 等）集中在此维护，更新时只需改一处。
5|"""
6|
7|import os
8|import random
9|import re
10|import threading
11|import time
12|
13|import yaml
14|
15|
16|# ── 常量 ──────────────────────────────────────────
17|
18|USER_AGENTS = [
19|    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
20|    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
21|    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
22|    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
23|    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
24|]
25|
26|# ── 签名 & API 共享参数 ───────────────────────────
27|# 抖音 Web 端参数，签名和 WebSocket URL 共用。
28|# 抖音版本更新时只需修改这里。
29|
30|APP_ID = '6383'                  # 抖音 Web 端应用 ID
31|LIVE_ID = '1'                    # 直播类型标识（1 = 普通直播）
32|VERSION_CODE = '180800'          # 客户端版本号（对应 18.08.00）
33|WEBCAST_SDK_VERSION = '1.0.15'   # WebCast SDK 版本，签名和 WS URL 须一致
34|DID_RULE = '3'                   # 设备 ID 生成规则版本（3 = 当前线上版本）
35|DEVICE_PLATFORM = 'web'          # 平台标识
36|
37|# 低频/低价值消息类型，仅计数不解析
38|LOW_VALUE_TYPES = frozenset({
39|    'WebcastRanklistHourEntranceMessage', 'WebcastRoomDataSyncMessage',
40|    'WebcastChatLikeMessage', 'WebcastResidentGuestMessage',
41|    'WebcastLowPcuGuideMessage', 'WebcastCommonDotMessage',
42|    'WebcastGiftUpdateMessage', 'WebcastInRoomBannerMessage',
43|    'WebcastNotifyEffectMessage', 'WebcastHotRoomMessage',
44|})
45|
46|# 交互类消息，用于"等待开播"模式判断直播间是否活跃
47|INTERACTIVE_TYPES = frozenset({
48|    'WebcastChatMessage', 'WebcastGiftMessage', 'WebcastLikeMessage',
49|    'WebcastMemberMessage', 'WebcastSocialMessage', 'WebcastFansclubMessage',
50|    'WebcastEmojiChatMessage',
51|})
52|
53|# WebSocket method → output config key 映射
54|# strip('Webcast','Message').lower() 后与 config key 不一致的特殊映射
55|METHOD_TO_CONFIG = {
56|    'WebcastChatMessage':                 'chat',
57|    'WebcastGiftMessage':                 'gift',
58|    'WebcastLikeMessage':                 'like',
59|    'WebcastMemberMessage':               'member',
60|    'WebcastSocialMessage':               'social',
61|    'WebcastRoomUserSeqMessage':          'stats',
62|    'WebcastFansclubMessage':             'fansclub',
63|    'WebcastControlMessage':              'control',
64|    'WebcastEmojiChatMessage':            'emoji',
65|    'WebcastRoomStatsMessage':            'roomstats',
66|    'WebcastRoomMessage':                 'room',
67|    'WebcastRoomRankMessage':             'rank',
68|    'WebcastRoomStreamAdaptationMessage': 'control',  # 无独立 config，归入 control
69|}
70|
71|SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
72|
73|_MIN_UA_SWITCH_INTERVAL = 8       # UA 切换最小间隔（秒），防止频繁切换触发风控
74|_ua_switch_lock = threading.Lock()
75|_last_ua_switch_time = 0.0
76|
77|
78|# ── 配置加载 ──────────────────────────────────────
79|
80|def load_config(config_file, default_config):
81|    """加载 YAML 配置文件，与默认配置做浅合并。
82|
83|    字典类型的配置项（如 output）做一层嵌套合并，
84|    非字典类型直接覆盖。文件不存在时返回默认配置。
85|
86|    Args:
87|        config_file: 配置文件路径（相对路径相对于项目根目录）。
88|        default_config: 默认配置字典。
89|
90|    Returns:
91|        合并后的配置字典。
92|    """
93|    if not os.path.isabs(config_file):
94|        config_file = os.path.join(SCRIPT_DIR, config_file)
95|
96|    if not os.path.exists(config_file):
97|        base = os.path.splitext(config_file)[0]
98|        for ext in ['.yaml', '.yml']:
99|            alt = base + ext
100|            if os.path.exists(alt):
101|                config_file = alt
102|                break
103|
104|    try:
105|        with open(config_file, 'r', encoding='utf-8') as f:
106|            user_cfg = yaml.safe_load(f.read()) or {}
107|        cfg = dict(default_config)
108|        for k, v in user_cfg.items():
109|            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
110|                cfg[k] = {**cfg[k], **v}
111|            else:
112|                cfg[k] = v
113|        return cfg
114|    except (FileNotFoundError, yaml.YAMLError) as e:
115|        print(f"配置加载失败({e})，使用默认配置")
116|        return dict(default_config)
117|
118|
119|def load_cookies(cookie_file, script_dir=''):
120|    """加载 Cookie 文件，自动识别三种格式。
121|
122|    支持格式：
123|    - 浏览器导出：name1=value1; name2=value2
124|    - 每行一个：name1=value1（多行）
125|    - Netscape cookie jar：带 tab 分隔的 7 列格式
126|
127|    Args:
128|        cookie_file: Cookie 文件路径。
129|        script_dir: 相对路径的基准目录（为空时使用项目根目录）。
130|
131|    Returns:
132|        {cookie_name: cookie_value} 字典，文件不存在时返回空字典。
133|    """
134|    if not os.path.isabs(cookie_file):
135|        cookie_file = os.path.join(script_dir, cookie_file)
136|    if not os.path.exists(cookie_file):
137|        return {}
138|
139|    try:
140|        with open(cookie_file, 'r', encoding='utf-8') as f:
141|            content = f.read().strip()
142|    except Exception:
143|        return {}
144|    if not content:
145|        return {}
146|
147|    cookies = {}
148|    lines = content.splitlines()
149|    is_netscape = any(line.count('\t') >= 6 and not line.startswith('#') for line in lines[:10])
150|
151|    if is_netscape:
152|        for line in lines:
153|            line = line.strip()
154|            if not line or line.startswith('#'):
155|                continue
156|            parts = line.split('\t')
157|            if len(parts) >= 7:
158|                name, value = parts[5].strip(), parts[6].strip()
159|                if name:
160|                    cookies[name] = value
161|    else:
162|        content = content.replace('\n', ';').replace('\r', '')
163|        for item in content.split(';'):
164|            item = item.strip()
165|            if not item or '=' not in item:
166|                continue
167|            name, value = item.split('=', 1)
168|            if name.strip():
169|                cookies[name.strip()] = value.strip()
170|    return cookies
171|
172|
173|# ── 配置写回 ──────────────────────────────────────
174|
175|_config_write_lock = threading.RLock()
176|
177|
178|def update_room_name_in_config(room_id, anchor_name, rooms_file='rooms.txt'):
179|    """更新或添加 rooms.txt 中的房间记录。
180|
181|    线程安全：通过可重入锁防止多房间并发写入。
182|    - 房间已存在：更新主播名
183|    - 房间不存在：追加到文件末尾
184|    - 文件不存在：创建文件并写入
185|
186|    Args:
187|        room_id: 直播间 ID。
188|        anchor_name: 主播昵称。
189|        rooms_file: 房间文件路径（相对于项目根目录）。
190|    """
191|    if not anchor_name:
192|        return
193|    if not os.path.isabs(rooms_file):
194|        rooms_file = os.path.join(SCRIPT_DIR, rooms_file)
195|
196|    with _config_write_lock:
197|        try:
198|            if not os.path.exists(rooms_file):
199|                with open(rooms_file, 'w', encoding='utf-8') as f:
200|                    f.write(f'{room_id},{anchor_name}\n')
201|                return
202|
203|            with open(rooms_file, 'r', encoding='utf-8') as f:
204|                lines = f.readlines()
205|
206|            updated = False
207|            found = False
208|            new_lines = []
209|
210|            for line in lines:
211|                stripped = line.strip()
212|                if not stripped:
213|                    new_lines.append(line)
214|                    continue
215|
216|                prefix = ''
217|                content = stripped
218|                if stripped.startswith('#'):
219|                    prefix = '#'
220|                    content = stripped[1:].strip()
221|
222|                if not content:
223|                    new_lines.append(line)
224|                    continue
225|
226|                parts = content.split(',', 1)
227|                if parts[0].strip() == room_id:
228|                    indent = re.match(r'^(\s*)', line).group(1) if re.match(r'^(\s*)', line) else ''
229|                    new_lines.append(f'{indent}{prefix}{room_id},{anchor_name}\n')
230|                    updated = True
231|                    found = True
232|                else:
233|                    new_lines.append(line)
234|
235|            if not found:
236|                if new_lines and not new_lines[-1].endswith('\n'):
237|                    new_lines.append('\n')
238|                new_lines.append(f'{room_id},{anchor_name}\n')
239|                updated = True
240|
241|            if updated:
242|                import tempfile
243|                import shutil
244|
245|                fd, temp_path = tempfile.mkstemp(suffix='.txt', dir=os.path.dirname(rooms_file))
246|                try:
247|                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
248|                        f.writelines(new_lines)
249|                    shutil.move(temp_path, rooms_file)
250|                except Exception:
251|                    if os.path.exists(temp_path):
252|                        os.remove(temp_path)
253|                    raise
254|
255|        except Exception as e:
256|            try:
257|                logger = logging.getLogger(__name__)
258|                logger.error(f"[配置] 更新主播名失败：room_id={room_id}, error={e}")
259|            except Exception:
260|                pass
261|
262|
263|# ── 工具函数 ──────────────────────────────────────
264|
265|def generate_user_unique_id():
266|    """生成随机用户唯一 ID，用于 WebSocket 连接标识。
267|
268|    Returns:
269|        18~19 位随机数字字符串。
270|    """
271|    return str(random.randint(10**18, 10**19 - 1))
272|
273|
274|def generate_ms_token(length=182):
275|    """生成随机 msToken 字符串，用于 HTTP 请求参数。
276|
277|    Args:
278|        length: token 主体长度（不含末尾 '=_' 后缀）。
279|
280|    Returns:
281|        指定长度的随机字符串 + '=_' 后缀。
282|    """
283|    charset = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+='
284|    return ''.join(random.choice(charset) for _ in range(length)) + '=_'
285|
286|
287|def extract_ua_version(ua: str) -> str:
288|    """从 User-Agent 字符串中提取 Chrome 版本号。
289|
290|    Args:
291|        ua: 完整的 User-Agent 字符串。
292|
293|    Returns:
294|        'Chrome/x.x.x.x' 格式的版本字符串，无法匹配时返回默认值。
295|    """
296|    m = re.search(r'Chrome/(\d+\.\d+\.\d+\.\d+)', ua)
297|    return f"Chrome/{m.group(1)}" if m else "Chrome/132.0.0.0"
298|
299|
300|def fmt_fans_club(user):
301|    """格式化用户的粉丝团信息为显示字符串。
302|
303|    Args:
304|        user: protobuf User 对象。
305|
306|    Returns:
307|        '[粉丝团:名称 Lv等级]' 或 '[粉丝团 Lv等级]'，无粉丝团时返回空字符串。
308|    """
309|    try:
310|        club = user.fans_club.data
311|        if club and club.club_name:
312|            return f"[粉丝团:{club.club_name} Lv{club.level}]"
313|        elif club and club.level > 0:
314|            return f"[粉丝团 Lv{club.level}]"
315|    except (AttributeError, TypeError):
316|        pass
317|    return ''
318|
319|
320|def fmt_grade(user):
321|    """格式化用户的消费等级为显示字符串。
322|
323|    Args:
324|        user: protobuf User 对象。
325|
326|    Returns:
327|        '[等级N]' 格式字符串，等级为 0 或缺失时返回空字符串。
328|    """
329|    try:
330|        if user.pay_grade and user.pay_grade.level > 0:
331|            return f"[等级{user.pay_grade.level}]"
332|    except (AttributeError, TypeError):
333|        pass
334|    return ''
335|
336|
337|def rotate_ua(current_ua):
338|    """重连时切换 User-Agent，降低风控风险。
339|
340|    两次切换间隔不足 _MIN_UA_SWITCH_INTERVAL 秒时跳过，
341|    避免重连密集期频繁切换反而触发异常检测。
342|
343|    线程安全：多实例并发时通过锁保护全局切换时间。
344|
345|    Args:
346|        current_ua: 当前使用的 User-Agent 字符串。
347|
348|    Returns:
349|        (新 UA 字符串, 新 UA 版本字符串) 元组。
350|    """
351|    global _last_ua_switch_time
352|    with _ua_switch_lock:
353|        now = time.time()
354|        if now - _last_ua_switch_time < _MIN_UA_SWITCH_INTERVAL:
355|            return current_ua, extract_ua_version(current_ua)
356|        candidates = [u for u in USER_AGENTS if u != current_ua]
357|        if not candidates:
358|            return current_ua, extract_ua_version(current_ua)
359|        new_ua = random.choice(candidates)
360|        _last_ua_switch_time = now
361|        return new_ua, extract_ua_version(new_ua)
362|
363|
364|def get_user_id(user):
365|    """获取用户 ID 字符串，优先使用 id_str（大数精度更高）。
366|
367|    Args:
368|        user: protobuf User 对象。
369|
370|    Returns:
371|        用户 ID 字符串。
372|    """
373|    s = user.id_str
374|    return s if s else str(user.id)
375|