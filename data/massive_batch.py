"""Vendor batch-price transport and split-only reconstruction, no strategy rules."""
from collections import defaultdict
from datetime import date, datetime, timezone

from data.common import DataIssue, finite
from data.massive import SPLITS, MassiveError
from data.massive_technology import NY, aligned_prices
from framework.collection import PriceHistory


def grouped_rows(saved, day, wanted):
    """Validate date/count/uniqueness before using the whole-market response."""
    raw = saved['data']
    rows = raw.get('results')
    if (raw.get('adjusted') is not False or not isinstance(rows, list)
            or raw.get('next_url') or type(raw.get('resultsCount')) is not int
            or raw['resultsCount'] != len(rows)):
        raise DataIssue('invalid_grouped_response')
    found, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise DataIssue('invalid_grouped_row')
        symbol = row.get('T')
        # Other security types include vendor tickers with lowercase suffixes.
        if not isinstance(symbol, str) or not symbol or len(symbol) > 40 or symbol in seen:
            raise DataIssue('duplicate_or_invalid_grouped_ticker')
        seen.add(symbol)
        if symbol not in wanted and symbol != 'SPY':
            continue
        stamp = finite(row['t'])
        observed_day = datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(NY).date()
        prices = [finite(row[k]) for k in ('o', 'h', 'l', 'c')]
        volume = finite(row['v'])
        if (observed_day != day or row.get('otc', False) is not False or volume < 0
                or any(v <= 0 for v in prices) or prices[1] < max(prices[0], prices[3])
                or prices[2] > min(prices[0], prices[3]) or prices[2] > prices[1]):
            raise DataIssue('invalid_grouped_date_price_or_volume')
        found[symbol] = (prices[3], volume)
    if 'SPY' not in found:
        raise DataIssue('grouped_reference_missing')
    return found


def split_events(api, first, end):
    saved = api.pages(SPLITS, {'execution_date.gte': str(first), 'execution_date.lte': str(end),
                             'sort': 'execution_date.asc', 'limit': 5000})
    events, seen, identities, ambiguous = defaultdict(list), set(), set(), set()
    for row in saved['results']:
        symbol, ident = row.get('ticker'), row.get('id')
        day = date.fromisoformat(row['execution_date'])
        old, new = finite(row['split_from']), finite(row['split_to'])
        if (not isinstance(symbol, str) or not symbol or not isinstance(ident, str) or not ident
                or ident in identities or not first <= day <= end
                or old <= 0 or new <= 0):
            raise DataIssue('invalid_or_duplicate_split_event')
        ratio = finite(new / old)
        if ratio <= 0:
            raise DataIssue('invalid_split_ratio')
        if (symbol, day) in seen:
            # Legitimate same-day forward/reverse or stock-dividend events can
            # coexist. Their composition is not guessed; use vendor adjusted
            # history for the affected listed symbols only.
            ambiguous.add(symbol)
        seen.add((symbol, day))
        identities.add(ident)
        # Do not use historical_adjustment_factor: it can include splits AFTER
        # the signal date. Apply only the original ratio inside this window.
        events[symbol].append((day, ratio))
    for rows in events.values():
        rows.sort(reverse=True)
    return events, saved['observed_at'], ambiguous


def market_histories(api, sessions, symbols, request, now, notify):
    splits, split_observed, ambiguous = split_events(api, sessions[0], sessions[-1])
    histories = {s: {} for s in symbols}
    observations = [split_observed]
    wanted = set(symbols)
    today = now.astimezone(NY).date()
    for index, day in enumerate(sessions, 1):
        api.checkpoint()
        # Recent bars may be revised. Older raw bars survive a rolling window
        # and future splits, but are revalidated after 90 days rather than forever.
        ttl = 86400 if (today - day).days <= 7 else 90 * 86400
        saved = api.grouped_daily(str(day), cache_seconds=ttl)
        rows = grouped_rows(saved, day, wanted)
        observations.append(saved['observed_at'])
        for symbol in wanted.intersection(rows):
            histories[symbol][day] = rows[symbol]
        notify(f'批量日线 {index}/{len(sessions)} · {day} · 请求 {api.requests} · 缓存 {api.hits}')
    usable, excluded, issues = {}, [], []
    for symbol, rows in histories.items():
        api.checkpoint()
        if len(rows) != len(sessions):
            # Insufficient history is not filled or used. A gap inside a listed
            # series is flagged separately from a series that starts later.
            if rows and min(rows) == sessions[0]:
                issues.append({'ticker': symbol, 'reason': 'incomplete_daily_history'})
            else:
                excluded.append({'ticker': symbol, 'reason': 'listing_or_price_history_too_short'})
            continue
        prices, shares, dollars = [], [], []
        events = iter(() if symbol in ambiguous else splits.get(symbol, ()))
        event = next(events, None)
        factor = 1.0
        for day in reversed(sessions):
            while event is not None and event[0] > day:
                factor = finite(factor * event[1])
                event = next(events, None)
            close, volume = rows[day]
            prices.append(finite(close / factor))
            shares.append(finite(volume * factor))
            dollars.append(finite(close * volume))
        prices, shares, dollars = list(reversed(prices)), list(reversed(shares)), list(reversed(dollars))
        if symbol in ambiguous:
            notify(f'复核同日多次拆股 · {symbol}')
            try:
                adjusted = api.daily(symbol, str(sessions[0]), str(sessions[-1]))
                raw = {'data': {'results': [bar_from_history(day, *rows[day]) for day in sessions]}}
                prices, raw_prices, volumes, _ = aligned_prices(adjusted, raw, sessions, sessions[0], sessions[-1])
            except MassiveError as exc:
                if exc.code != 'not_found': raise
                issues.append({'ticker': symbol, 'reason': exc.code})
                continue
            except (DataIssue, ValueError, KeyError, TypeError, OverflowError) as exc:
                issues.append({'ticker': symbol, 'reason': str(exc) if isinstance(exc, DataIssue)
                               else 'invalid_split_fallback_history'})
                continue
            scale = prices[-1] / raw_prices[-1]
            shares = [finite(v*r/p*scale) for v,r,p in zip(volumes, raw_prices, prices)]
            dollars = [finite(p*v) for p,v in zip(raw_prices, volumes)]
            observations.append(adjusted['observed_at'])
        if any(p <= 0 for p in prices) or any(v < 0 for v in shares):
            raise DataIssue('invalid_reconstructed_history')
        if (set(request.fields) & {'dollar_volumes', 'share_volumes', 'raw_close'}
                and any(v <= 0 for v in dollars[-20:])):
            issues.append({'ticker': symbol, 'reason': 'nonpositive_dollar_volume'})
            continue
        usable[symbol] = PriceHistory(symbol, sessions[-1], tuple(prices[-request.price_bars:]),
                                      rows[sessions[-1]][0], tuple(shares[-request.volume_bars:]),
                                      tuple(dollars[-request.volume_bars:]))
    return usable, max(observations), excluded, issues


def bar_from_history(day, close, volume):
    return {'t': datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()*1000 + 12*3600000,
            'c': close, 'v': volume}
