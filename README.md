# 安宁礼仪与公墓运营服务

这是一个供殡仪馆、公墓和合作医疗机构使用的 Python 后端服务，统一管理逝者业务档案、遗体保管交接、送别厅与火化设备预约、服务订单、墓位权属、账单收款和审计时间线。针对身份不明逝者，系统提供身份候选与认领流程：医院、公安、寻亲家庭等来源的线索分源保留原文与可信等级，候选身份支持合并、排除与撤回，家属认领须经过材料校验与职责分离的复核后才能形成认领结论。系统把容易产生争议的交接、排程与收费动作保存在本地 SQLite 中，支持在单个 Linux 应用容器内离线运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

依次执行 python -m venv .venv、source .venv/bin/activate、python -m pip install -e ".[dev]"。可通过 PEACEFUL_CARE_DATABASE_PATH 指定数据库文件，默认写入项目的 data 目录。

## 初始化与启动

先执行 python -m app.cli init-db 和 python -m app.cli check-db，再用 uvicorn app.main:app --host 0.0.0.0 --port 8432 启动。健康检查为 GET /api/system/health。殡葬业务接口位于 /api/mortuary，涵盖档案、交接、资源、预约、服务订单、墓位权属、账单和时间线。身份候选与认领接口位于 /api/identify，涵盖线索报送与撤回、候选合并/排除/撤回、认领提交、材料校验、复核确认、认领结论与时间线。

## 测试与编译检查

测试命令：python -m pytest

编译命令：python -m compileall -q app tests

API 与 CLI 冒烟命令：python -m app.cli smoke、python -m app.cli mortuary-demo

## 目录结构

- app/mortuary：档案、保管交接、资源排程、权属和账单领域
- app/identify：身份不明逝者的线索、身份候选与认领复核领域
- app/api：登录、角色、审计及系统管理接口
- app/core：时钟、安全、异常、隐私与分页能力
- app/repositories：通用身份和审计数据访问
- app/services：会话、权限、后台任务及维护服务
- tests：领域、接口、异常路径和身份回归测试

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时事务。业务档案采用外部编号去重，保管交接与预约保留幂等键，服务订单开票后不可再次开票，支付流水不能重复分配。关键状态变化同时写入领域时间线；会话令牌仅保存摘要，审计记录不会保存明文密码或令牌。

身份候选与认领流程的约定：线索按来源保留原文与可信等级，重复报送凭幂等键去重且内容不一致时拒绝；线索与候选只做状态变更（撤回、合并、排除），不做物理删除，已被认领结论引用的线索不能撤回。认领必须先通过材料校验（verifier 角色），再由不同人员以复核职责（reviewer 角色）确认或驳回；每个档案至多形成一份认领结论，同一证件号码不能被确认为两起案件的逝者身份，确认后其余待比对候选自动排除并留存排除说明。查询接口按角色遮蔽证件号码与联系方式，coordinator、verifier、reviewer 可见原文，其余角色（含缺省）一律遮蔽。
