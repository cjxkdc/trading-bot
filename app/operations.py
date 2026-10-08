"""The same built-in operations are used by the service, workers and CLI."""
ACTIONS = frozenset({'offline', 'status', 'observe', 'preview', 'prepare', 'execute', 'refresh', 'data-status', 'configure-data', 'calendar', 'reconcile', 'recover', 'resolve-remainder', 'close-terminal'})
# Composite service jobs are deliberately not valid worker commands.
WORKBENCH_ACTIONS = ACTIONS | {'preflight'}
BROKER_CHECKS = frozenset({'offline', 'status', 'observe', 'calendar', 'reconcile', 'resolve-remainder', 'close-terminal'})
DATA_CAPABILITIES = {'configure-data': 'configure', 'data-status': 'inspect', 'refresh': 'collect', 'preview': 'preview'}


def context_for(registry, action, broker_id, source_id):
    if action in BROKER_CHECKS:
        return registry.connection_context(broker_id)
    if action == 'configure-data':
        return registry.source_context(source_id)
    return registry.context(broker_id, source_id)
