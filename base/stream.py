"""推流地址解析：从房间信息中选取指定画质的推流 URL。

抖音 API 返回的 flv_pull_url 为 {质量键: URL} 字典。
支持两种 API 格式:
  - 旧版: ORIGIN → UHD → HD → SD → LD
  - 新版: FULL_HD1 → HD1 → SD1 → SD2
支持画质降级回退和推流地址连通性检测。

画质名称与抖音官方 (live_core_sdk_data.pull_data.options.qualities) 一致:
  - 原画 (level 5, sdk_key=origin)        → 最高
  - 蓝光 (level 4, sdk_key=uhd)
  - 超清 (level 3, sdk_key=hd)
  - 高清 (level 2, sdk_key=sd)
  - 标清 (level 1, sdk_key=ld)            → 最低
"""

import logging

import requests

logger = logging.getLogger(__name__)

# API 返回的推流质量键，按画质从高到低排列（兼容新旧两种 API 格式）
_QUALITY_KEYS = [
    'ORIGIN', 'FULL_HD1',     # 原画 / 蓝光
    'UHD',                     # 蓝光
    'HD', 'HD1',               # 超清
    'SD', 'SD2',               # 高清
    'LD', 'SD1',               # 标清
]

# API key → 画质索引映射
_KEY_TO_INDEX = {
    'ORIGIN': 0, 'FULL_HD1': 0,
    'UHD': 1,
    'HD': 2, 'HD1': 2,
    'SD': 3, 'SD2': 3,
    'LD': 4, 'SD1': 4,
}

# 画质名称 → 索引映射（与抖音官方名一致，支持 sdk_key 别名）
_QUALITY_NAMES = {
    '原画': 0, 'origin': 0,
    '蓝光': 1, 'uhd': 1, 'blue': 1,
    '超清': 2, 'hd': 2,
    '高清': 3, 'sd': 3,
    '标清': 4, 'ld': 4,
}

# 画质索引 → 中文名（与抖音官方名一致，用于回退时输出）
_QUALITY_LABELS = ['原画', '蓝光', '超清', '高清', '标清']


def _check_url(url, timeout=10):
    """检查推流地址是否可访问。

    Args:
        url: 推流 URL。
        timeout: 超时秒数。

    Returns:
        bool: URL 可访问返回 True。
    """
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        return resp.status_code < 400
    except requests.RequestException:
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.close()
            return resp.status_code < 400
        except requests.RequestException:
            return False


def _build_ordered_list(raw_dict):
    """将 API 返回的 {key: url} 字典按画质排序为列表。

    使用 _KEY_TO_INDEX 将 API key 映射到画质索引 [0..4]，
    未识别的 key 按字典顺序追加到末尾。

    Args:
        raw_dict: flv_pull_url 或 hls_pull_url_map 字典。

    Returns:
        长度 5 的 URL 列表，索引 0=原画 4=标清。缺失画质用前一级填充。
    """
    if not raw_dict:
        return []
    # 按画质索引排序
    indexed = {}
    for key, url in raw_dict.items():
        idx = _KEY_TO_INDEX.get(key)
        if idx is not None and idx not in indexed:
            indexed[idx] = url
    if not indexed:
        # 全部未识别，按字典顺序取
        for i, url in enumerate(raw_dict.values()):
            if i < 5:
                indexed[i] = url
    # 构建 5 元素列表，缺失的用前一级填充
    values = []
    for i in range(5):
        if i in indexed:
            values.append(indexed[i])
        elif values:
            values.append(values[-1])
    return values


def select_stream_url(room_info, quality_name='原画', check_health=True, quiet=False):
    """从房间信息中选取指定画质的推流 URL。

    按用户指定的画质选取推流地址，如果该画质的地址不可达，
    自动降级到更低一档。降级时记录日志。

    Args:
        room_info: enter_room_api 返回的房间信息字典。
        quality_name: 画质名称（原画/蓝光/超清/高清/标清，支持 sdk_key 别名）。
        check_health: 是否对推流地址做连通性检测。

    Returns:
        dict:
        - is_live: bool，是否直播中。
        - flv_url: FLV 推流地址。
        - m3u8_url: HLS 推流地址。
        - record_url: 选用的录制地址（优先 flv）。
        - quality: 最终选用的画质中文名。
    """
    result = {
        'is_live': False,
        'flv_url': '',
        'm3u8_url': '',
        'record_url': '',
        'quality': quality_name,
    }

    if room_info.get('status') != 2:
        return result

    stream_url = room_info.get('stream_url')
    if not stream_url:
        logger.warning("[推流] 房间信息中无推流地址")
        return result

    flv_pull_url = stream_url.get('flv_pull_url', {})
    hls_pull_url_map = stream_url.get('hls_pull_url_map', {})

    if not flv_pull_url and not hls_pull_url_map:
        logger.warning("[推流] 推流地址为空")
        return result

    quality_index = _QUALITY_NAMES.get(quality_name, 0)
    quality_index = max(0, min(quality_index, 4))

    flv_values = _build_ordered_list(flv_pull_url)
    hls_values = _build_ordered_list(hls_pull_url_map)

    selected_label = quality_name
    record_url = ''

    # 尝试从指定画质开始，依次降级
    for attempt in range(quality_index, len(_QUALITY_LABELS)):
        fi = min(attempt, len(flv_values) - 1) if flv_values else -1
        hi = min(attempt, len(hls_values) - 1) if hls_values else -1
        flv_url = flv_values[fi] if fi >= 0 else ''
        m3u8_url = hls_values[hi] if hi >= 0 else ''
        candidate = flv_url or m3u8_url

        if not candidate:
            continue

        if check_health:
            ok = _check_url(flv_url or m3u8_url) if candidate else False
            if not ok:
                if attempt < len(_QUALITY_LABELS) - 1:
                    logger.info(f"[推流] 画质 {_QUALITY_LABELS[attempt]} 不可达，降级到 {_QUALITY_LABELS[attempt + 1]}")
                continue

        record_url = candidate
        selected_label = _QUALITY_LABELS[attempt] if attempt < len(_QUALITY_LABELS) else quality_name
        break

    if not record_url:
        for attempt in range(quality_index, len(_QUALITY_LABELS)):
            fi = min(attempt, len(flv_values) - 1) if flv_values else -1
            hi = min(attempt, len(hls_values) - 1) if hls_values else -1
            url = (flv_values[fi] if fi >= 0 else '') or (hls_values[hi] if hi >= 0 else '')
            if url:
                record_url = url
                selected_label = _QUALITY_LABELS[attempt]
                break

    if not record_url:
        logger.warning(f"[推流] 画质 {quality_name} 无可用地址")
        return result

    actual_index = _QUALITY_NAMES.get(selected_label, quality_index)
    actual_flv = flv_values[min(actual_index, len(flv_values) - 1)] if flv_values else ''
    actual_hls = hls_values[min(actual_index, len(hls_values) - 1)] if hls_values else ''

    result.update({
        'is_live': True,
        'flv_url': actual_flv,
        'm3u8_url': actual_hls,
        'record_url': record_url,
        'quality': selected_label,
    })

    if selected_label != quality_name:
        logger.info(f"[推流] 画质降级: {quality_name} → {selected_label}")
    if not quiet:
        logger.info(f"[推流] 选取画质 {selected_label}，地址: {record_url[:80]}...")
    return result
