# 本地 Web 服务与账号

当前 V3 工作台使用 `spatial/` 下的 React 前端，FastAPI 提供查询、模型设置与自动运行接口。旧版轻前端与 L4/L5/L6 工作流接口保留供历史实现参考；它们不代表 V3 使用者需要手动触发采集、研究或生成。

## 启动

```bash
python -m pip install -r requirements.txt
python -m webapp.auth --init-user owner --role operator
python -m uvicorn webapp.main:app --host 127.0.0.1 --port 8202
```

初始化命令在终端交互输入并确认至少 12 字符的密码，不通过命令参数传递密码，也不打印密码或散列。新账号使用随机盐和 600,000 轮 PBKDF2-SHA256。文件已存在时命令拒绝覆盖，以保留已有本地账号。

默认账号文件为本地 `webapp/auth_users.json`，已加入 Git 忽略规则。可用 `TAPTAP_AUTH_USERS_FILE` 指定另一本地文件。账号文件缺失时登录返回 401，不自动创建公开演示账号。环境变量指定的文件也不得提交。角色权限在服务端检查；`operator` 适用于本地内容操作，`viewer` 为只读。

已有安装中的旧 SHA256 账号仍兼容；这不代表旧公开演示密码适合对外部署。旧文件不会被此次升级自动覆盖，请在部署环境自行配置新的私有账号。令牌采用本地 HMAC 签名，12 小时过期，目前没有 SSO、令牌撤销或完善的公网登录限流；当前默认绑定本机地址。

前端启动方法见[仓库 README](../README.md#本地运行)。GitHub Pages 是静态成果阅读站，不提供这套本地登录接口，也不运行模型或采集任务。

## 接口与隔离

- `POST /api/login` 校验账号并发放令牌；`GET /api/me` 验证当前身份。
- V3 业务与模型接口实现在 `agent_v3/api.py`，路由挂载于 `webapp/main.py`。
- 自动任务、研究工具、证据校验与业务交付由 `agent_v3/` 执行，不在 Web 层重复实现。
- 历史权限与审计由 `execution/audit.py` 定义；Agent 服务角色不拥有批准、上线或急停权限。
- 账号测试在临时目录生成合成账号，不依赖仓库内的可登录账号文件。

公开仓库检查脚本 `scripts/public_repo_guard.py` 检查当前 Git 索引，即便文件被忽略但强制加入索引也会拦截；它不检查或清除 Git 历史。
