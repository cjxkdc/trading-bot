"""A short-lived strategy calculation process. Receives data, never broker handles."""
from dataclasses import asdict
from datetime import date
import json
import sys


def calculate(request):
    from app.assembly import build_portfolio_strategy
    from framework.portfolio import PortfolioContext, PortfolioEngine, StockSnapshot, EquitySnapshot
    rows = []
    def restore(value):
        if isinstance(value, dict) and set(value) == {'nonfinite'} and value['nonfinite'] in {'nan', 'inf', '-inf'}:
            return float(value['nonfinite'])
        if isinstance(value, list):
            return [restore(v) for v in value]
        return value
    for value in request['stocks']:
        value = {k: restore(v) for k, v in value.items()}
        value['known_on'] = date.fromisoformat(value['known_on'])
        value['price_as_of'] = date.fromisoformat(value['price_as_of'])
        for key in ('adjusted_closes', 'dollar_volumes', 'share_volumes'):
            if key in value:
                value[key] = tuple(value[key])
        rows.append((EquitySnapshot if 'market_cap' in value else StockSnapshot)(**value))
    day = date.fromisoformat(request['day'])
    context = PortfolioContext(day, tuple(request['held_ids']))
    engine = PortfolioEngine(build_portfolio_strategy(request['strategy']), **request['limits'])
    return [asdict(t) for t in engine.plan(rows, day, context, current_information=True)]


def main():
    # Linux server hard caps; Windows transport also imposes a wall-time limit.
    if sys.platform != 'win32':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    try:
        text = sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
        if len(text) > 32 * 1024 * 1024:
            raise ValueError('Selection input too large')
        result = {'strategy_protocol': 1, 'targets': calculate(json.loads(text))}
    except Exception:
        result = {'strategy_protocol': 1, 'error': '策略计算未完成，请检查参数、数据和运行限制。'}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
