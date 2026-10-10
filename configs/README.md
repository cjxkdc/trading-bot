# 配置索引

| 文件 | 用途 |
|---|---|
| `portfolio_config.json` | 主策略、仓位限制、费用和 QC 回测参数 |
| `broker_profiles.json` | Portal 可选券商实例，引用同目录的券商配置 |
| `data_sources.json` | Portal 数据源实例及配置、缓存路径 |
| `*_paper_config.json` | 各券商模拟盘配置 |
| `*_live_config.json` | 独立实盘配置；Portal 不启用 |
| `mom8.json`、`momentum_*.json` | 策略实验配置 |
| `massive.json` | 统一 Massive 供应商的请求预算、缓存时间及 SIC 映射；股票范围与字段由策略声明 |
| `massive_*.json` | 旧采集器兼容配置，Portal 不再拆分成多个 Massive |
| `futu_technology.json` | 当前 Futu 科技股采集器参数 |
| `paper_config.json` | 主配置的兼容别名 |

`extends` 相对当前配置文件解析。`config_file`、`config_dir`、`state_dir`、`source_config` 和 `cache_dir` 仍相对项目根目录解析，不能随配置文件一起改成相对本目录。

示例（在项目根目录运行）：

```text
python bot.py config --config configs/mom8.json
python bot.py broker-status --config configs/futu_paper_config.json
```

第二条默认只作离线检查。实际账户连接设置与密钥仍在 `secrets/`；运行账本仍在 `runs/`。
