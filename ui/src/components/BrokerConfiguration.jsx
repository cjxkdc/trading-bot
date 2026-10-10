import React, { useEffect, useState } from 'react';
import { api } from '../api';
import { Button } from '../ui';
import { ModuleFields } from './ModuleFields';
import { brokerChoices, marketLabel, settingsPayload, settingsValues } from '../broker_settings';
import { executionContext, currencyLabel, timezoneLabel } from '../execution_context';

export function BrokerConfiguration({ state, busy, act, chooseBroker }) {
  const broker = state.broker, context = executionContext(broker);
  const { environments, markets } = brokerChoices(state.brokers, broker);
  const [values, setValues] = useState(() => settingsValues(broker));
  const [revision, setRevision] = useState(broker.settings_revision);
  const [dirty, setDirty] = useState(false);
  const running = state.automation?.enabled || state.strategy_manager?.instances?.some(item => item.enabled);
  const locked = busy || running;
  const fields = (broker.fields || []).map(field => ({ ...field,
    placeholder: field.secret && broker.settings?.configured_fields?.includes(field.name) ? '已保存 · 留空保留' : field.placeholder }));

  useEffect(() => {
    if (!dirty && revision !== broker.settings_revision) {
      setValues(settingsValues(broker)); setRevision(broker.settings_revision);
    }
  }, [broker.settings_revision, dirty]);

  function reset() { setValues(settingsValues(broker)); setRevision(broker.settings_revision); setDirty(false); }
  async function save(event) {
    event.preventDefault();
    const result = await act(() => api('broker/settings', settingsPayload(broker, values, revision)));
    if (result) {
      setValues(current => Object.fromEntries(fields.map(field => [field.name, field.secret ? '' : current[field.name]])));
      setRevision(result.settings_revision || broker.settings_revision);
      setDirty(false);
    }
  }
  return <div className="broker-configuration">
    <div className="form-grid">
      <label>账户环境<select aria-label="账户环境" value={broker.id} disabled={locked || dirty || environments.length < 2} onChange={event => chooseBroker(event.target.value)}>{(environments.length ? environments : [broker]).map(item => <option key={item.id} value={item.id}>{item.mode_label} · {item.name}</option>)}</select></label>
      <label>交易市场<select aria-label="交易市场" value={broker.id} disabled={locked || dirty || markets.length < 2} onChange={event => chooseBroker(event.target.value)}>{(markets.length ? markets : [broker]).map(item => <option key={item.id} value={item.id}>{marketLabel(item)}</option>)}</select></label>
    </div>
    <div className="info-grid"><div><span>结算币种</span><strong>{currencyLabel(context.currency)}</strong></div><div><span>市场时区</span><strong>{timezoneLabel(context.timezone)}</strong></div><div><span>交易市场来源</span><strong>所选账户模块</strong></div></div>
    <p className="small muted">币种和时区随市场自动匹配。{markets.length < 2 ? '当前账户模块只提供上述市场；其他市场需接入对应适配器。' : '选择市场会切换到该市场对应的账户配置。'}</p>
    {broker.capabilities.includes('configure') && <details className="settings" open><summary>连接设置</summary>
      {broker.settings?.issue && <p className="inline-warning">{broker.settings.issue}</p>}
      {running && <p className="inline-warning">请先暂停自动运行，再修改连接设置。</p>}
      <form onSubmit={save}><div className="form-grid"><ModuleFields fields={fields} values={values} disabled={locked} onChange={(key, value) => { setValues(current => ({ ...current, [key]: value })); setDirty(true); }}/></div>
        <div className="card-actions"><Button primary type="submit" disabled={locked || !dirty}>保存连接设置</Button><Button type="button" disabled={busy || !dirty} onClick={reset}>重置修改</Button><span className="small muted">保存在运行工作台的机器上；保存后重新检查连接。</span></div>
      </form>
    </details>}
  </div>;
}
