from framework.order_capabilities import US_EQUITY_LIMIT_ORDERS, OrderRequirements
"""Data-module control adapters; vendor configuration stays in the data module."""
from datetime import datetime
from hashlib import sha256
import importlib
import json
from pathlib import Path

from app.control_contracts import ControlError, SnapshotInput, project_path
from data.snapshot import parse_snapshot
from framework.portfolio import StockSnapshot


class SnapshotControl:
    def __init__(self, root, profile, entry):
        self.root, self.profile, self.entry = Path(root), dict(profile), entry

    @property
    def directory(self):
        return project_path(self.root, self.profile['directory'], under='runs')

    def configuration_status(self):
        return {'configured': True, 'message': self.entry.get('description', ''), 'network_requested': False}

    def config_revision(self):
        hasher = sha256()
        for field in self.entry['control'].get('revision_paths', []):
            path = project_path(self.root, self.profile[field])
            paths = sorted(path.glob('*')) if path.is_dir() else [path]
            for item in paths:
                if item.is_file():
                    if item.stat().st_size > 1024 * 1024:
                        raise ControlError('数据模块配置超过读取上限。')
                    hasher.update(item.name.encode())
                    hasher.update(item.read_bytes())
        return hasher.hexdigest()

    def order_requirements(self, raw):
        definition = self.entry.get('order_requirements')
        if definition is not None:
            return OrderRequirements(**definition)
        # Explicit compatibility for the original stock schemas only. New modules must declare.
        if self.entry.get('strategy_schema') in {'us_technology_daily_v1','us_equity_momentum_daily_v1'}:
            return US_EQUITY_LIMIT_ORDERS
        raise ControlError('数据模块尚未声明订单要求。')

    def load(self, raw):
        snapshot = parse_snapshot(raw)
        return SnapshotInput(self.entry['strategy_schema'], False, raw, snapshot.stocks,
                             self.metadata(raw), {s.asset_id: {'ticker': s.ticker, 'industry': s.industry}
                                                  for s in snapshot.stocks}, snapshot, order_requirements=self.order_requirements(raw))

    def metadata(self, raw):
        if not isinstance(raw, dict) or not isinstance(raw.get('stocks'), list):
            raise ControlError('数据文件缺少股票列表。')
        if raw.get('input_schema') != self.entry['input_schema']:
            raise ControlError('文件格式与所选数据模块不匹配。')
        return {'price_as_of': raw.get('price_as_of'), 'collected_at': raw.get('execution_time'),
                'candidate_count': len(raw['stocks']), 'usable_count': len(raw['stocks']),
                'collection_complete': raw.get('universe_complete'), 'research_only': False,
                'synthetic': raw.get('synthetic', False), 'limitations': []}


class CurrentResearchControl(SnapshotControl):
    def execution_signal(self, raw, clock):
        # Original providers keep their old method name as a compatibility API.
        return self.paper_signal(raw, clock)

    @property
    def directory(self):
        return project_path(self.root, self.profile['cache_dir'], under='runs').parent

    def collector(self):
        # The collector is declared by this data plug-in, not selected by a Portal branch.
        module, name = self.entry['entrypoint'].split(':')
        paths = self.entry['control'].get('path_fields', [])
        params = {key: str(project_path(self.root, value)) if key in paths else value
                  for key, value in self.profile.items() if key != 'type'}
        return getattr(importlib.import_module(module), name)(**params)

    def collect(self, out, *, price_bars, progress, as_of=None):
        # Only collectors that accept a date are declared with the backfill capability.
        return self.collector().collect(out, price_bars=price_bars, progress=progress,
                                        **({'as_of': as_of} if as_of is not None else {}))

    def metadata(self, raw):
        if (not isinstance(raw, dict) or raw.get('schema') != self.entry['output_schema']
                or raw.get('execution_allowed') is not False or not isinstance(raw.get('stocks'), list)):
            raise ControlError('这份研究快照与所选数据模块的格式不匹配。')
        return {'price_as_of': raw.get('price_as_of'), 'collected_at': raw.get('finished_at'),
                'candidate_count': raw['candidate_count'], 'usable_count': len(raw['stocks']),
                'collection_complete': raw.get('collection_complete'), 'research_only': True,
                'synthetic': raw.get('synthetic', False), 'limitations': raw.get('limitations', [])}

    def load(self, raw):
        meta = self.metadata(raw)
        stocks = tuple(StockSnapshot(s['asset_id'], s['ticker'], datetime.fromisoformat(s['observed_at']).date(),
                        datetime.fromisoformat(s['price_as_of']).date(), s['industry'], float(s['operating_income_ttm']),
                        tuple(s['adjusted_closes']), tuple(s['dollar_volumes'])) for s in raw['stocks'])
        labels = {s['asset_id']: {'ticker': s['ticker'], 'industry': s.get('industry_name', s['industry'])}
                  for s in raw['stocks']}
        return SnapshotInput(self.entry['strategy_schema'], True, raw, stocks, meta, labels, order_requirements=self.order_requirements(raw))


