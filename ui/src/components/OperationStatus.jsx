import React from 'react';

const labels = { status: '检查账户连接', refresh: '更新数据', preview: '预览选股', prepare: '准备订单',
  data: '查看数据模块', broker: '查看账户模块', strategy: '查看策略' };

export function OperationStatus({ operation, actions, busy, onNext }) {
  const next = operation?.next_action;
  const navigation = ['data', 'broker', 'strategy'].includes(next);
  const available = navigation || actions?.[next]?.enabled;
  return <div>
    <strong>{operation?.enabled ? '可以执行一轮组合' : '执行条件待满足'}</strong>
    <p role="status">{operation?.message || '正在读取执行条件…'}</p>
    {next && labels[next] && available && <button className="text-link" disabled={busy}
      onClick={() => onNext(next)}>{labels[next]} →</button>}
  </div>;
}
