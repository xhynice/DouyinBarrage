#!/usr/bin/env python3
"""弹幕数据构建脚本：将原始 CSV/JSONL 转换为前端可用的 JSON 格式。

使用方式:
    python scripts/build_barrage.py

输出结构:
    docs/data/barrage/
    ├── index.json                    # 全局索引
    └── {live_id}/
        ├── index.json                # 直播间索引
        └── {session_id}/
            ├── meta.json             # 会话元数据
            ├── chat.jsonl            # 弹幕数据
            ├── gift.jsonl            # 礼物数据
            └── ...
"""

import os
import json
import csv
import glob
import shutil
from datetime import datetime
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SCRIPT_DIR, 'data')
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'docs', 'data', 'barrage')

# 消息类型 → 前端展示配置，key 为 CSV/JSONL 文件名后缀（如 chat.csv）
# label: 显示名称，icon: 前端图标
TYPE_CONFIG = {
    'chat': {'label': '弹幕', 'icon': ''},
    'gift': {'label': '礼物', 'icon': '🎁'},
    'lucky_bag': {'label': '福袋', 'icon': '🎯'},
    'member': {'label': '进场', 'icon': '👤'},
    'social': {'label': '关注', 'icon': '❤️'},
    'like': {'label': '点赞', 'icon': '👍'},
    'fansclub': {'label': '粉丝团', 'icon': '🏆'},
    'stats': {'label': '统计', 'icon': '📊'},
    'roomstats': {'label': '房间统计', 'icon': '📊'},
    'room': {'label': '房间', 'icon': '🏠'},
    'rank': {'label': '排行', 'icon': '🏅'},
    'control': {'label': '控制', 'icon': '⚙️'},
    'emoji': {'label': '表情', 'icon': '😀'},
}

FORMAT_PRIORITY = {'.jsonl': 1, '.csv': 2}


