# 陶驿窑务·预检记录分片

加权 Rendezvous Hash 重算候选归属，结合每轮搬迁字节预算生成可执行计划。复核后的搬迁清单可在工作台预约搬迁时段，按库计算带宽用量并排期。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 窑口节点
- 预检批次
- 重分片预算
- 搬迁清单

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18301`
- FastAPI 文档 `http://127.0.0.1:19301/docs`
- MySQL 8.0 端口 `127.0.0.1:17801`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷
- 后端测试 `cd backend && python -m pytest tests -q`（使用内存 SQLite，无需容器）

### 时段预约

搬迁清单页（即执行证据页）在清单复核后可预约时段，经办员操作、复核员维护带宽、观察员只读。

- 库管（复核员）在清单页的「库管可借带宽」按 `字节/小时` 给各窑口库设可借带宽。未设置的库不能预约。
- 用量 = 清单中该库收发的字节数 ÷ 预约时长。清单里一条搬迁同时占用源库和目标库的带宽。
- 同一时刻各库用量相加后不得超过该库可借带宽；首尾相接（上一段结束即下一段开始）可以连排。
- 清单涉及的每个库都要有余量才预约成功；并发请求按库加锁，不会超卖。
- 同一清单只保留一份有效预约：重复提交同一时段返回原预约，不会重复占额度；想换时段用改期。
- 改期约不上时保留原时段；撤销后余量立即释放，可马上再约。

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果 |
| `reviewer` | 复核员 | 读取、导出和签署结果 |
| `viewer` | 观察员 | 读取、导出已有结果 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | MySQL 8.0，持久卷保存档案、执行、证据、带宽、预约及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明，`backend/src/services/booking_rules.py` 是预约的纯规则（用量与时段冲突），`backend/src/services/booking.py` 负责持久化、加锁与幂等。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。开发时更新源文件后重新构建对应服务。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY` 、`SEED_DEMO` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

[MIT License](LICENSE)
