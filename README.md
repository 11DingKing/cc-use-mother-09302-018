# 校园文化活动申诉

校园文化活动申诉的领域约定与完整后端：把案件与**原业务记录、理由、证据版本、受理期限、回避关系、法定签署人数**连接起来，支持撤回、补证、案件合并、临时措施、决定重开的按状态推进，并区分当事人与管理员视图。

## 领域约束（见 `domain/contract.json`）

- **状态机**：提出 → 受理 → 调查 → 复核 → 决定；决定 → 重开 → 复核；提出/受理/调查/复核 → 已撤回；被合并案件 → 已合并。
- **案件关联**：立案必须关联原业务记录编号；仅同一原业务记录且决定前的案件可合并。
- **期限控制**：受理期限 5 日、重开期限 15 日（可在契约 `policies` 调整）。
- **法定签署**：作出决定至少 3 名具资格签署人，且签署人不得与原业务记录存在回避关系。
- **原评审人回避**：回避关系按「人员 × 原业务记录」登记，在受理、指派、调查、复核、签署各环节强制拦截。
- **证据版本只增不改**：邮件等渠道补证只追加新版本（`supersedes` 指向前序版本），最初版本永久保留。
- **立案幂等**：`Idempotency-Key` + 请求体指纹；重试返回原案件，篡改请求体直接拒绝。
- **当事人隔离**：当事人只看得到自己的案件、自己提交的材料；管理员看到完整时间线与新旧决定差异。

## 目录

- `domain/contract.json`：领域角色、状态、推进表、策略参数与样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/appeals/`：申诉后端
  - `models.py`：用户、当事人、回避关系、证据版本、案件、决定、临时措施、时间线。
  - `states.py` / `policy.py`：状态推进表与期限/签署策略（均从契约装配）。
  - `storage.py`：线程安全内存存储与幂等键表（可替换为数据库实现）。
  - `service.py`：全部业务规则与当事人/复核人/管理员视图。
  - `diffing.py`：新旧决定的结构化差异（结论、正文 unified diff、签署人增减）。
  - `http_api.py`：零依赖 HTTP API（标准库）。
  - `app.py`：契约装配与演示种子数据。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、36 项服务规则、HTTP 端到端测试。

## HTTP API

所有业务请求带 `X-User-Id` 头标识操作人；立案带 `Idempotency-Key` 实现重试幂等。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/users` | 登记用户（四类角色） |
| POST | `/recusals` | 登记回避关系（人员 × 原业务记录） |
| POST | `/cases` | 提起申诉（幂等） |
| GET | `/cases` | 案件列表（按角色隔离） |
| GET | `/cases/{id}` | 案件详情（当事人/复核人/管理员视图不同） |
| POST | `/cases/{id}/evidence` | 补证（追加版本，渠道 online/email/post/onsite） |
| POST | `/cases/{id}/accept` | 受理（校验受理期限与回避） |
| POST | `/cases/{id}/assign` | 指派复核人员（校验回避） |
| POST | `/cases/{id}/investigate` | 进入调查 |
| POST | `/cases/{id}/review` | 进入复核（重开后必须再次经过） |
| POST | `/cases/{id}/decisions` | 作出决定（≥3 名签署人，产生新版本） |
| POST | `/cases/{id}/withdraw` | 撤回（仅决定前、当事人本人） |
| POST | `/cases/{id}/reopen` | 决定重开（15 日内） |
| POST | `/cases/merge` | 案件合并（管理员） |
| POST | `/cases/{id}/interim-measures` | 申请临时措施 |
| POST | `/cases/{id}/interim-measures/{mid}/decide` | 批准/驳回临时措施 |
| POST | `/cases/{id}/interim-measures/{mid}/lift` | 解除临时措施 |
| GET | `/cases/{id}/decision-diff` | 新旧决定差异（管理员） |
| GET | `/records/{rid}/recusals` | 按原业务记录查回避关系（管理员） |

错误返回统一为 `{"error": <code>, "message": ...}`，状态码：400 校验、403 越权、404 不存在、409 状态/幂等冲突、422 期限/回避/签署/合并违规。

## 运行

```bash
# 启动（可选 --seed 写入演示案件：含邮件补证 v2 与原评审人回避关系）
PYTHONPATH=src python3 -m appeals.http_api --port 8080 --seed

# 示例
curl -s -H "X-User-Id: parent1" http://127.0.0.1:8080/cases
```

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