class BarrageBuilder:
    def __init__(self, data_dir=DATA_DIR, output_dir=OUTPUT_DIR):
        """初始化构建器。

        Args:
            data_dir: 原始数据根目录（含各 live_id 子目录）。
            output_dir: 前端 JSON 输出目录。
        """
        self.data_dir = data_dir
        self.output_dir = output_dir
    
    def build(self):
        """构建所有直播间的弹幕数据。"""
        os.makedirs(self.output_dir, exist_ok=True)
        
        all_live_rooms = []
        
        for live_id in sorted(os.listdir(self.data_dir)):
            live_dir = os.path.join(self.data_dir, live_id)
            if not os.path.isdir(live_dir):
                continue
            
            print(f"处理直播间: {live_id}")
            sessions = self.build_live_room(live_id, live_dir)
            
            if sessions:
                anchor_name = self.get_anchor_name(live_dir)
                all_live_rooms.append({
                    'live_id': live_id,
                    'anchor_name': anchor_name,
                    'session_count': len(sessions),
                    'latest_session': sessions[-1]['session_id'] if sessions else None,
                    'total_stats': self.sum_stats(sessions)
                })
        
        index_file = os.path.join(self.output_dir, 'index.json')
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump({
                'live_rooms': all_live_rooms,
                'type_config': TYPE_CONFIG,
                'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }, f, ensure_ascii=False, indent=2)
        
        print(f"\n构建完成: {len(all_live_rooms)} 个直播间")
        print(f"输出目录: {self.output_dir}")
    
    def get_anchor_name(self, live_dir):
        """从 meta.json 获取主播名字。"""
        meta = self.load_meta(live_dir)
        return meta.get('anchor_name', '') if meta else ''

    def load_meta(self, live_dir):
        """从 meta.json 加载直播间元数据。"""
        meta_file = os.path.join(live_dir, 'meta.json')
        if os.path.exists(meta_file):
            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return None
    
    def sum_stats(self, sessions):
        """汇总所有会话的统计。"""
        total = defaultdict(int)
        for session in sessions:
            for k, v in session.get('stats', {}).items():
                total[k] += v
        return dict(total)
    
    def build_live_room(self, live_id, live_dir):
        """构建单个直播间的数据。"""
        output_live_dir = os.path.join(self.output_dir, live_id)
        os.makedirs(output_live_dir, exist_ok=True)

        meta = self.load_meta(live_dir)

        for asset in ['avatar.jpg', 'cover.jpg']:
            src = os.path.join(live_dir, asset)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(output_live_dir, asset))
                print(f"  复制 {asset}")

        sessions = []

        for ym_dir in sorted(os.listdir(live_dir)):
            ym_path = os.path.join(live_dir, ym_dir)
            if not os.path.isdir(ym_path):
                continue

            session_groups = self.detect_sessions(ym_path)

            for session_id in sorted(session_groups.keys()):
                files = session_groups[session_id]
                session_data = self.build_session(live_id, session_id, files, output_live_dir)
                if session_data:
                    sessions.append(session_data)

        room_index = {
            'live_id': live_id,
            'sessions': sessions
        }

        if meta:
            room_index['anchor_name'] = meta.get('anchor_name', '')
            room_index['anchor_avatar'] = meta.get('anchor_avatar', '')
            room_index['room_title'] = meta.get('room_title', '')

        index_file = os.path.join(output_live_dir, 'index.json')
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(room_index, f, ensure_ascii=False, indent=2)

        return sessions
    
    def detect_sessions(self, ym_path):
        """检测会话分组（支持 CSV 和 JSONL）。

        文件命名格式: {timestamp}_{room_id}_{type}.{csv|jsonl}
        如 20250401_1430_123456_chat.jsonl
        按前 3 段 (timestamp_room_id) 作为 session_id 分组，
        同一会话的不同类型文件归入同一组。

        Args:
            ym_path: 年月目录路径（如 data/{live_id}/202504）。

        Returns:
            {session_id: [file_path, ...]} 字典。
        """
        sessions = defaultdict(list)
        
        for pattern in ['*_*.jsonl', '*_*.csv']:
            for file_path in glob.glob(os.path.join(ym_path, pattern)):
                filename = os.path.basename(file_path)
                parts = filename.replace('.jsonl', '').replace('.csv', '').split('_')
                if len(parts) >= 4:
                    session_id = f"{parts[0]}_{parts[1]}_{parts[2]}"
                    sessions[session_id].append(file_path)
        
        return sessions
    
    def build_session(self, live_id, session_id, files, output_live_dir):
        """构建单个会话数据。"""
        type_files = defaultdict(list)
        for file_path in files:
            type_name = self.extract_type(file_path)
            if type_name in TYPE_CONFIG:
                type_files[type_name].append(file_path)
        
        if not type_files:
            return None
        
        output_session_dir = os.path.join(output_live_dir, session_id)
        os.makedirs(output_session_dir, exist_ok=True)
        
        available_types = []
        stats = {}
        
        for type_name in sorted(type_files.keys()):
            file_list = type_files[type_name]
            file_list.sort(key=lambda x: FORMAT_PRIORITY.get(os.path.splitext(x)[1], 99))
            
            chosen_file = file_list[0]
            output_file = os.path.join(output_session_dir, f"{type_name}.jsonl")
            
            self.copy_or_convert(chosen_file, output_file)
            
            available_types.append(type_name)
            stats[type_name] = self.count_lines(output_file)
            
            print(f"  {session_id}/{type_name}: {stats[type_name]} 条 ({os.path.basename(chosen_file)})")
        
        meta = {
            'session_id': session_id,
            'live_id': live_id,
            'available_types': available_types,
            'stats': stats,
            'total': sum(stats.values())
        }

        rankings = self.compute_rankings(output_session_dir, available_types)
        if rankings:
            meta['rankings'] = rankings

        gift_diamond = self.compute_gift_diamond(output_session_dir)
        if gift_diamond > 0:
            meta['gift_diamond'] = gift_diamond

        total_pv = self.compute_total_pv(output_session_dir)
        if total_pv:
            meta['total_pv'] = total_pv
        
        meta_file = os.path.join(output_session_dir, 'meta.json')
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        
        return meta
    
    def extract_type(self, file_path):
        """从文件名提取类型。"""
        filename = os.path.basename(file_path)
        name = filename.replace('.jsonl', '').replace('.csv', '')
        parts = name.split('_')
        return parts[-1] if parts else None
    
    def copy_or_convert(self, src, dst):
        """复制或转换文件。"""
        if src.endswith('.jsonl'):
            shutil.copy(src, dst)
        elif src.endswith('.csv'):
            with open(src, 'r', encoding='utf-8-sig') as fin:
                with open(dst, 'w', encoding='utf-8') as fout:
                    reader = csv.DictReader(fin)
                    for row in reader:
                        for key in ('gift_count', 'diamond_total', 'count', 'total', 'current', 'total_pv', 'total_user', 'online_anchor', 'member_count', 'follow_count'):
                            if key in row and row[key]:
                                try:
                                    row[key] = int(row[key])
                                except ValueError:
                                    try:
                                        row[key] = float(row[key])
                                    except ValueError:
                                        pass
                        fout.write(json.dumps(row, ensure_ascii=False) + '\n')
    
    def count_lines(self, file_path):
        """统计文件行数。"""
        count = 0
        with open(file_path, 'r', encoding='utf-8') as f:
            for _ in f:
                count += 1
        return count

    def read_jsonl(self, file_path):
        """读取 JSONL 文件返回行列表。"""
        items = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            items.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except FileNotFoundError:
            pass
        return items

    def compute_gift_diamond(self, session_dir):
        """计算礼物总抖币。"""
        items = self.read_jsonl(os.path.join(session_dir, 'gift.jsonl'))
        return sum(int(item.get('diamond_total', 0)) for item in items)

    def compute_total_pv(self, session_dir):
        """从 stats.jsonl 获取总观看。"""
        items = self.read_jsonl(os.path.join(session_dir, 'stats.jsonl'))
        if not items:
            return None
        last = items[-1]
        pv = last.get('total_pv')
        if pv is None:
            return None
        if isinstance(pv, str):
            pv = pv.replace('万', '')
            try:
                return int(float(pv) * 10000)
            except ValueError:
                return pv
        return pv

    def compute_rankings(self, session_dir, available_types):
        """计算各类型排行榜。"""
        rankings = {}

        if 'chat' in available_types:
            items = self.read_jsonl(os.path.join(session_dir, 'chat.jsonl'))
            if items:
                user_count = defaultdict(int)
                at_user_count = defaultdict(int)
                import re
                for item in items:
                    name = item.get('user_name', '')
                    user_count[name] += 1
                    ats = re.findall(r'@[\w\u4e00-\u9fa5]+', item.get('content', ''))
                    if ats:
                        at_user_count[name] += len(ats)
                top_chat = sorted(user_count.items(), key=lambda x: x[1], reverse=True)[:6]
                rankings['chat'] = {
                    'top_users': [{'name': n, 'count': c} for n, c in top_chat]
                }
                if at_user_count:
                    top_at = sorted(at_user_count.items(), key=lambda x: x[1], reverse=True)[:6]
                    rankings['chat']['top_at'] = [{'name': n, 'count': c} for n, c in top_at]

        if 'gift' in available_types:
            items = self.read_jsonl(os.path.join(session_dir, 'gift.jsonl'))
            if items:
                user_diamond = defaultdict(int)
                user_max_gift = {}
                for item in items:
                    name = item.get('user_name', '')
                    d = int(item.get('diamond_total', 0))
                    user_diamond[name] += d
                    if name not in user_max_gift or d > int(user_max_gift[name].get('diamond_total', 0)):
                        user_max_gift[name] = item
                top_gift = sorted(user_diamond.items(), key=lambda x: x[1], reverse=True)[:6]
                top_users = []
                for n, d in top_gift:
                    entry = {'name': n, 'diamond': d}
                    if n in user_max_gift:
                        mg = user_max_gift[n]
                        entry['max_gift'] = mg.get('gift_name', '')
                        entry['max_gift_diamond'] = int(mg.get('diamond_total', 0))
                    top_users.append(entry)
                rankings['gift'] = {
                    'top_users': top_users,
                    'total_diamond': sum(user_diamond.values())
                }

        if 'like' in available_types:
            items = self.read_jsonl(os.path.join(session_dir, 'like.jsonl'))
            if items:
                user_like = defaultdict(int)
                for item in items:
                    user_like[item.get('user_name', '')] += int(item.get('count', 0))
                top_like = sorted(user_like.items(), key=lambda x: x[1], reverse=True)[:6]
                rankings['like'] = {
                    'top_users': [{'name': n, 'count': c} for n, c in top_like],
                    'total_likes': sum(user_like.values())
                }

        if 'lucky_bag' in available_types:
            items = self.read_jsonl(os.path.join(session_dir, 'lucky_bag.jsonl'))
            if items:
                user_count = defaultdict(int)
                for item in items:
                    user_count[item.get('user_name', '')] += 1
                top_lb = sorted(user_count.items(), key=lambda x: x[1], reverse=True)[:6]
                rankings['lucky_bag'] = {
                    'top_users': [{'name': n, 'count': c} for n, c in top_lb]
                }

        return rankings if rankings else None


if __name__ == '__main__':
    builder = BarrageBuilder()
    builder.build()
