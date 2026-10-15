# 两种部署，同一套模块

当前安装包、源码版本和已有安装的升级步骤见 [版本与升级说明](../docs/RELEASES.md)。

- 桌面：`desktop/Install Local.cmd` 初始化 Python，`desktop/Start Local Portal.cmd` 启动本机；可继续使用现有 exe。
- 服务器：`server/install.sh` 管理 Ubuntu 上的 Portal 用户服务；`server/install_web.sh` 提供 HTTPS 入口，直接用浏览器访问。
- 云端快捷入口：在 `configs/portal/cloud.json` 填写 HTTPS 地址后，运行 `desktop/Open Cloud Portal.cmd`；无需本机后台或隧道。
- 发布：`python tools/package_release.py --output runs/releases` 从一个仓库生成两个独立 ZIP。

[AWS 安装、账户限制与验证范围](../docs/SERVER.md)。发布包没有密钥、登录令牌、账本、缓存和 Python 虚拟环境。部署分开，策略、数据、券商和框架源码保持同一套。

回归测试请在完整源码仓库运行 `python -X utf8 tools/test_project.py`。测试夹具不会登记到正式策略库，也不包含在桌面或服务器发布 ZIP 中。
