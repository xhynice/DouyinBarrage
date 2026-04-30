"""签名生成：subprocess 调用 Node.js 执行 sign.js。

签名流程：
    13 个参数拼接 → MD5 哈希 → Node.js 运行 sign.js → 返回 X-Bogus 签名。

sign.js 包含完整的 webmssdk + Proxy polyfill，来自 DouYin_Spider 项目。
首次调用时会复制 sign.js 到临时目录并追加 stdin 读取接口，原始文件不被修改。
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse

from base.utils import (
    SCRIPT_DIR, APP_ID, LIVE_ID, VERSION_CODE,
    WEBCAST_SDK_VERSION, DID_RULE, DEVICE_PLATFORM,
)

logger = logging.getLogger(__name__)

SIGN_JS_ORIG = os.path.join(SCRIPT_DIR, 'sign.js')

# 临时目录中 patch 后的 sign.js 路径，进程生命周期内不变
_SIGN_JS_PATCHED = None
_sign_init_lock = threading.Lock()

# stdin wrapper：注入到 sign.js 末尾，使 Python subprocess 可通过 stdin 传入 MD5
_STDIN_WRAPPER = '''

// stdin 接口 - 供 Python subprocess 调用
if (typeof process !== 'undefined' && process.stdin) {
    var _input = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', function(c) { _input += c; });
    process.stdin.on('end', function() {
        try {
            var result = get_signature(_input.trim());
            process.stdout.write(JSON.stringify(result));
        } catch (e) {
            process.stderr.write('sign error: ' + e.message);
            process.exit(1);
        }
    });
}
'''


def _ensure_sign_js():
    """确保有带 stdin wrapper 的 sign.js 可用。

    策略：
    1. 如果原始文件已包含 stdin wrapper → 直接使用原始文件（兼容旧版）
    2. 否则 → 复制到临时目录并追加 wrapper，不修改原始文件

    进程内只执行一次（_SIGN_JS_PATCHED 缓存结果）。
    线程安全：加锁防止多线程并发初始化。

    Returns:
        可执行的 sign.js 文件路径。
    """
    global _SIGN_JS_PATCHED
    if _SIGN_JS_PATCHED is not None:
        return _SIGN_JS_PATCHED

    with _sign_init_lock:
        # double-check: 拿到锁后再检查一次
        if _SIGN_JS_PATCHED is not None:
            return _SIGN_JS_PATCHED

        with open(SIGN_JS_ORIG, 'r', encoding='utf-8') as f:
            content = f.read()

        # 原始文件已有 wrapper → 直接使用（不复制）
        if '// stdin 接口 - 供 Python subprocess 调用' in content:
            _SIGN_JS_PATCHED = SIGN_JS_ORIG
            logger.debug("[签名] sign.js 已包含 stdin 接口，直接使用")
            return _SIGN_JS_PATCHED

        # 复制到临时目录并追加 wrapper
        tmp_dir = os.path.join(tempfile.gettempdir(), 'douyin_sign')
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, 'sign.js')

        shutil.copy2(SIGN_JS_ORIG, tmp_path)
        with open(tmp_path, 'a', encoding='utf-8') as f:
            f.write(_STDIN_WRAPPER)

        _SIGN_JS_PATCHED = tmp_path
        logger.debug(f"[签名] 已复制 sign.js 到临时目录: {tmp_path}")
        return _SIGN_JS_PATCHED


def generate_signature(room_id, user_unique_id):
    """生成 WebSocket 连接的 X-Bogus 签名。

    拼接 13 个业务参数 → MD5 哈希 → 通过 stdin 传入 Node.js 执行 sign.js →
    从 stdout 的最后一行 JSON 中提取 X-Bogus 值。

    Args:
        room_id: 直播间真实 room_id。
        user_unique_id: 用户唯一 ID（随机 18~19 位数字）。

    Returns:
        X-Bogus 签名字符串，失败时返回空字符串。
    """
    raw_string = (
        f"live_id={LIVE_ID},aid={APP_ID},version_code={VERSION_CODE},"
        f"webcast_sdk_version={WEBCAST_SDK_VERSION},room_id={room_id},"
        f"sub_room_id=,sub_channel_id=,did_rule={DID_RULE},"
        f"user_unique_id={user_unique_id},device_platform={DEVICE_PLATFORM},"
        f"device_type=,ac=,identity=audience"
    )
    x_ms_stub = hashlib.md5(raw_string.encode("utf-8")).hexdigest()
    logger.debug(f"[签名] 输入 MD5: {x_ms_stub}")

    proc = None
    try:
        sign_js = _ensure_sign_js()

        proc = subprocess.run(
            ['node', sign_js],
            input=x_ms_stub,
            capture_output=True, text=True, timeout=15,
        )

        if proc.returncode == 0 and proc.stdout.strip():
            # stdout 可能被 webmssdk 内部 console.log 污染，取最后一行 JSON
            lines = [l.strip() for l in proc.stdout.strip().split('\n') if l.strip()]
            json_line = ''
            for line in reversed(lines):
                if line.startswith('{') and 'X-Bogus' in line:
                    json_line = line
                    break
            if json_line:
                result = json.loads(json_line)
                xbogus = (result or {}).get("X-Bogus", "")
                logger.debug(f"[签名] X-Bogus 结果: {xbogus} (长度={len(xbogus)})")
                return xbogus
            else:
                logger.error(f"[签名] stdout 未找到 JSON: {proc.stdout[:300]}")
        else:
            stderr_out = proc.stderr.strip() if proc.stderr else '(无 stderr)'
            logger.error(f"[签名] X-Bogus 签名生成失败 (exit={proc.returncode}): {stderr_out}")
    except subprocess.TimeoutExpired:
        logger.error("[签名] X-Bogus 签名生成超时 (>15s)")
    except FileNotFoundError:
        logger.error(f"[签名] Node.js 未找到，无法执行 sign.js")
    except json.JSONDecodeError as e:
        stdout_preview = proc.stdout[:200] if proc and proc.stdout else '(无 stdout)'
        logger.error(f"[签名] X-Bogus 结果 JSON 解析失败: {e}, stdout={stdout_preview}")
    except Exception as e:
        logger.error(f"[签名] X-Bogus 签名生成异常: {e}")

    return ''
