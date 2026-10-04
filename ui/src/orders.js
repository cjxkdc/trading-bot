const statuses = {
  abandoned: '已结束（未提交）', filled: '已成交', planned: '待提交',
  accepted: '等待成交', pending: '等待成交', partial: '部分成交',
  unknown: '待核对', cancelled: '已撤单', rejected: '已拒绝',
};

export const orderStatus = value => statuses[String(value).toLowerCase()] || value || '—';
export const orderSide = value => ({ buy: '买入', sell: '卖出' })[String(value).toLowerCase()] || value || '—';
