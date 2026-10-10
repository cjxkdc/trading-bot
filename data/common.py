"""Provider-independent data errors, atomic files and collection locks."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json, math, os, time
from pathlib import Path
from uuid import uuid4


class DataIssue(ValueError):
    """Public, bounded data-quality reason; never raw SDK error output."""


# Machine-readable reason codes stay in issue lists; the user sees these texts.
ISSUE_REASONS = {
    'invalid_grouped_response': 'Massive 批量日线响应格式无效（已清除该日缓存，可重新下载）。',
    'invalid_grouped_row': 'Massive 批量日线包含无效记录（已清除该日缓存，可重新下载）。',
    'grouped_reference_missing': 'Massive 批量日线缺少参考标的 SPY 的有效记录，无法核对交易日。',
    'duplicate_or_invalid_grouped_ticker': '批量日线中该股票的记录重复或代码无效。',
    'invalid_grouped_date_price_or_volume': '该股票当日的开高低收或成交量不自洽，已跳过该股票。',
    'invalid_daily_bar': '该股票有一天的日线数据不自洽，本轮已跳过它。',
    'incomplete_daily_history': '该股票在区间内缺少部分交易日，已跳过。',
    'listing_or_price_history_too_short': '该股票上市时间或价格历史不足策略所需长度。',
    'invalid_reconstructed_history': '拆股复权后的价格或成交量出现非正值，无法使用。',
    'invalid_or_duplicate_split_event': 'Massive 拆股事件记录无效或重复。',
    'invalid_split_ratio': 'Massive 拆股比例无效。',
    'invalid_split_fallback_history': '同日多次拆股的复核日线无法对齐。',
    'nonpositive_dollar_volume': '最近 20 日出现零成交量交易日。',
    'preflight_reference_identity_mismatch': 'Massive 参考股票 AAPL 的资料与请求不符，可能是接口变更。',
    'missing_sic_classification': 'Massive 没有返回 SIC 行业代码，可能是套餐不含参考数据（Reference）权限。',
    'missing_market_cap': 'Massive 没有返回市值，可能是套餐不含参考数据权限。',
    'invalid_or_duplicate_reference_ticker': 'Massive 股票列表包含无效或重复的代码。',
    'empty_reference_universe': 'Massive 返回的股票池为空，请检查交易所筛选与权限。',
    'ticker_detail_identity_mismatch': 'Massive 返回的股票资料与请求的代码不符。',
    'missing_stable_security_identity': '该股票缺少稳定的证券标识（FIGI）。',
    'duplicate_share_class_identity': '多只股票共用同一证券标识。',
    'invalid_market_cap': '该股票的市值为非正数。',
    'financial_after_cutoff': '财报披露日期晚于价格截止日，已排除以避免前视偏差。',
    'invalid_numeric_value': '数据中出现布尔值而不是数值。',
    'missing_or_nonfinite_value': '数据中出现缺失或非有限数值。',
    'not_found': 'Massive 没有找到该股票的数据。',
    'invalid_strategy_collection_priority': '策略的采集优先级函数返回了无效值。',
    'invalid_strategy_collection_completion': '策略的采集完成判断返回了无效值。',
}


def describe_issue(code):
    """Chinese guidance for a reason code; unknown codes stay visible for diagnosis."""
    if not isinstance(code, str):
        return str(code)
    text = ISSUE_REASONS.get(code)
    if text:
        return f'{text}（{code}）'
    if any('一' <= ch <= '鿿' for ch in code):
        return code  # Already a user-facing sentence.
    return f'数据校验未通过：{code}'



def utcnow():
    return datetime.now(timezone.utc)



def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
        # On Windows an antivirus or indexer can hold the target for a moment;
        # the replace itself stays atomic, so a short retry is safe.
        for attempt in range(6):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)



def finite(value):
    if isinstance(value, bool):
        raise DataIssue('invalid_numeric_value')
    number = float(value)
    if not math.isfinite(number):
        raise DataIssue('missing_or_nonfinite_value')
    return number



@contextmanager
def collection_lock(directory):
    """One collector across CLI and GUI processes; OS releases it after a crash."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'collector.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DataIssue('已有数据采集任务正在运行，请等待完成后再更新。') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