class MassiveControl(CurrentResearchControl):
    def benchmark_observation(self, symbol, *, clock):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        from data.common import collection_lock
        from data.massive import MassiveClient, settings
        from framework.performance import number
        local = clock.astimezone(ZoneInfo('America/New_York'))
        # Use completed prior days. Today's delayed/partial aggregate is never a close.
        end = local.date() - timedelta(days=1)
        cache = project_path(self.root, self.profile['cache_dir'], under='runs')
        with collection_lock(cache):
            client = MassiveClient(settings(project_path(self.root, self.profile['config_dir'])), cache, max_requests=2)
            saved = client.daily(symbol, str(end-timedelta(days=10)), str(end))
        rows = saved['data'].get('results', [])
        if not rows:
            raise ControlError('基准没有可用的已完成日线。')
        row = max(rows, key=lambda r: r['t'])
        day = datetime.fromtimestamp(row['t']/1000, ZoneInfo('America/New_York')).date()
        if day > end:
            raise ControlError('基准日线尚未完成。')
        return dict(symbol=symbol, price=number(row['c'], positive=True), session_day=str(day),
                    source='Massive', currency='USD', return_type='split_adjusted_price', observed_at=saved['observed_at'])

    def configuration_status(self):
        from data.massive import configuration_status
        return configuration_status(project_path(self.root, self.profile['config_dir']))

    def configure(self, values):
        from data.massive import save_settings, settings
        if set(values) - {'api_key', 'requests_per_minute'}:
            raise ControlError('未知连接参数。')
        folder = project_path(self.root, self.profile['config_dir'])
        key = values.get('api_key', '').strip()
        if not key:
            key = settings(folder).api_key
        save_settings(folder, key, values.get('requests_per_minute', 5))
        return {'message': '连接配置已保存到本机；请检查数据权限。'}

    def inspect(self, *, price_bars, progress):
        progress('正在检查历史价格、股票信息和财报权限…')
        return self.collector().inspect(price_bars=price_bars)

    def backfill_profile(self):
        """Figures for estimating a historical collection; missing configuration keeps the defaults."""
        from data.massive import settings
        from data.massive_provider import source_settings
        profile = {'requests_per_minute': 5, 'listing_pages': 6}
        try:
            profile['requests_per_minute'] = settings(project_path(self.root, self.profile['config_dir'])).requests_per_minute
        except Exception:
            pass
        try:
            symbols = source_settings(project_path(self.root, self.profile['source_config']))['max_reference_symbols']
            profile['listing_pages'] = -(-symbols // 1000)
        except Exception:
            pass
        return profile

    def backfill_profile(self):
        """Figures for estimating a historical collection; missing configuration keeps the defaults."""
        from data.massive import settings
        from data.massive_provider import source_settings
        profile = {'requests_per_minute': 5, 'listing_pages': 6}
        try:
            profile['requests_per_minute'] = settings(project_path(self.root, self.profile['config_dir'])).requests_per_minute
        except Exception:
            pass
        try:
            symbols = source_settings(project_path(self.root, self.profile['source_config']))['max_reference_symbols']
            profile['listing_pages'] = -(-symbols // 1000)
        except Exception:
            pass
        return profile

    def backfill_profile(self):
        """Figures for estimating a historical collection; missing configuration keeps the defaults."""
        from data.massive import settings
        from data.massive_provider import source_settings
        profile = {'requests_per_minute': 5, 'listing_pages': 6}
        try:
            profile['requests_per_minute'] = settings(project_path(self.root, self.profile['config_dir'])).requests_per_minute
        except Exception:
            pass
        try:
            symbols = source_settings(project_path(self.root, self.profile['source_config']))['max_reference_symbols']
            profile['listing_pages'] = -(-symbols // 1000)
        except Exception:
            pass
        return profile

    def cancel(self):
        path = project_path(self.root, self.profile['cache_dir'], under='runs') / 'cancel.request'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('user requested stop', encoding='utf-8')
