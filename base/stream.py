"""推流地址解析：从房间信息中选取指定画质的推流 URL。

抖音 API 返回的 flv_pull_url 为 {质量键: URL} 字典，
按 ORIGIN → UHD → HD → SD → LD 优先级排序。
支持画质降级回退和推流地址连通性检测。
"""

import logging

import requests

logger = logging.getLogger(__name__)

# API 返回的推流质量键，按画质从高到低排列
_QUALITY_KEYS = ['ORIGIN', 'UHD', 'HD', 'SD', 'LD']

# 画质名称 → 索引映射（支持别名）
_QUALITY_NAMES = {
    '原画': 0, 'origin': 0, '4k': 0,
    '超清': 1, 'uhd': 1,
    '高清': 2, 'hd': 2, 'high': 2,
    '标清': 3, 'sd': 3, 'medium': 3,
    '省流': 4, 'ld': 4, 'low': 4,
}

# 画质索引 → 中文名（用于回退时输出）
_QUALITY_LABELS = ['原画', '超清', '高清', '标清', '省流']


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
        return resp.status_code < 500
    except requests.RequestException:
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.close()
            return resp.status_code < 500
        except requests.RequestException:
            return False


def _build_ordered_list(raw_dict):
    """将 API 返回的 {key: url} 字典按 _QUALITY_KEYS 排序为列表。

    Args:
        raw_dict: flv_pull_url 或 hls_pull_url_map 字典。

    Returns:
        排序后的 URL 列表。
    """
    if not raw_dict:
        return []
    values = []
    for key in _QUALITY_KEYS:
        url = raw_dict.get(key)
        if url:
            values.append(url)
    if not values:
        values = list(raw_dict.values())
    while len(values) < 5 and values:
        values.append(values[-1])
    return values


def select_stream_url(room_info, quality_name='原画', check_health=True):
    """从房间信息中选取指定画质的推流 URL。

    按用户指定的画质选取推流地址，如果该画质的地址不可达，
    自动降级到更低一档。降级时记录日志。

    Args:
        room_info: enter_room_api 返回的房间信息字典。
        quality_name: 画质名称（原画/超清/高清/标清/省流，支持英文别名）。
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

    actual_flv = flv_values[min(quality_index, len(flv_values) - 1)] if flv_values else ''
    actual_hls = hls_values[min(quality_index, len(hls_values) - 1)] if hls_values else ''

    result.update({
        'is_live': True,
        'flv_url': actual_flv,
        'm3u8_url': actual_hls,
        'record_url': record_url,
        'quality': selected_label,
    })

    if selected_label != quality_name:
        logger.info(f"[推流] 画质降级: {quality_name} → {selected_label}")
    logger.info(f"[推流] 选取画质 {selected_label}，地址: {record_url[:80]}...")
    return result
