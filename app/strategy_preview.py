"""Research preview of cached strategy data, without constructing a broker control."""
from datetime import datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from app.control_contracts import ControlError, project_path
from app.module_registry import ModuleRegistry
from app.strategy_instances import preview_revision
from app.strategy_worker import instance_config, selection
from framework.configuration import policy_from_config
from framework.decisions import digest
from framework.strategy_book import quantities
from framework.trading import amount


def preview(root, item, progress, *, clock=None):
    root = Path(root).resolve()
    clock = clock or datetime.now(timezone.utc)
    held_positions = item.get('_held_positions', {})
    revision = preview_revision(root, item, held_positions)
    registry = ModuleRegistry(root)
    cfg = instance_config(registry, item)
    source = registry.control('data', item['source_id'], config=cfg)
    directory = project_path(root, source.directory, under='runs')
    pointer = directory / 'latest.json'
    prepared = item.get('prepared')
    selected = None
    if prepared and prepared.get('path'):
        candidate = project_path(root, prepared['path'], under='runs')
        if candidate.is_relative_to(directory) and candidate.is_file():
            selected = candidate
    if selected is None:
        if not pointer.is_file():
            raise ControlError('此实例尚无匹配的数据，请先在数据中心更新所需数据。')
        if pointer.stat().st_size > 64 * 1024:
            raise ControlError('数据索引超过读取上限，请重新更新数据。')
        selected = project_path(root, json.loads(pointer.read_text(encoding='utf-8'))['path'], under='runs')
    if not selected.is_relative_to(directory) or not selected.is_file():
        raise ControlError('缓存数据不属于当前策略的数据需求，请重新更新数据。')
    if selected.stat().st_size > 32 * 1024 * 1024:
        raise ControlError('缓存数据超过 32 MB 读取上限，请缩小数据需求。')
    loaded = source.load(json.loads(selected.read_text(encoding='utf-8-sig')))
    positions = quantities(held_positions)
    held = tuple(s.asset_id for s in loaded.stocks if s.ticker in positions)
    progress('使用已有数据计算选股，不连接券商…')
    targets = selection(root, item, loaded.stocks, clock.astimezone(ZoneInfo('America/New_York')).date(),
                        held, policy_from_config(cfg), item['_selection_timeout_seconds'])
    budget = amount(item['budget'], True)
    if budget > 1:
        raise ControlError('账户资金比例不能超过 100%。')
    stocks = {s.asset_id: s for s in loaded.stocks}
    dates = sorted({str(s.price_as_of) for s in loaded.stocks})
    return dict(status='complete', message=f'选股预览完成：{len(targets)} 只股票。',
        instance_revision=revision, research_only=True, execution_ready=False,
        created_at=clock.isoformat(), source_id=item['source_id'], snapshot_sha256=digest(loaded.raw),
        data_as_of=dates[-1] if dates else None, data_first_date=dates[0] if dates else None,
        candidate_count=len(loaded.stocks), budget=str(budget),
        account_target_gross=str(sum((amount(t.weight) for t in targets), amount(0)) * budget),
        targets=[dict(ticker=stocks[t.asset_id].ticker, industry=stocks[t.asset_id].industry, score=t.score,
                      weight=t.weight, account_weight=float(amount(t.weight) * budget)) for t in targets])
