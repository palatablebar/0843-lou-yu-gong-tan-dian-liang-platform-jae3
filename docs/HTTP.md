# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据（若已衍生凭证，内含 `voucher`/`corrections`）。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 更正、凭证与入账（M8）

签发通过后自动生成当期凭证（整数 Wh 记账，`*_kwh` 为展示值）。

- `POST /api/vouchers`：创建独立凭证，或在 body 带 `run_id` 时取得批次凭证。
- `GET /api/vouchers?run_id=&period=`：列出凭证。
- `GET /api/vouchers/{id}`：凭证当前状态、版本、更正链与差额。
- `POST /api/vouchers/{id}/corrections`：提交更正并自动对账。
  - 绝对电量：`quantity_kwh` / `total_kwh` / `after_kwh` / `corrected_kwh`，或 `quantity_wh`、历史命名 `common_mwh` 等。
  - 相对差额：`delta_kwh` / `difference_wh` / `adjustment_kwh`（按“当前电量 + 带符号差额”）。
  - 可带 `expected_revision` 做乐观锁、`idempotency_key` 做重试幂等。
  - 对账失败返回 `409 RECONCILE_FAILED`，旧版本保持不变且仍可下载。
- `POST /api/vouchers/{id}/post`（别名 `/posting`、`/book`、`/enter`）：入账；旧 revision 返回 `409 STALE_VOUCHER`，超过 `expires_at` 返回 `409 VOUCHER_EXPIRED`。
- `GET /api/vouchers/{id}/download[?version=N|posted|original][&format=csv]`：下载凭证，含更正前 `original/before_wh` 与更正后 `corrected/after_wh` 电量、版本链与稳定摘要。
- `GET /api/vouchers/{id}/differences`：本凭证结转下期的差额。
- `GET /api/periods/{period}/differences`：某下期期间收到的差额。

批次作用域同义入口（会按需自动建立凭证）：

- `GET|POST /api/runs/{id}/voucher`、`/api/runs/{id}/vouchers`
- `POST /api/runs/{id}/corrections`（别名 `/correct`、`/correction`）
- `POST /api/runs/{id}/post`（别名 `/posting`、`/book`、`/enter`）
- `GET /api/runs/{id}/voucher/download`

规则：入账前对账通过的更正生成新版本并更新当期凭证；入账后 `posted_snapshot` 冻结，更正只产生一笔按 `correction_id` 去重的差额到下期凭证。并发提交由 `BEGIN IMMEDIATE` 事务与 revision 串行化：入账先生效时更正自动结转差额，更正先生效时旧版入账请求以 `STALE_VOUCHER` 拒绝。相同 `idempotency_key` 或相同更正内容的重试只返回首次结果，不重复记差额。
