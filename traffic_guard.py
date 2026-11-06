# -*- coding: utf-8 -*-
"""流量护栏：按日统计出流量，超阈值自动限流，防止按流量计费被刷爆。

集成进 server.py 的方式：
1. 在每个响应里调用 track(bytes)
2. 启动时 load_state()
3. 超阈值时 reject() 返回 429
"""
import json
import os
import threading
import time

STATE = os.environ.get('TRAFFIC_STATE') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data', 'traffic.json')

# 阈值（字节）
DAILY_WARN = 200 * 1024 * 1024      # 200 MB：开始记录警告
DAILY_LIMIT = 500 * 1024 * 1024     # 500 MB：开始限流
DAILY_HARD = 1024 * 1024 * 1024     # 1 GB：直接拒绝非白名单


class TrafficGuard:
    def __init__(self):
        self.lock = threading.Lock()
        self.day = time.strftime('%Y-%m-%d')
        self.total = 0
        self.count = 0
        self.hits = {}          # ip -> bytes
        self.blocked = False
        self._load()

    def _load(self):
        try:
            with open(STATE, encoding='utf-8') as f:
                d = json.load(f)
            if d.get('day') == self.day:
                self.total = d.get('total', 0)
                self.count = d.get('count', 0)
                self.hits = d.get('hits', {})
                self.blocked = d.get('blocked', False)
        except Exception:
            pass

    def _save(self):
        try:
            with open(STATE, 'w', encoding='utf-8') as f:
                json.dump({'day': self.day, 'total': self.total,
                           'count': self.count, 'hits': self.hits,
                           'blocked': self.blocked}, f)
        except Exception:
            pass

    def _roll(self):
        today = time.strftime('%Y-%m-%d')
        if today != self.day:
            self.day = today
            self.total = 0
            self.count = 0
            self.hits = {}
            self.blocked = False

    def allow(self, ip):
        """请求进来先问一句：这个 IP 还被允许吗？"""
        with self.lock:
            self._roll()
            if self.blocked:
                return False
            # 单 IP 超过 100 MB 直接封
            if self.hits.get(ip, 0) > 100 * 1024 * 1024:
                return False
            return True

    def track(self, nbytes, ip=''):
        """响应发出去后记账。"""
        with self.lock:
            self._roll()
            self.total += nbytes
            self.count += 1
            if ip:
                self.hits[ip] = self.hits.get(ip, 0) + nbytes
            # 进入限流态
            if self.total >= DAILY_HARD:
                self.blocked = True
            # 每 50 次写一次盘，减少 IO
            if self.count % 50 == 0 or nbytes > 100000:
                self._save()

    def status(self):
        with self.lock:
            self._roll()
            pct = self.total / DAILY_LIMIT * 100 if DAILY_LIMIT else 0
            return {
                'day': self.day,
                'mb': round(self.total / 1024 / 1024, 2),
                'count': self.count,
                'limit_mb': DAILY_LIMIT // 1024 // 1024,
                'pct': round(pct, 1),
                'blocked': self.blocked,
                'cost_yuan': round(self.total / 1024 / 1024 / 1024 * 0.8, 3),
                'top_ips': sorted(self.hits.items(), key=lambda x: -x[1])[:5],
            }

    def reset(self):
        with self.lock:
            self.total = 0
            self.count = 0
            self.hits = {}
            self.blocked = False
            self._save()


guard = TrafficGuard()
