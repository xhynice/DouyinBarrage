#!/usr/bin/env python3
"""弹幕数据构建脚本：将原始 SQLite/CSV 数据转换为前端可用的 JSON 格式。

数据源优先级：SQLite (data.db) > CSV（会话目录）。

使用方式:
    python docs/build_barrage.py

输出结构:
    docs/data/barrage/
    ├── index.json                    # 全局索引
    └── {主播名}/
        ├── index.json                # 直播间索引
        └── {session_id}/
            ├── meta.json             # 会话元数据
            ├── chat.jsonl            # 弹幕数据
            ├── gift.jsonl            # 礼物数据
            └── ...
"""

import os
import re
import json
import csv
import glob
import shutil
from datetime import datetime
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'docs', 'data', 'barrage')


def _resolve_data_dir():
    """从 config.yaml 读取 output_dir，不存在则回退 'data'。"""
    try:
        import yaml
        config_path = os.path.join(SCRIPT_DIR, 'config.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        file_dir = cfg.get('output_dir', 'data')
    except Exception:
        file_dir = 'data'
    if not os.path.isabs(file_dir):
        file_dir = os.path.join(SCRIPT_DIR, file_dir)
    return file_dir


DATA_DIR = _resolve_data_dir()

# 消息类型 → 前端展示配置
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


class BarrageBuilder:
    def __init__(self, data_dir=DATA_DIR, output_dir=OUTPUT_DIR):
        """初始化构建器。

        Args:
            data_dir: 原始数据根目录（含各主播子目录）。
            output_dir: 前端 JSON 输出目录。
        """
        self.data_dir = data_dir
        self.output_dir = output_dir
    
    def build(self):
        """构建所有主播的弹幕数据。"""
        os.makedirs(self.output_dir, exist_ok=True)

        all_live_rooms = []

        for anchor_name in sorted(os.listdir(self.data_dir)):
            anchor_dir = os.path.join(self.data_dir, anchor_name)
            if not os.path.isdir(anchor_dir):
                continue

            print(f"处理主播: {anchor_name}")
            sessions = self.build_anchor_room(anchor_name, anchor_dir)

            if sessions:
                all_live_rooms.append({
                    'live_id': anchor_name,
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
    
    def load_meta(self, anchor_dir):
        """从 meta.json 加载主播元数据。"""
        meta_file = os.path.join(anchor_dir, 'meta.json')
        if os.path.exists(meta_file):
            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return None
    
    def sum_stats(self, sessions):
        """汇总所有会话的统计。"""
        total = defaultdict(int)
        for session in sessions:
            for k, v in session.get('stats', {}).items():
                total[k] += v
        return dict(total)
    
    @staticmethod
    def migrate_csv_dirs(anchor_dir):
        """迁移旧格式 CSV 目录名到 YYYYMMDD_HHMM 格式。"""
        if not os.path.isdir(anchor_dir):
            return 0
        migrated = 0
        # 旧格式带数据库ID: 20260424_1203_7632171016466238259 → 20260424_1203
        pat_old = re.compile(r'^(\d{8})_(\d{4})_\d+$')
        # 新格式带横线: 2026-05-29_11-48-32 → 20260529_1148
        pat_new = re.compile(r'^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-\d{2}$')

        for d in sorted(os.listdir(anchor_dir)):
            src = os.path.join(anchor_dir, d)
            if not os.path.isdir(src):
                continue
            target = None
            m = pat_old.match(d)
            if m:
                target = f"{m.group(1)}_{m.group(2)}"
            else:
                m = pat_new.match(d)
                if m:
                    target = f"{m.group(1)}{m.group(2)}{m.group(3)}_{m.group(4)}{m.group(5)}"
            if target and target != d:
                dst = os.path.join(anchor_dir, target)
                if os.path.exists(dst):
                    for f in os.listdir(src):
                        shutil.move(os.path.join(src, f), os.path.join(dst, f))
                    os.rmdir(src)
                else:
                    os.rename(src, dst)
                migrated += 1
        return migrated

    def build_anchor_room(self, anchor_name, anchor_dir):
        """构建单个主播的数据（仅 CSV）。"""
        output_dir = os.path.join(self.output_dir, anchor_name)
        os.makedirs(output_dir, exist_ok=True)

        meta = self.load_meta(anchor_dir)

        for asset in ['avatar.jpg', 'cover.jpg']:
            src = os.path.join(anchor_dir, asset)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(output_dir, asset))
                print(f"  复制 {asset}")

        # 迁移旧目录名
        migrated = self.migrate_csv_dirs(anchor_dir)
        if migrated:
            print(f"  迁移: {migrated} 个目录重命名")

        csv_sessions = sorted([
            d for d in os.listdir(anchor_dir)
            if os.path.isdir(os.path.join(anchor_dir, d)) and d[0:1].isdigit()
        ])
        if csv_sessions:
            print(f"  数据源: CSV ({len(csv_sessions)} 个会话)")
            sessions = self.build_from_csv(anchor_name, anchor_dir, output_dir)
        else:
            print(f"  跳过: 无 CSV 会话目录")
            sessions = []

        room_index = {
            'anchor_name': anchor_name,
            'sessions': sessions
        }

        if meta:
            room_index['anchor_avatar'] = meta.get('anchor_avatar', '')
            room_index['room_title'] = meta.get('room_title', '')

        index_file = os.path.join(output_dir, 'index.json')
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(room_index, f, ensure_ascii=False, indent=2)

        return sessions

    def build_from_csv(self, anchor_name, anchor_dir, output_dir):
        """从 CSV 会话目录构建数据（SQLite 不存在时的回退）。"""
        sessions = []

        for session_id in sorted(os.listdir(anchor_dir)):
            session_path = os.path.join(anchor_dir, session_id)
            if not os.path.isdir(session_path):
                continue

            files = glob.glob(os.path.join(session_path, '*.csv'))
            if not files:
                continue

            session_data = self.build_session(anchor_name, session_id, files, output_dir)
            if session_data:
                sessions.append(session_data)

        return sessions
    
    def build_session(self, anchor_name, session_id, files, output_dir):
        """构建单个会话数据（CSV 回退路径）。"""
        type_files = defaultdict(list)
        for file_path in files:
            type_name = os.path.basename(file_path).replace('.csv', '')
            if type_name in TYPE_CONFIG:
                type_files[type_name].append(file_path)

        if not type_files:
            return None

        output_session_dir = os.path.join(output_dir, session_id)
        os.makedirs(output_session_dir, exist_ok=True)

        available_types = []
        stats = {}

        for type_name in sorted(type_files.keys()):
            chosen_file = type_files[type_name][0]
            output_file = os.path.join(output_session_dir, f"{type_name}.jsonl")

            self.csv_to_jsonl(chosen_file, output_file)

            available_types.append(type_name)
            stats[type_name] = self.count_lines(output_file)

            print(f"  {session_id}/{type_name}: {stats[type_name]} 条 ({os.path.basename(chosen_file)})")

        meta = {
            'session_id': session_id,
            'anchor_name': anchor_name,
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
    
    @staticmethod
    def csv_to_jsonl(src, dst):
        """将 CSV 文件转换为 JSONL 格式。"""
        with open(src, 'r', encoding='utf-8-sig') as fin:
            with open(dst, 'w', encoding='utf-8') as fout:
                reader = csv.DictReader(fin)
                for row in reader:
                    for key in ('gift_count', 'diamond_total', 'count', 'total',
                                'current', 'total_pv', 'total_user', 'online_anchor',
                                'member_count', 'follow_count'):
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


    def _compute_user_gift_diamonds(self, items, key='user_id'):
        """按用户分组计算礼物抖币，处理连送。返回 {user: {'diamond': int, 'max_gift': item}}"""
        user_items = defaultdict(list)
        for item in items:
            user_items[item.get(key, '')].append(item)
        result = {}
        for uid, uitems in user_items.items():
            uitems.sort(key=lambda x: x.get('time', ''))
            total = current_max = 0
            max_gift_item = None
            for item in uitems:
                gc = int(item.get('gift_count', 0))
                d = int(item.get('diamond_total', 0))
                if gc == 1:
                    total += current_max
                    current_max = d
                else:
                    current_max = max(current_max, d)
                if max_gift_item is None or d > int(max_gift_item.get('diamond_total', 0)):
                    max_gift_item = item
            total += current_max
            result[uid] = {'diamond': total, 'max_gift': max_gift_item}
        return result

    def compute_gift_diamond(self, session_dir):
        """计算礼物总抖币。按 gift_count 识别连送，处理乱序。"""
        items = self.read_jsonl(os.path.join(session_dir, 'gift.jsonl'))
        if not items:
            return 0
        return sum(v['diamond'] for v in self._compute_user_gift_diamonds(items, 'user_id').values())

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
                # 按用户分组
                user_gift_items = defaultdict(list)
                for item in items:
                    user_gift_items[item.get('user_name', '')].append(item)

                user_gift_data = self._compute_user_gift_diamonds(items, 'user_name')

                top_gift = sorted(user_gift_data.items(), key=lambda x: x[1]['diamond'], reverse=True)[:6]
                top_users = []
                for n, data in top_gift:
                    entry = {'name': n, 'diamond': data['diamond']}
                    if data['max_gift']:
                        mg = data['max_gift']
                        entry['max_gift'] = mg.get('gift_name', '')
                        entry['max_gift_diamond'] = int(mg.get('diamond_total', 0))
                    top_users.append(entry)
                rankings['gift'] = {
                    'top_users': top_users,
                    'total_diamond': sum(d['diamond'] for d in user_gift_data.values())
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
