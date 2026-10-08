"""Account configuration lifecycle, independent of broker-specific fields."""
from app.control_contracts import ControlError, project_path
from framework.journal_reader import JournalReader, JournalReadError


def configure_broker(service, value):
    service.idle()
    if (not isinstance(value, dict) or set(value) != {'broker', 'revision', 'values'}
            or value['broker'] != service.broker_id or not isinstance(value['values'], dict)):
        raise ControlError('当前账户模块已变化，请刷新连接设置后重试。')
    descriptor = service.registry.descriptor('broker', service.broker_id)
    if not descriptor['enabled'] or 'configure' not in descriptor['capabilities']:
        raise ControlError('此模块未提供网页连接设置。')
    control = service.registry.control('broker', service.broker_id)
    if value['revision'] != control.config_revision():
        raise ControlError('连接配置已被其他页面或程序修改，请刷新后重新编辑。')
    fields = {field['name'] for field in descriptor['fields']}
    if set(value['values']) - fields:
        raise ControlError('提交的设置不属于当前券商模块。')
    if service.automation.corrupt or service.strategy_instances.corrupt:
        raise ControlError('自动计划或实例状态无法核对，暂不能修改账户连接。')
    if (service.automation.data['enabled'] or any(item['enabled'] for item in service.strategy_instances.data['instances'])
            or service.strategy_instances.data['pending']):
        raise ControlError('请先暂停自动计划和运行中的策略实例，并核对未完成批次，再修改连接设置。')
    service.switch_issues()
    # Profiles may share one private connection directory. Inspect every owner,
    # so a second profile cannot bypass an unresolved account ledger.
    folder = control.profile.get('config_dir')
    if folder:
        directory = project_path(service.root, folder)
        for ident, profile in service.registry.profiles('broker').items():
            if not profile.get('config_dir') or project_path(service.root, profile['config_dir']) != directory:
                continue
            other = service.registry.control('broker', ident)
            try:
                if JournalReader(other.journal_path).has_unresolved():
                    raise ControlError('共用此连接的账户仍有未完成委托，请先核对订单。')
            except JournalReadError:
                raise ControlError('共用连接的账户账本无法核对，原配置未修改。') from None
            identity_fields = {field['name'] for field in descriptor['fields'] if field.get('secret')}
            if other.state_dir != control.state_dir and other.journal_path.exists() and any(
                    value['values'].get(name) for name in identity_fields):
                raise ControlError('共用连接已有其他账户账本；请保留账户 ID，只修改连接端口。')
    result = control.configure(value['values'])
    if result.get('changed', True):
        service.disconnect_authorization()
        service.monitor.last_check = service.monitor.last_success = service.monitor.error = None
        service.monitor.next_observation = 0
    return dict(result, settings_revision=control.config_revision())
