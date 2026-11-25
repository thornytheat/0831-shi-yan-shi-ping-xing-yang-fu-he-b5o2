# 阶段接口

`POST /api/stages/<stage>` 接收 `multipart/form-data`。字段 `sample=dirty`、`sample=clean` 或 `file=<CSV>` 三选一。阶段为 `intake`、`prepare`、`calculate`、`policy`、`balance`。预览不入库。

`POST /api/runs` 接收同样的输入，执行全批并返回运行报告。`GET /api/runs` 返回最近 50 批。`GET /api/runs/<id>` 查询一批和当前复核状态。

`PATCH /api/runs/<id>` 接收 `revision`、`status` 和 `note`。状态使用 `APPROVED` 或 `HELD`。冲突返回 409，客户端读取新版本后重试。

`GET /api/runs/<id>/evidence` 下载内含运行清单、行号隔离、业务证据和复核信息的 JSON。

## 样品签发与复测

样品键为 `<case>:<sample>`，例如 `0004:1`；路径中也可写成 `0004/1`。

- `GET /api/runs/<id>/samples`（别名 `/sample-decisions`、`/conclusions`、`/workbench`）返回各样品当前结论、版本、是否可签发及采用的测量记录。`GET /api/runs/<id>` 也会附带 `samples`。
- `POST /api/runs/<id>/retests` 提交一侧或同次成对复测。成对数据必须使用相同 `round`/`retest_id`；只到一侧时该样品保持 `HELD/RETEST_PAIR_INCOMPLETE`。也支持上传同表头 CSV，并用表单字段 `round` 指定复测批次。
- `POST /api/runs/<id>/samples/<sample>/invalidation` 提交原测作废凭据，字段为 `evidence`（也接受 `voidance`/`invalidation` 对象）。
- `POST /api/runs/<id>/samples/<sample>/release` 签发当前可签发样品；body 可带样品 `revision` 或批次 `revision`。同批其他样品暂缓不阻止合格样品签发。
- `POST /api/runs/<id>/corrections` 或 `POST /api/runs/<id>/retests/<measurement>/corrections` 入账复测更正。更正会推进样品与批次版本并重算待签结论；旧版本签发返回 409 `STALE_REVIEW`。

原测成对合格时采用 `ORIGINAL` 记录；原测超差后，只有同一 `round` 的复测成对合格、两侧 LOD/LOQ 一致且原测作废凭据已入账，才采用 `RETEST` 记录签发。禁止把单侧复测与原测或另一次复测拼对。
