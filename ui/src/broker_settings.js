import { executionContext } from './execution_context.js';

export function brokerFamily(broker) {
  return broker.configuration_family || broker.type || broker.id;
}

export function marketKey(broker) {
  const context = executionContext(broker);
  return JSON.stringify([context.market, context.asset_class, context.currency, context.timezone]);
}

export function brokerChoices(brokers, selected) {
  const family = brokers.filter(item => item.enabled && brokerFamily(item) === brokerFamily(selected));
  const environments = family.filter(item => marketKey(item) === marketKey(selected));
  const markets = family.filter(item => item.mode === selected.mode);
  return { environments, markets };
}

export function settingsValues(broker) {
  return Object.fromEntries((broker.fields || []).map(field => [field.name,
    field.secret ? '' : broker.settings?.values?.[field.name] ?? field.default ?? '']));
}

export function settingsPayload(broker, values, revision = broker.settings_revision) {
  return { broker: broker.id, revision, values: Object.fromEntries((broker.fields || []).map(field => [field.name, values[field.name]])) };
}

export function marketLabel(broker) {
  const context = executionContext(broker);
  const asset = { equity: '股票', crypto: '加密资产', forex: '外汇', future: '期货', option: '期权' }[context.asset_class] || context.asset_class;
  return context.market ? `${context.market} · ${asset || '资产'}` : '模块未提供市场信息';
}
