// Research belongs to the strategy being viewed, not the global manual selection.
export function researchSources(state, strategyType) {
  if (typeof strategyType !== 'string' || !strategyType) return [];
  const sources = (state?.strategy_manager?.instances || [])
    .filter(item => item?.id && item.strategy?.type === strategyType)
    .map(item => ({
      id: `instance:${item.id}`, kind: 'instance', instance: item,
      label: `运行实例 · ${item.name}`, schedule: item.schedule,
    }));
  if (state?.strategy?.type === strategyType) {
    sources.push({ id: 'manual', kind: 'manual', label: '手动配置', schedule: state.automation?.config });
  }
  return sources;
}

export function resolveResearchSource(state, strategyType, selection) {
  if (selection?.strategyType !== strategyType) return null;
  return researchSources(state, strategyType).find(item => item.id === selection.sourceId) || null;
}

export function reconcileResearchSelection(state, strategyType, selection) {
  if (resolveResearchSource(state, strategyType, selection)) return selection;
  if (selection?.strategyType === strategyType && !selection.sourceId) return selection;
  // Never replace a removed or changed source with another instance or manual config.
  return { strategyType, sourceId: '' };
}

export function researchExperiments(items, strategyType) {
  const matching = [], unassigned = [];
  for (const item of items || []) {
    const owner = item?.configuration?.strategy?.type;
    if (item?.invalid || typeof owner !== 'string' || !owner) {
      unassigned.push(item);
    } else if (strategyType && owner === strategyType) {
      matching.push(item);
    }
  }
  return { matching, unassigned };
}

export function researchResultSelection(experiments, selected) {
  const available = new Set(experiments.flatMap(item => (item.results || [])
    .filter(result => !result.invalid && typeof result.id === 'string').map(result => result.id)));
  return [...new Set(selected)].filter(id => available.has(id));
}
