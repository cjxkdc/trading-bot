"""Project-side operations for the desktop launcher; Futu simulation only.

Runs with the project's venv, not the launcher's bundled Python. This keeps
strategy/broker code replaceable without bundling credentials or account state.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import sys
from math import isfinite
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.assembly import (build_portfolio_runner, build_snapshot_collector, collection_config, inspect_broker,
                      open_broker_paper_session, open_paper_session, platform_catalog, strategy_data_requirements)
from framework.configuration import load_config, normalize_config
from framework.decisions import digest
from framework.execution import ExecutionBlocked
from framework.portfolio import StockSnapshot
from framework.trading import fresh
from data.snapshot import demo_snapshot, parse_snapshot
from data.common import write_json
from data.massive_technology import SCHEMA as CURRENT_SCHEMA
from data.massive_provider import SCHEMA as REQUEST_SCHEMA
from data.futu_technology import SCHEMA as PREVIEW_SCHEMA
from platforms.opend import OpenDError, settings

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ('offline', 'status', 'preview', 'execute', 'demo', 'refresh', 'data-status', 'configure-data')
PREVIEW_SCHEMAS = {PREVIEW_SCHEMA, CURRENT_SCHEMA, REQUEST_SCHEMA}


def current_data_preview(cfg, raw, path):
    """Current-data screen, explicitly outside the historical execution contract."""
    if raw.get('schema') not in PREVIEW_SCHEMAS or raw.get('execution_allowed') is not False:
        raise ValueError('不是有效的研究数据文件。')
    runner = build_portfolio_runner(cfg)
    if raw.get('schema') == REQUEST_SCHEMA:
        from data.request_control import MassiveProviderControl
        from app.portal_worker import research_preview
        control = MassiveProviderControl(Path(path).parent, {}, {}).for_request(strategy_data_requirements(cfg['strategy']))
        return research_preview(cfg, runner, control.load(raw), path)
    if getattr(runner.engine.strategy, 'requires_context', False):
        raise ValueError('当前数据预览不支持需要账户持仓的策略。')
    stocks = tuple(StockSnapshot(s['asset_id'], s['ticker'],
                   datetime.fromisoformat(s['observed_at']).date(),
                   datetime.fromisoformat(s['price_as_of']).date(), s['industry'],
                   float(s['operating_income_ttm']), tuple(s['adjusted_closes']), tuple(s['dollar_volumes']))
                   for s in raw['stocks'])
    # No backdated known_on and no PortfolioRunner.execute/decision token.
    targets = tuple(runner.engine.strategy.select(stocks))
    by_id = {s['asset_id']: s for s in raw['stocks']}
    if (len(by_id) != len(stocks) or len({t.asset_id for t in targets}) != len(targets)
            or any(t.asset_id not in by_id or not isfinite(t.weight) or not isfinite(t.score)
                   or not 0 < t.weight <= runner.engine.max_weight + 1e-12 for t in targets)
            or sum(t.weight for t in targets) > runner.engine.max_gross + 1e-12):
        raise ValueError('策略目标没有通过仓位检查。')
    return {'status': 'previewed', 'message': f"真实数据预览：有效 {len(stocks)} / 候选 {raw['candidate_count']}；仅供研究。",
            'execution_ready': False, 'snapshot_path': str(path),
            'targets': [{'asset_id': t.asset_id, 'ticker': by_id[t.asset_id]['ticker'],
                         'weight': t.weight, 'score': t.score,
                         'industry': by_id[t.asset_id]['industry_name']} for t in targets],
            'issues': raw['limitations'],
            'details': {'provider': raw.get('provider', 'futu'), 'source_universe': raw['universe_id'],
                        'candidate_count': raw['candidate_count'], 'usable_count': len(stocks),
                        'excluded_count': len(raw['excluded']), 'data_issue_count': len(raw['issues']),
                        'collection_complete': raw['collection_complete'],
                        'price_as_of': raw['price_as_of'], 'collected_at': raw['finished_at'],
                        'strategy': cfg['strategy']['type'], 'execution_allowed': False}}


def paper_config(root):
    cfg = normalize_config(load_config(root / 'configs/futu_paper_config.json'))
    broker = cfg['broker']
    if (broker.get('type') != 'futu_paper'
            or set(broker) != {'type', 'config_dir', 'state_dir'}
            or (root / broker['config_dir']).resolve() != (root / 'secrets/futu').resolve()
            or (root / broker['state_dir']).resolve() != (root / 'runs/futu-paper').resolve()):
        raise ValueError('启动器只允许 futu_paper、secrets/futu 和原有 runs/futu-paper 账本。')
    if cfg['data'] != {'type': 'snapshot_file'}:
        raise ValueError('当前启动器需要 snapshot_file 数据接口。')
    return cfg


def bound_account(root):
    connection = settings('futu', root / 'secrets/futu', environment='SIMULATE')
    if not connection.account_id or not connection.login_user_id:
        raise ValueError('请先绑定 Futu 模拟账户和登录 ID，再连接或执行。')


def execution_issues(raw, snapshot, decision, policy, now):
    """Early usability checks; the executor repeats its full checks at submission."""
    issues = []
    if (raw.get('synthetic', False) is not False
            or any(str(s.ticker).startswith('DEMO-') or str(s.asset_id).startswith('DEMO-')
                   for s in snapshot.stocks)):
        issues.append('合成演示数据不能提交到 Futu。')
    if not snapshot.stocks:
        issues.append('股票池为空，请提供完整科技股数据。')
    if snapshot.execution_time.astimezone(ZoneInfo('America/New_York')).date() != now.astimezone(ZoneInfo('America/New_York')).date():
        issues.append('快照不是当前美股交易日的数据。')
    if not snapshot.quotes or any(t.asset_id not in snapshot.quotes for t in decision.targets):
        issues.append('缺少目标股票的买卖报价。')
    try:
        for quote in snapshot.quotes.values():
            fresh(quote.as_of, now, policy.max_quote_age_seconds)
            if quote.bid > quote.ask:
                raise ValueError('crossed quote')
    except ValueError:
        issues.append('报价已过期、来自未来或买卖价格无效；请更新后重新预览。')
    return issues


def snapshot_plan(root, cfg, path, now):
    if path is None:
        raise ValueError('请先选择行情快照 JSON。')
    # One read: validation, strategy and fingerprint all see identical input.
    raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(raw, dict):
        raise ValueError('行情快照必须是 JSON 对象。')
    snapshot = parse_snapshot(raw)
    runner = build_portfolio_runner(cfg)
    decision = runner.plan(snapshot)
    token = digest({'configuration': cfg, 'snapshot': raw, 'decision': decision.record})
    issues = execution_issues(raw, snapshot, decision, runner.policy, now)
    return runner, snapshot, decision, token, issues


def perform(action, *, root=ROOT, snapshot_path=None, preview_token=None, now=None, progress=None,
            data_source='futu', credentials=None):
    if action not in ACTIONS:
        raise ValueError('Unknown launcher action')
    root = Path(root).resolve()
    cfg = paper_config(root)
    now = now or datetime.now(timezone.utc)
    if action == 'configure-data':
        from data.massive import save_settings, settings as massive_settings
        if data_source != 'massive' or not isinstance(credentials, dict):
            raise ValueError('本地密钥配置仅适用于 Massive。')
        folder = root / 'secrets/massive'
        key = credentials.get('api_key', '').strip()
        if not key:
            key = massive_settings(folder).api_key
        save_settings(folder, key, credentials.get('requests_per_minute', 5))
        return {'status': 'ready', 'message': 'Massive 密钥已保存到本机；请点击「检查数据权限」。',
                'details': {'provider': 'massive', 'network_requested': False}}
    if action == 'data-status':
        spec = collection_config(root, data_source)
        if data_source != 'massive':
            raise ValueError('Futu 数据权限请通过现有 market-status 命令检查。')
        if progress:
            progress('按策略需要的数据字段检查 Massive 权限…')
        collector = build_snapshot_collector(spec)
        if platform_catalog()[spec['type']].get('control', {}).get('request_driven'):
            collector.for_request(strategy_data_requirements(cfg['strategy']))
        requirements = build_portfolio_runner(cfg).engine.strategy.history_requirements
        result = collector.inspect(price_bars=max(requirements.price_bars, requirements.dollar_volume_bars))
        return {'status': 'connected', 'message': '当前策略所需的数据接口可用，可以开始按需下载。',
                'details': result}
    if action == 'refresh':
        spec = collection_config(root, data_source)
        collector = build_snapshot_collector(spec)
        directory = Path(spec['cache_dir']).parent
        if platform_catalog()[spec['type']].get('control', {}).get('request_driven'):
            request = strategy_data_requirements(cfg['strategy'])
            collector.for_request(request)
            directory = directory / 'requests' / request.fingerprint
        out = directory / (now.strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8])
        requirements = build_portfolio_runner(cfg).engine.strategy.history_requirements
        path = collector.collect(out, price_bars=max(requirements.price_bars, requirements.dollar_volume_bars), progress=progress)
        raw = json.loads(path.read_text(encoding='utf-8'))
        result = current_data_preview(cfg, raw, path)
        write_json(directory / 'latest.json', {'path': str(path)})
        return result
    if action in {'offline', 'status'}:
        if action == 'status':
            bound_account(root)
        result = inspect_broker('futu_paper', root / 'secrets/futu', connect=action == 'status')
        if result.get('account_environment') != 'SIMULATE':
            raise ValueError('模拟账户环境校验未通过。')
        ok = not result.get('issues')
        connected = ok and result.get('status') == 'connected'
        return {'status': 'connected' if connected else ('ready' if ok else 'blocked'),
                'message': ('Futu 模拟账户已连接（只读检查）。' if connected else
                            '本地配置检查完成；尚未连接账户。' if ok else '本地配置未就绪。'),
                'strategy': cfg['strategy']['type'], 'details': result}
    if action == 'demo':
        # Always a separate local account, never a broker-hosted order.
        local_cfg = deepcopy(cfg)
        local_cfg['broker'] = {'type': 'paper'}
        out = root / 'runs/launcher/demos' / (now.strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8])
        out.mkdir(parents=True, exist_ok=False)
        raw = demo_snapshot()
        (out / 'synthetic-snapshot.json').write_text(json.dumps(raw, indent=2), encoding='utf-8')
        snapshot = parse_snapshot(raw)
        runner = build_portfolio_runner(local_cfg)
        decision = runner.plan(snapshot)
        with open_paper_session(local_cfg, snapshot, out / 'state', runner.fees) as (broker, journal):
            result = runner.execute(decision, snapshot, broker, journal)
        return {'status': 'complete', 'message': '离线演示完成：合成数据、本地虚拟资金。',
                'details': {'synthetic': True, 'network_requested': False, 'orders': result['orders'],
                            'output_directory': str(out)}, 'targets': decision.record['targets']}
    if action == 'preview' and snapshot_path is not None:
        raw = json.loads(Path(snapshot_path).read_text(encoding='utf-8-sig'))
        if isinstance(raw, dict) and raw.get('schema') in PREVIEW_SCHEMAS:
            return current_data_preview(cfg, raw, snapshot_path)
    runner, snapshot, decision, token, issues = snapshot_plan(root, cfg, snapshot_path, now)
    if action == 'preview':
        tickers = {s.asset_id: s.ticker for s in snapshot.stocks}
        return {'status': 'previewed', 'message': '策略预览完成；尚未发送订单。',
                'preview_token': token, 'execution_ready': not issues, 'issues': issues,
                'targets': [dict(t, ticker=tickers[t['asset_id']]) for t in decision.record['targets']],
                'details': {'execution_day': decision.record['execution_day'],
                            'candidates': len(snapshot.stocks), 'strategy': cfg['strategy']['type'],
                            'gross_weight': sum(t.weight for t in decision.targets)}}
    if not preview_token or token != preview_token:
        raise ValueError('数据、策略或配置已变化，请重新预览后再执行。')
    if issues:
        raise ValueError(' '.join(issues))
    bound_account(root)
    # No live session is imported or dispatched by this entry point.
    with open_broker_paper_session(cfg, snapshot, root / 'runs/futu-paper', runner.fees) as (broker, journal):
        if broker.mode != 'broker_paper':
            raise ValueError('启动器拒绝非模拟券商。')
        result = runner.execute(decision, snapshot, broker, journal)
    return {'status': 'complete', 'message': '本轮模拟交易已完成；没有启动持续调度。',
            'details': {key: value for key, value in result.items() if key != 'account'}}


def dispatch(action, **kwargs):
    """Only sanitized reports leave the worker; never forward SDK tracebacks."""
    try:
        result = perform(action, **kwargs)
    except (ExecutionBlocked, OpenDError, ValueError) as exc:
        result = {'status': 'paused' if action == 'execute' else 'blocked', 'message': str(exc)}
    except (OSError, KeyError, TypeError) as exc:
        result = {'status': 'paused' if action == 'execute' else 'blocked',
                  'message': f'文件或数据格式无法读取（{type(exc).__name__}）。请检查配置和快照。'}
    except Exception as exc:
        result = {'status': 'paused' if action == 'execute' else 'blocked',
                  'message': f'操作中断（{type(exc).__name__}）。请保留账本并核对状态。'}
    return dict(result, launcher_protocol=1, action=action, automatic_retry=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--preview-token')
    parser.add_argument('--data-source', choices=('futu', 'massive'), default='futu')
    args = parser.parse_args()
    def progress(message):
        print(json.dumps({'launcher_progress': 1, 'message': message}, ensure_ascii=False), flush=True)
    credentials = None
    if args.action == 'configure-data':
        try:
            credentials = json.loads(sys.stdin.read(4096))
        except ValueError:
            credentials = None
    result = dispatch(args.action, snapshot_path=args.snapshot, preview_token=args.preview_token,
                      progress=progress, data_source=args.data_source, credentials=credentials)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 2 if result['status'] in {'blocked', 'paused'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
