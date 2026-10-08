const statuses = {
  abandoned: '已结束（未提交）', closed: '已关闭（已核对）', filled: '已成交', planned: '待提交',
  accepted: '等待成交', pending: '等待成交', partial: '部分成交',
  unknown: '待核对', cancelled: '已撤单', rejected: '已拒绝',
};
const tones = {
  filled: 'green', accepted: 'blue', pending: 'blue', planned: 'muted', abandoned: 'muted', closed: 'muted',
  partial: 'amber', unknown: 'amber', cancelled: 'red', rejected: 'red',
};
// Orders that still stop automatic execution until reconciled or closed.
const open = new Set(['planned', 'accepted', 'pending', 'partial', 'unknown', 'cancelled', 'rejected']);

export const orderStatus = value => statuses[String(value).toLowerCase()] || value || '—';
export const orderTone = value => tones[String(value).toLowerCase()] || 'muted';
export const orderNeedsAttention = value => open.has(String(value).toLowerCase());
export const orderSide = value => ({ buy: '买入', sell: '卖出' })[String(value).toLowerCase()] || value || '—';
export const orderDay = cycle => typeof cycle === 'string' && /:\d{4}-\d{2}-\d{2}$/.test(cycle) ? cycle.slice(-10) : '—';

export function orderSummary(orders = [], context) {
  const totals = { buy: 0, sell: 0, buyCount: 0, sellCount: 0 };
  for (const order of orders) {
    const multiplier = order.contract_multiplier ?? order.instrument_rules?.contract_multiplier ?? context?.instrument_rules?.contract_multiplier;
    const asNumber = value => value == null || typeof value === 'boolean' || (typeof value === 'string' && !value.trim()) ? NaN : Number(value);
    // A missing multiplier is unknown, not an implicit one-share contract.
    const notional = order.notional != null ? asNumber(order.notional) : multiplier != null
      ? asNumber(order.quantity) * asNumber(order.limit_price) * asNumber(multiplier) : NaN;
    const side = String(order.side).toLowerCase() === 'sell' ? 'sell' : 'buy';
    if (!Number.isFinite(notional) || notional < 0) totals[side] = null;
    else if (totals[side] !== null) {
      totals[side] += notional;
      if (!Number.isFinite(totals[side])) totals[side] = null;
    }
    totals[`${side}Count`] += 1;
  }
  return totals;
}
