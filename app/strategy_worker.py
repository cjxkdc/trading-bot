"""Managed paper batches. Plugin selection is isolated; execution reuses the ledger."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.automation import save
from app.control_contracts import ControlError, SnapshotInput, project_path
from app.module_registry import ModuleRegistry
from app.strategy_instances import managed_guard, instance_binding
from framework.configuration import fees_from_config, policy_from_config
from framework.decisions import Decision, digest, canonical
from framework.order_capabilities import US_EQUITY_LIMIT_ORDERS
from framework.portfolio import StockTarget
from framework.runtime import PortfolioRunner
from framework.strategy_book import reconcile_ownership, owner_plan, settle_ownership
from framework.trading import Account, Quote, amount

NY = ZoneInfo('America/New_York')
SCHEMA = 'managed_strategy_signals_v1'


def instance_config(registry, item):
    cfg = registry.configuration(item['broker_id'])
    cfg['strategy'] = deepcopy(item['strategy'])
    registry.validate_strategy(cfg)
    registry.compatible(cfg, item['source_id'])
    return cfg


def selection(root, item, stocks, day, held, limits, timeout):
    request = dict(strategy=item['strategy'], stocks=[asdict(s) for s in stocks], day=str(day),
                   held_ids=list(held), limits={'max_gross': float(limits.max_gross), 'max_weight': float(limits.max_weight)})
    payload = json.dumps(canonical(request), ensure_ascii=True, allow_nan=False)
    if len(payload.encode()) > 32 * 1024 * 1024:
        raise ControlError('策略计算输入超过 32 MB，请缩小所需股票范围。')
    from app.launcher import external_dll_search, external_environment
    python = Path(root) / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    if not python.is_file():
        python = Path(sys.executable)
    with external_dll_search():
        process = subprocess.Popen([str(python), '-u', '-m', 'app.strategy_select'], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=external_environment(), text=True, encoding='utf-8', errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        output, _ = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()  # This process cannot submit broker orders.
        process.communicate()
        raise ControlError(f'策略 {item["name"]} 计算超过 {timeout} 秒，已停止计算。') from None
    try:
        raw = json.loads(output.strip().splitlines()[-1])
        if process.returncode or raw.get('strategy_protocol') != 1 or raw.get('error'):
            raise ValueError()
        targets = tuple(StockTarget(**value) for value in raw['targets'])
        known = {s.asset_id for s in stocks}
        if len({t.asset_id for t in targets}) != len(targets) or any(t.asset_id not in known for t in targets):
            raise ValueError()
        if any(not 0 < amount(t.weight) <= limits.max_weight + Decimal('0.000000000001') for t in targets) or sum((amount(t.weight) for t in targets), amount(0)) > limits.max_gross + Decimal('0.000000000001'):
            raise ValueError()
        return targets
    except (ValueError, KeyError, TypeError, IndexError):
        raise ControlError(f'策略 {item["name"]} 未返回有效组合，请检查日志和参数。') from None


class ManagedSource:
    def __init__(self, root, registry, cycle):
        self.root, self.registry, self.cycle = Path(root), registry, cycle
        self.parts, self.rows = {}, ()
        self.labels = {}

    def load(self, signal):
        if signal.get('schema') != SCHEMA or signal.get('cycle_id') != self.cycle['id']:
            raise ControlError('合并信号不属于当前账户批次。')
        parts, rows, labels = {}, {}, {}
        for component in signal['components']:
            item = next(i for i in self.cycle['instances'] if i['id'] == component['id'])
            if item['id'] not in self.cycle['due_ids']:
                raise ControlError('合并信号包含未到期的实例。')
            cfg = instance_config(self.registry, item)
            source = self.registry.control('data', item['source_id'], config=cfg)
            path = project_path(self.root, component['path'], under='runs')
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ControlError('数据快照超过读取限制。')
            raw = json.loads(path.read_text(encoding='utf-8-sig'))
            if digest(raw) != component['sha256']:
                raise ControlError('策略数据快照已变化，请重新准备。')
            loaded = source.load(raw)
            if loaded.order_requirements != US_EQUITY_LIMIT_ORDERS:
                raise ControlError('共享账户目前支持美股、美元、整股 DAY 限价单。')
            converted = tuple(replace(s, asset_id='MANAGED:US:' + s.ticker) for s in loaded.stocks)
            parts[item['id']] = dict(source=source, raw=raw, stocks=converted)
            for stock in converted:
                rows.setdefault(stock.asset_id, stock)
                labels[stock.asset_id] = {'ticker': stock.ticker, 'industry': stock.industry}
        if set(parts) != set(self.cycle['due_ids']):
            raise ControlError('合并信号缺少到期的策略。')
        self.parts, self.rows = parts, tuple(rows.values())
        self.labels.update(labels)
        return SnapshotInput(SCHEMA, True, signal, self.rows,
                             {'candidate_count': len(rows), 'strategy_instances': len(parts)}, self.labels,
                             order_requirements=US_EQUITY_LIMIT_ORDERS)

    def paper_signal(self, signal, clock):
        self.load(signal)
        rows, warnings = {}, []
        for ident, part in self.parts.items():
            stocks, notes = part['source'].paper_signal(part['raw'], clock)
            converted = tuple(replace(s, asset_id='MANAGED:US:' + s.ticker) for s in stocks)
            part['stocks'] = converted
            rows.update({s.asset_id: s for s in converted})
            warnings.extend(notes)
        self.rows = tuple(rows.values())
        return self.rows, list(dict.fromkeys(warnings))


class ManagedRunner(PortfolioRunner):
    allow_cash_target = True

    def __init__(self, root, source, cycle, cfg, *, account=None, frozen_quotes=None, selector=selection):
        self.root, self.source, self.cycle, self.selector = Path(root), source, cycle, selector
        self.account, self.frozen_quotes = account, frozen_quotes
        self.cache, self.planned, self.unmanaged, self.component_targets = {}, {}, {}, {}
        policy, fees = policy_from_config(cfg), fees_from_config(cfg)
        self.policy, self.fees, self.input_schema = policy, fees, SCHEMA
        self.engine = SimpleNamespace(max_gross=float(policy.max_gross), max_weight=float(policy.max_weight))

    def plan(self, snapshot):
        if snapshot.input_schema != SCHEMA or self.account is None or snapshot.execution_time.tzinfo is None:
            raise ControlError('共享账户上下文尚未取得。')
        book = self.cycle['book']
        if book.get('account_ref') and book['account_ref'] != digest(self.account.account_id):
            raise ControlError('当前模拟账户与原持仓归属账本不同。')
        observed = {s: str(q) for s, q in self.account.positions}
        self.unmanaged = reconcile_ownership(book['owners'], observed, book.get('unmanaged'))
        if self.account.currency != 'USD':
            raise ControlError('共享策略账户须为美元模拟账户。')
        reverse = {t: a for a, t in snapshot.instruments.items()}
        day = snapshot.execution_time.astimezone(NY).date()
        targets_by_owner, scores = {}, {}
        for item in self.cycle['instances']:
            if item['id'] not in self.cycle['due_ids']:
                continue
            stocks = self.source.parts[item['id']]['stocks']
            held = tuple('MANAGED:US:' + ticker for ticker in book['owners'].get(item['id'], {}))
            key = digest({'strategy': item['strategy'], 'stocks': [asdict(s) for s in stocks], 'day': str(day), 'held': held})
            if key not in self.cache:
                self.cache[key] = self.selector(self.root, item, stocks, day, held, self.policy,
                                                self.cycle['limits']['selection_timeout_seconds'])
            targets = self.cache[key]
            targets_by_owner[item['id']] = {reverse_id(snapshot.instruments, t.asset_id): t.weight for t in targets}
            scores.update({t.asset_id: t.score for t in targets})
        self.component_targets = targets_by_owner
        # Preliminary planning exists only to request the complete set of quotes.
        if not snapshot.quotes:
            combined = {}
            for item in self.cycle['instances']:
                for symbol, weight in targets_by_owner.get(item['id'], {}).items():
                    asset = reverse[symbol]
                    combined[asset] = combined.get(asset, 0.) + float(item['budget']) * weight
            ts = tuple(StockTarget(a, w, scores.get(a, 0.)) for a, w in sorted(combined.items()) if w)
            return Decision('preliminary', ts, {})
        if self.frozen_quotes is None:
            self.frozen_quotes = dict(snapshot.quotes)
        quotes = self.frozen_quotes
        equity = self.account.cash + sum((q * quotes[reverse[s]].mark for s, q in self.account.positions), amount(0))
        asks = {ticker: quotes[asset].ask for ticker, asset in reverse.items() if asset in quotes}
        self.planned, desired = owner_plan(book['owners'], self.unmanaged, targets_by_owner,
            {i['id']: i['budget'] for i in self.cycle['instances']}, equity, asks)
        combined = {reverse[t]: q for t, q in desired.items()}
        weights = {a: float(amount(q) * quotes[a].ask / equity) for a, q in combined.items()}
        if sum(weights.values()) > float(self.policy.max_gross) + 1e-12 or any(w > float(self.policy.max_weight) + 1e-12 for w in weights.values()):
            raise ControlError('合并目标超过账户仓位限制；保留持仓未被自动卖出，请调整资金分配。')
        ts = tuple(StockTarget(a, w, scores.get(a, 0.)) for a, w in sorted(weights.items()))
        record = dict(schema=1, input_schema=SCHEMA, execution_day=str(day), cycle_id=self.cycle['id'],
                      instances=[identity_for_decision(i) for i in self.cycle['instances']],
                      targets=[asdict(t) for t in ts], desired_positions=combined,
                      owner_positions=self.planned, unmanaged=self.unmanaged,
                      input_sha256={ident: digest(part.get('raw', [asdict(s) for s in part['stocks']]))
                                    for ident, part in self.source.parts.items()},
                      limits={'max_gross': float(self.policy.max_gross), 'max_weight': float(self.policy.max_weight)},
                      data_sha256=digest([asdict(s) for s in sorted(self.source.rows, key=lambda s: s.asset_id)]))
        return Decision(digest(record), ts, record)


def identity_for_decision(item):
    return {k: item[k] for k in ('id', 'strategy', 'budget', 'generation')}


def reverse_id(mapping, asset):
    return mapping[asset]


class CaptureControl:
    def __init__(self, delegate, runner):
        self.delegate, self.runner = delegate, runner

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def observe_account(self):
        account = self.delegate.observe_account()
        self.runner.account = account
        for ticker, quantity in account.positions:
            self.runner.source.labels.setdefault('HELD:US:' + ticker, {'ticker': ticker, 'industry': '保留持仓'})
        return account


def check(root, item, progress):
    registry = ModuleRegistry(root)
    cfg = instance_config(registry, item)
    control = registry.control('broker', item['broker_id'])
    report = control.inspect(connect=True)
    if report.get('status') != 'connected' or report.get('issues'):
        raise ControlError('模拟账户检查未通过，请检查 OpenD 和模拟账户。')
    source = registry.control('data', item['source_id'], config=cfg)
    if not source.configuration_status().get('configured'):
        raise ControlError('请先配置所选数据源。')
    required = registry.data_requirements(cfg)
    source.inspect(price_bars=max(required.price_bars, required.volume_bars), progress=progress)
    return {'status': 'complete', 'account_ref': digest(control.observe_account().account_id),
            'message': '模拟账户和数据接口检查通过。'}


def refresh(root, item, progress):
    registry = ModuleRegistry(root)
    cfg = instance_config(registry, item)
    source = registry.control('data', item['source_id'], config=cfg)
    required = registry.data_requirements(cfg)
    path = source.collect(source.directory / ('managed-' + uuid4().hex[:16]),
                          price_bars=max(required.price_bars, required.volume_bars), progress=progress)
    path = project_path(root, path, under='runs')
    save(source.directory / 'latest.json', {'path': str(path)})
    return {'status': 'complete', 'message': '策略所需数据已更新。', 'snapshot_path': str(path)}


def run_cycle(root, cycle_path, progress):
    root = Path(root)
    path = project_path(root, cycle_path, under='runs/strategy_manager/cycles')
    cycle = json.loads(path.read_text(encoding='utf-8'))
    managed_guard(root, cycle)
    registry = ModuleRegistry(root)
    cfg = registry.configuration(cycle['broker_id'])
    components, collected = [], {}
    for item in cycle['instances']:
        if item['id'] not in cycle['due_ids']:
            continue
        configured = instance_config(registry, item)
        source = registry.control('data', item['source_id'], config=configured)
        requirements = registry.data_requirements(configured)
        key = (item['source_id'], requirements.fingerprint)
        if key not in collected:
            managed_guard(root, cycle)
            prepared = item.get('prepared')
            if prepared and prepared.get('day') == str(datetime.now(timezone.utc).astimezone(NY).date()):
                selected = project_path(root, prepared['path'], under='runs')
            else:
                progress(f'采集 {item["name"]} 所需数据…')
                target = source.directory / ('managed-' + uuid4().hex[:16])
                selected = source.collect(target, price_bars=max(requirements.price_bars, requirements.volume_bars), progress=progress)
            selected = project_path(root, selected, under='runs')
            raw = json.loads(selected.read_text(encoding='utf-8-sig'))
            collected[key] = (str(selected), digest(raw))
        selected, sha = collected[key]
        components.append({'id': item['id'], 'path': selected, 'sha256': sha})
    signal = {'schema': SCHEMA, 'cycle_id': cycle['id'], 'components': components}
    signal_path = path.with_name(cycle['id'] + '-signal.json')
    save(signal_path, signal)
    source = ManagedSource(root, registry, cycle)
    loaded = source.load(signal)
    runner = ManagedRunner(root, source, cycle, cfg)
    control = CaptureControl(registry.control('broker', cycle['broker_id']), runner)
    from app.paper_preparation import prepare, execute_prepared
    context = digest({'cycle_id': cycle['id'], 'bindings': cycle['bindings'], 'code': cycle['code_revision']})
    managed_guard(root, cycle)
    preview = prepare(root=root, source=source, loaded=loaded, runner=runner, control=control,
                      path=signal_path, context=context, clock=datetime.now(timezone.utc), progress=progress)
    cycle.update(phase='prepared', signal_path=str(signal_path), planned=runner.planned,
                 unmanaged=runner.unmanaged, preview_path=preview.get('snapshot_path'),
                 preview_token=preview.get('preview_token'), context=context)
    save(path, cycle)
    if preview.get('no_orders'):
        result = preview
    else:
        managed_guard(root, cycle)
        cycle['execution_attempted'] = True
        cycle['phase'] = 'executing'
        save(path, cycle)
        raw = json.loads(Path(preview['snapshot_path']).read_text(encoding='utf-8'))
        progress('按合并后的账户差额提交一批模拟订单，并核对成交…')
        result = execute_prepared(root=root, raw=raw, context=context, token=preview['preview_token'],
            source=source, runner=runner, control=control, cfg=cfg, clock=datetime.now(timezone.utc),
            before_submit=lambda: managed_guard(root, cycle))
    final_account = control.observe_account()
    observed = {s: str(q) for s, q in final_account.positions}
    owners = settle_ownership(runner.planned, cycle['book']['owners'], runner.unmanaged, observed, cycle['due_ids'])
    book = {'owners': owners, 'unmanaged': runner.unmanaged, 'account_ref': digest(final_account.account_id)}
    cycle.update(phase='complete', completed_at=datetime.now(timezone.utc).isoformat(), book=book)
    save(path, cycle)
    return dict(result, status='complete', message='合并模拟交易完成，订单和各策略持仓归属已核对。', book=book,
                instance_targets=runner.component_targets, owner_positions=owners)


def reconcile(root, cycle_path, progress, *, close=False):
    root = Path(root)
    path = project_path(root, cycle_path, under='runs/strategy_manager/cycles')
    cycle = json.loads(path.read_text(encoding='utf-8'))
    registry = ModuleRegistry(root)
    control = registry.control('broker', cycle['broker_id'])
    cfg = registry.configuration(cycle['broker_id'])
    from app.paper_recovery import reconcile_orders, close_unsubmitted
    if close:
        close_unsubmitted(control=control, policy=policy_from_config(cfg), fees=fees_from_config(cfg))
    report = reconcile_orders(control=control, policy=policy_from_config(cfg), fees=fees_from_config(cfg))
    if report.get('recovery_state') != 'complete':
        return report
    if not cycle.get('planned'):
        return {'status': 'complete', 'book': cycle['book'], 'message': '未提交订单，已关闭中断的准备批次。'}
    account = control.observe_account()
    if cycle['book'].get('account_ref') and cycle['book']['account_ref'] != digest(account.account_id):
        raise ControlError('模拟账户已变化，无法核对原持仓归属。')
    owners = settle_ownership(cycle['planned'], cycle['book']['owners'], cycle['unmanaged'],
                             {s: str(q) for s, q in account.positions}, cycle['due_ids'], partial=close)
    book = {'owners': owners, 'unmanaged': cycle['unmanaged'], 'account_ref': digest(account.account_id)}
    cycle.update(phase='complete', book=book)
    save(path, cycle)
    return {'status': 'complete', 'book': book, 'message': '已核对成交与持仓归属，实例保持停止。' if not close else
            '未提交余单已结束，归属按已确认实际持仓更新；不代表组合已达到目标。'}


def dispatch(root, action, *, instance=None, cycle_path=None, progress=lambda message: None):
    try:
        if action == 'strategy-check':
            return check(root, instance, progress)
        if action == 'strategy-preview':
            from app.strategy_preview import preview
            return preview(root, instance, progress)
        if action == 'strategy-refresh':
            return refresh(root, instance, progress)
        if action == 'strategy-cycle':
            return run_cycle(root, cycle_path, progress)
        if action == 'strategy-reconcile':
            return reconcile(root, cycle_path, progress)
        if action == 'strategy-resolve':
            return reconcile(root, cycle_path, progress, close=True)
        raise ControlError('未知策略运行操作。')
    except ControlError as exc:
        return {'status': 'blocked' if action in {'strategy-check', 'strategy-preview'} else 'paused', 'message': str(exc)}
    except Exception:
        if action in {'strategy-check', 'strategy-preview'}:
            return {'status': 'blocked', 'message': '本次检查未完成，请检查连接、数据和日志后重试。'}
        return {'status': 'paused', 'message': '策略账户批次未完成，请检查运行记录并核对订单与持仓归属。'}


def main():
    try:
        request = json.loads(sys.stdin.read())
        if set(request) - {'action', 'instance', 'cycle_path'}:
            raise ValueError()
        action = request.pop('action')
        def progress(message):
            print(json.dumps({'portal_progress': 1, 'message': str(message)[:800]}, ensure_ascii=True), flush=True)
        result = dispatch(Path(__file__).resolve().parents[1], action, progress=progress, **request)
        print(json.dumps(dict(result, portal_protocol=1, action=action), ensure_ascii=True, allow_nan=False), flush=True)
    except Exception:
        print(json.dumps({'portal_protocol': 1, 'action': 'strategy-cycle', 'status': 'paused',
                          'message': '策略任务未返回可核对结果，请先核对订单。'}), flush=True)


if __name__ == '__main__':
    main()
