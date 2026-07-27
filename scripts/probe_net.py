#!/usr/bin/env python
# coding: utf-8
"""Probe: resolve a live room, list all quality stream URLs, and A/B benchmark
FLV pull throughput DIRECT (no proxy) vs via PROXY. Prints a decision."""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from service.network import fetch_ttwid, enter_room_api, resolve_live_id, build_http_headers
from base.utils import USER_AGENTS, WEBCAST_SDK_VERSION, extract_ua_version
from base.stream import _build_ordered_list

PROXY = {'http': os.environ.get('HTTP_PROXY'), 'https': os.environ.get('HTTPS_PROXY')}
ROOM = sys.argv[1] if len(sys.argv) > 1 else '56697889278'
UA = USER_AGENTS[0]
UAV = extract_ua_version(UA)

def make_session():
    s = requests.Session()
    s.headers.update(build_http_headers(UA, UAV))
    return s

def get_room_info(room):
    s = make_session()
    live_id = resolve_live_id(room, s)
    ttwid, login = fetch_ttwid(s, live_id, {})
    info = enter_room_api(ttwid, UA, UAV, live_id, session=s)
    return live_id, info

def bench(url, use_proxy, seconds=8):
    """Download from url for `seconds`, return (bytes, mbps)."""
    hdr = {'User-Agent': UA}
    s = requests.Session()
    if use_proxy:
        s.proxies = PROXY
    else:
        s.trust_env = False          # ignore HTTP_PROXY/HTTPS_PROXY/ALL_PROXY env
        s.proxies = {}
    t0 = time.time(); total = 0
    try:
        r = s.get(url, headers=hdr, stream=True, timeout=15)
        for chunk in r.iter_content(65536):
            total += len(chunk)
            if time.time() - t0 >= seconds:
                break
        r.close()
    except Exception as e:
        return None, f"ERROR: {type(e).__name__}: {str(e)[:120]}"
    dt = time.time() - t0
    mbps = (total * 8 / 1e6) / dt if dt > 0 else 0
    return total, mbps

def main():
    print(f"== resolving room {ROOM} ==")
    live_id, info = get_room_info(ROOM)
    status = info.get('status')
    print(f"live_id={live_id} anchor={info.get('anchor_name')} status={status} (2=live)")
    if status != 2:
        print("NOT LIVE — pick another room"); return
    su = info.get('stream_url') or {}
    flv = su.get('flv_pull_url', {}) or {}
    print("== available FLV qualities ==")
    for k, v in flv.items():
        print(f"  {k}: {v[:90]}")
    ordered = _build_ordered_list(flv)  # index 0=原画(highest) .. 4=标清(lowest)
    if not ordered:
        print("no flv urls"); return
    low_url = ordered[-1]   # 标清 = lowest quality = the subset we'll record
    print(f"\n== benchmark 标清/lowest FLV (8s each) ==\nlow_url:  {low_url[:80]}")
    for label, use_proxy in [("DIRECT (no proxy)", False), ("PROXY", True)]:
        b, mbps = bench(low_url, use_proxy)
        if b is None:
            print(f"  {label:20s}: {mbps}")
        else:
            print(f"  {label:20s}: {b/1e6:6.2f} MB in 8s  =>  {mbps:6.2f} Mbps")

if __name__ == '__main__':
    main()
