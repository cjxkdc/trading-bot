"""Read-only account evidence, separate from the execution ledger and buying power."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import re
from pathlib import Path
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Observation time requires a timezone')
    return result.astimezone(timezone.utc)


def number(value, *, optional=False, positive=False):
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError('Boolean is not a financial value')
    try:
        n = Decimal(str(value))
    except ArithmeticError:
        raise ValueError('Invalid financial value') from None
    if not n.is_finite() or positive and n <= 0:
        raise ValueError('Invalid financial value')
    result = float(n)
    if not math.isfinite(result):
        raise ValueError('Financial value exceeds numeric range')
    return result


def observation_context(item):
    currency = item.get('currency')
    environment = item.get('environment')
    if not isinstance(currency, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{1,11}', currency):
        raise ValueError('An explicit reporting currency is required')
    if environment not in {'SIMULATE','REAL','paper','broker_paper','live'}:
        raise ValueError('An explicit account environment is required')
    zone = item.get('timezone')
    # Compatibility for old observations only. Other currencies need an explicit zone.
    if zone is None and environment == 'SIMULATE' and currency == 'USD':
        zone = 'America/New_York'
    if not isinstance(zone,str):
        raise ValueError('An explicit accounting timezone is required')
    ZoneInfo(zone)
    return currency, environment, zone


class PerformanceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS observations (
                    scope TEXT, as_of TEXT, day TEXT, body TEXT,
                    PRIMARY KEY(scope, as_of));
                CREATE TABLE IF NOT EXISTS cycles (
                    scope TEXT, id TEXT, as_of TEXT, body TEXT, PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS cash_events (
                    scope TEXT, id TEXT, as_of TEXT, kind TEXT, amount TEXT, note TEXT,
                    PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS reviews (scope TEXT PRIMARY KEY, through TEXT);
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def record(self, scope, observation):
        item = deepcopy(observation)
        at = stamp(item['as_of'])
        currency, environment, market_timezone = observation_context(item)
        item['timezone'] = market_timezone
        if not item.get('account_ref') or len(item.get('positions', [])) > 1000:
            raise ValueError('Unsupported accounting observation')
        item['equity'] = number(item['equity'], positive=True)
        for key in ('cash_total', 'cash_available', 'market_value', 'fees'):
            item[key] = number(item.get(key), optional=True)
        seen = set()
        for p in item['positions']:
            if not isinstance(p.get('symbol'), str) or p['symbol'] in seen:
                raise ValueError('Duplicate or invalid accounting position')
            seen.add(p['symbol'])
            if p.get('currency', currency) != currency:
                raise ValueError('Position valuation must use the account reporting currency; explicit FX conversion is required')
            for key in ('quantity', 'market_value', 'cost_price', 'unrealized_pnl',
                        'last_price', 'position_pnl', 'pnl_rate', 'day_pnl'):
                p[key] = number(p.get(key), optional=key != 'quantity')
            # Read-only accounting can represent shorts even where execution cannot.
        item['as_of'] = at.isoformat()
        if isinstance(item.get('benchmark'), dict):
            item['benchmark']['price'] = number(item['benchmark']['price'], positive=True)
        encoded = json.dumps(item, ensure_ascii=False, allow_nan=False)
        with closing(self.connect()) as db, db:
            last = db.execute('SELECT body FROM observations WHERE scope=? ORDER BY as_of DESC LIMIT 1', (scope,)).fetchone()
            if last:
                previous = json.loads(last[0])
                if previous['account_ref'] != item['account_ref'] or observation_context(previous) != observation_context(item):
                    raise ValueError('Account identity, environment, reporting currency or timezone changed; start a separate scope')
            old = db.execute('SELECT body FROM observations WHERE scope=? AND as_of=?', (scope,item['as_of'])).fetchone()
            if old and old[0] != encoded:
                raise ValueError('Conflicting observation timestamp')
            db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)',
                       (scope,item['as_of'],str(at.astimezone(ZoneInfo(market_timezone)).date()),encoded))
        return item

    def positions_history(self, scope, limit=400):
        """Each day's last observation with its priced positions, oldest first."""
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError('Invalid history limit')
        with closing(self.connect()) as db:
            rows = db.execute('''SELECT o.body FROM observations o JOIN
                (SELECT day,MAX(as_of) at FROM observations WHERE scope=? GROUP BY day) d
                ON o.as_of=d.at AND o.scope=? ORDER BY o.as_of DESC LIMIT ?''', (scope, scope, limit)).fetchall()
        items = [json.loads(row[0]) for row in rows]
        return [dict(as_of=item['as_of'], equity=item.get('equity'), currency=item.get('currency'),
                     positions=item.get('positions', [])) for item in reversed(items)]

    def cycle(self, scope, result, *, strategy=None):
        ident = result.get('decision_id')
        if not ident or 'targets' not in result:
            return
        body = {k:result.get(k) for k in ('decision_id','targets','orders','status','message')}
        body.update(strategy=strategy, recorded_at=datetime.now(timezone.utc).isoformat())
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO cycles VALUES(?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET body=excluded.body',
                       (scope,ident,body['recorded_at'],json.dumps(body,ensure_ascii=False,allow_nan=False)))

    def cash_event(self, scope, value):
        if not isinstance(value, dict) or set(value) != {'as_of','kind','amount','note'}:
            raise ValueError('Cash event requires date, kind, amount and note')
        if value['kind'] not in {'deposit','withdrawal','dividend','fee','reset'}:
            raise ValueError('Unknown cash event')
        at, amount = stamp(value['as_of']), number(value['amount'])
        if amount < 0 or not isinstance(value['note'],str) or len(value['note']) > 500 or at > datetime.now(timezone.utc):
            raise ValueError('Invalid cash event')
        ident = uuid4().hex
        with closing(self.connect()) as db, db:
            first,last = db.execute('SELECT MIN(as_of),MAX(as_of) FROM observations WHERE scope=?', (scope,)).fetchone()
            if not first or not first <= at.isoformat() <= last:
                raise ValueError('Cash event must fall within observed history; refresh first')
            db.execute('INSERT INTO cash_events VALUES(?,?,?,?,?,?)',
                       (scope,ident,at.isoformat(),value['kind'],str(amount),value['note']))
            db.execute('DELETE FROM reviews WHERE scope=?',(scope,))
        return ident

    def review_cash(self, scope, through):
        at = stamp(through).isoformat()
        with closing(self.connect()) as db, db:
            last = db.execute('SELECT MAX(as_of) FROM observations WHERE scope=?',(scope,)).fetchone()[0]
            if not last or at != last:
                raise ValueError('Review must match the latest observation; refresh first')
            db.execute('INSERT OR REPLACE INTO reviews VALUES(?,?)',(scope,at))

    def remove_cash_event(self, scope, ident):
        with closing(self.connect()) as db, db:
            if db.execute('DELETE FROM cash_events WHERE scope=? AND id=?',(scope,ident)).rowcount != 1:
                raise ValueError('Cash event not found in this account')
            db.execute('DELETE FROM reviews WHERE scope=?',(scope,))

    def candles(self, scope, interval='1h', limit=96, *, now=None):
        """OHLC from every stored observation, independent of daily return metrics.

        SQL reads just scalar equity and aggregates before returning rows. Account
        scope, resets and real observation gaps are retained; no forward filling.
        """
        from framework.account_candles import KEY_SQL, bounds, validate_period, window_start
        validate_period(interval, limit)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError('Candle clock requires a timezone')
        result = dict(interval=interval, timezone=None, currency=None,
                      basis='account_equity', sampling_only=True, as_of=None, bars=[])
        with closing(self.connect()) as db:
            last_row = db.execute('SELECT body FROM observations WHERE scope=? ORDER BY as_of DESC LIMIT 1', (scope,)).fetchone()
            if last_row is None:
                return result
            latest_item = json.loads(last_row[0])
            latest = latest_item['as_of']
            currency, environment, market_timezone = observation_context(latest_item)
            result.update(as_of=latest, currency=currency, timezone=market_timezone, environment=environment)
            lower = window_start(stamp(latest), interval, limit, market_timezone).isoformat()
            reset = db.execute("SELECT MAX(as_of) FROM cash_events WHERE scope=? AND kind='reset'", (scope,)).fetchone()[0]
            if reset:
                lower = max(lower, reset)
            # SQL fragment comes only from the validated constant whitelist.
            rows = db.execute(f'''
                WITH points AS (
                    SELECT as_of, json_extract(body,'$.equity') AS equity,
                           {KEY_SQL[interval]} AS bucket
                    FROM observations WHERE scope=? AND as_of>=?
                ), grouped AS (
                    SELECT bucket, MIN(as_of) AS first_at, MAX(as_of) AS last_at,
                           MAX(equity) AS high, MIN(equity) AS low, COUNT(*) AS samples
                    FROM points GROUP BY bucket
                )
                SELECT g.first_at,g.last_at,g.high,g.low,g.samples,
                       json_extract(f.body,'$.equity'),json_extract(l.body,'$.equity')
                FROM grouped g
                JOIN observations f ON f.scope=? AND f.as_of=g.first_at
                JOIN observations l ON l.scope=? AND l.as_of=g.last_at
                ORDER BY g.first_at
            ''', (scope,lower,scope,scope)).fetchall()
        for first,last,high,low,samples,opened,closed in rows:
            start,end = bounds(stamp(first), interval, market_timezone)
            values = [number(v, positive=True) for v in (opened,high,low,closed)]
            if not values[2] <= min(values[0],values[3]) <= max(values[0],values[3]) <= values[1]:
                raise ValueError('Invalid stored equity candle')
            result['bars'].append(dict(start=start.isoformat(), end=end.isoformat(),
                open=values[0], high=values[1], low=values[2], close=values[3],
                sample_count=samples, first_observation=first, last_observation=last,
                in_progress=now.astimezone(timezone.utc) < end))
        return result

    def report(self, scope):
        with closing(self.connect()) as db:
            rows = db.execute('''SELECT o.body FROM observations o JOIN
                (SELECT day,MAX(as_of) at FROM observations WHERE scope=? GROUP BY day) d
                ON o.as_of=d.at AND o.scope=? ORDER BY o.as_of DESC''',(scope,scope)).fetchall()
            first = db.execute('SELECT body FROM observations WHERE scope=? ORDER BY as_of LIMIT 1',(scope,)).fetchone()
            cycles = [json.loads(r[0]) for r in db.execute('SELECT body FROM cycles WHERE scope=? ORDER BY as_of DESC LIMIT 12',(scope,))]
            flows = [dict(id=i,as_of=t,kind=k,amount=float(a),note=n) for i,t,k,a,n in db.execute(
                'SELECT id,as_of,kind,amount,note FROM cash_events WHERE scope=? ORDER BY as_of',(scope,))]
            review = db.execute('SELECT through FROM reviews WHERE scope=?',(scope,)).fetchone()
            review_point = db.execute('SELECT body FROM observations WHERE scope=? AND as_of=?',
                                      (scope, review[0])).fetchone() if review else None
            resets = [f['as_of'] for f in flows if f['kind']=='reset']
            if resets:
                first = db.execute('SELECT body FROM observations WHERE scope=? AND as_of>=? ORDER BY as_of LIMIT 1',
                                   (scope, max(resets))).fetchone()
        items = list(reversed([json.loads(r[0]) for r in rows]))
        if first and not any(r['as_of'] == json.loads(first[0])['as_of'] for r in items):
            items.append(json.loads(first[0]))
            items.sort(key=lambda r: r['as_of'])
        if not items:
            return dict(latest=None,curve=[],cycles=cycles,cash_events=flows,metrics={},flow_reviewed_through=None)
        # Resets are new measurement periods, not investment returns.
        if resets:
            items = [r for r in items if r['as_of'] >= max(resets)]
        if not items:
            return dict(latest=None,curve=[],cycles=cycles,cash_events=flows,metrics={},flow_reviewed_through=None)
        latest, base = items[-1],items[0]
        currency, environment, market_timezone = observation_context(latest)
        reviewed = bool(review and review[0] >= latest['as_of'])
        nav, peak, curve, max_dd = 1.,1.,[],0.
        for i,item in enumerate(items):
            if i:
                prev=items[i-1]
                t0,t1=stamp(prev['as_of']),stamp(item['as_of'])
                span=(t1-t0).total_seconds()
                external=[f for f in flows if prev['as_of'] < f['as_of'] <= item['as_of'] and f['kind'] in {'deposit','withdrawal'}]
                total=sum(f['amount']*(1 if f['kind']=='deposit' else -1) for f in external)
                weighted=sum(f['amount']*(1 if f['kind']=='deposit' else -1)*(t1-stamp(f['as_of'])).total_seconds()/span for f in external)
                denominator=prev['equity']+weighted
                if denominator <= 0:
                    reviewed=False
                else:
                    nav *= 1+(item['equity']-prev['equity']-total)/denominator
                    if nav <= 0 or not math.isfinite(nav):
                        reviewed=False
            peak=max(peak,nav)
            max_dd=max(max_dd,1-nav/peak)
            curve.append(dict(as_of=item['as_of'],equity=item['equity'],index=nav,
                              benchmark=item.get('benchmark')))
        # A completed close often arrives on the following day. Match by session date,
        # never compare today's account to yesterday's benchmark without saying so.
        benchmarks = {b['session_day']: b for item in items
                      if isinstance((b := item.get('benchmark')), dict)}
        bench=[]
        for p in curve:
            b=benchmarks.get(str(stamp(p['as_of']).astimezone(ZoneInfo(market_timezone)).date()))
            p['benchmark'] = b
            if isinstance(b,dict) and b.get('price') is not None and b.get('session_day')==str(stamp(p['as_of']).astimezone(ZoneInfo(market_timezone)).date()) and (b.get('currency', 'USD' if market_timezone == 'America/New_York' and currency == 'USD' else None) == currency) and (stamp(b['completed_at']) <= stamp(p['as_of']) if b.get('completed_at') else market_timezone == 'America/New_York' and currency == 'USD' and stamp(p['as_of']).astimezone(ZoneInfo(market_timezone)).hour >= 16):
                bench.append((p,b))
        benchmark_return=None
        benchmark_period=None
        if len(bench)>1 and len({b['symbol'] for _,b in bench})==1:
            benchmark_return=bench[-1][1]['price']/bench[0][1]['price']-1
            benchmark_period=dict(symbol=bench[0][1]['symbol'], start=bench[0][1]['session_day'],
                                  end=bench[-1][1]['session_day'], return_type='price_only',
                                  account_return=bench[-1][0]['index']/bench[0][0]['index']-1 if reviewed and bench[0][0]['index']>0 else None)
        last_target=next((c for c in cycles if c['status']=='complete'), None)
        targets={t.get('ticker',t.get('asset_id')):t['weight'] for t in (last_target or {}).get('targets',[])}
        positions=[]
        for symbol in sorted(set(targets)|{p['symbol'] for p in latest['positions']}):
            row=next((dict(p) for p in latest['positions'] if p['symbol']==symbol),dict(symbol=symbol,quantity=0,market_value=0))
            row['weight']=None if row.get('market_value') is None else row['market_value']/latest['equity']
            row['target_weight']=targets.get(symbol,0) if last_target else None
            row['drift']=None if row['weight'] is None or row['target_weight'] is None else row['weight']-row['target_weight']
            positions.append(row)
        from framework.performance_analytics import period_reports, position_summary
        analytic_items = {item['as_of']: item for item in items}
        # Preserve the exact reviewed point even after a later sample replaces
        # that day's close. An unreviewed update must not erase verified returns.
        if review_point and review[0] >= base['as_of']:
            analytic_items[review[0]] = json.loads(review_point[0])
        analytics = period_reports(sorted(analytic_items.values(), key=lambda item: item['as_of']),
                                   flows, review[0] if review else None, market_timezone)
        metrics=dict(asset_change=latest['equity']/base['equity']-1,return_rate=nav-1 if reviewed else None,
                     max_drawdown=max_dd if reviewed else None,benchmark_return=benchmark_return, benchmark_period=benchmark_period,
                     return_method='Daily linked Modified Dietz; user-reviewed cash flows',flow_review_required=not reviewed)
        for p in curve:
            if not reviewed:
                p['index']=None
        return dict(currency=currency,timezone=market_timezone,environment=environment,
                    latest=latest,curve=curve,cycles=cycles,positions=positions,cash_events=flows,
                    analytics=analytics, position_summary=position_summary(latest['positions']),
                    metrics=metrics,flow_reviewed_through=review[0] if review else None)
