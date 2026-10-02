# 安宁礼仪与公墓运营服务

这是一个供殡仪馆、公墓和合作医疗机构使用的 Python 后端服务，统一管理逝者业务档案、遗体保管交接、送别厅与火化设备预约、服务订单、墓位权属、账单收款和审计时间线。系统把容易产生争议的交接、排程与收费动作保存在本地 SQLite 中，支持在单个 Linux 应用容器内离线运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

依次执行 python -m venv .venv、source .venv/bin/activate、python -m pip install -e ".[dev]"。可通过 PEACEFUL_CARE_DATABASE_PATH 指定数据库文件，默认写入项目的 data 目录。

## 初始化与启动

先执行 python -m app.cli init-db 和 python -m app.cli check-db，再用 uvicorn app.main:app --host 0.0.0.0 --port 8432 启动。健康检查为 GET /api/system/health。殡葬业务接口位于 /api/mortuary，涵盖档案、交接、资源、预约、服务订单、墓位权属、账单和时间线。身份不明逝者的候选与家属认领接口位于 /api/identification，需使用 Bearer 会话令牌。

## 测试与编译检查

测试命令：python -m pytest

编译命令：python -m compileall -q app tests

API 与 CLI 冒烟命令：python -m app.cli smoke、python -m app.cli mortuary-demo

## 目录结构

- app/mortuary：档案、保管交接、资源排程、权属和账单领域
- app/identification：身份候选、多源线索、家属认领与确认结论领域
- app/api：登录、角色、审计及系统管理接口
- app/core：时钟、安全、异常、隐私与分页能力
- app/repositories：通用身份和审计数据访问
- app/services：会话、权限、后台任务及维护服务
- tests：领域、接口、异常路径和身份回归测试

## 身份候选与认领

面向身份不明逝者档案，系统把医院、公安和寻亲家属提供的线索结构化保存，而不是堆进备注：

- 每个候选保留来源类型、来源编号、可信等级（high/medium/low）；线索保留原文、类型（姓名片段、随身物品、亲属关系等）、来源和可信等级，只增不改。
- 候选可以合并（来源候选并入目标候选、线索随附）、排除或撤回，记录均不物理删除；有在途认领时禁止合并或排除。
- 每次合并、排除、撤回、材料校验、复核都会生成决定，并把当时依据的线索与材料做 SHA-256 快照，决定之后证据不可消失。
- 家属认领必须随附材料，先由具备 `identity.material_check` 权限的工作人员校验，再由具备 `identity.review` 权限的另一人复核；同一用户不能完成两步。
- 复核通过即确认身份：每个业务档案在 `identity_resolutions` 中至多一行结论，冲突候选自动排除，其他在途认领标记为 `superseded`，确认后档案锁定。
- 认领提交按 `(case_id, idempotency_key)` 幂等，重复报送回放原记录；结论持久化，服务重启不会产生第二份认领结论。
- 查询按角色遮蔽：仅 `identity.sensitive` 权限可查看证件号、家属电话与线索原文中的联系方式；其他角色看到的是脱敏值。
- 系统角色：`identity_clerk`（登记候选/线索/认领）、`identity_checker`（材料校验，可见明文）、`identity_reviewer`（复核确认，可见明文）；管理员拥有全部权限。

主要接口：`POST /api/identification/candidates`、`POST /candidates/{id}/clues`、`POST /candidates/merge`、`POST /candidates/{id}/exclude|withdraw`、`POST /api/identification/claims`、`POST /claims/{id}/material-check`、`POST /claims/{id}/review`、`POST /claims/{id}/withdraw`、`GET /api/identification/cases/{case_id}`。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时事务。业务档案采用外部编号去重，保管交接与预约保留幂等键，服务订单开票后不可再次开票，支付流水不能重复分配。关键状态变化同时写入领域时间线；会话令牌仅保存摘要，审计记录不会保存明文密码或令牌。
