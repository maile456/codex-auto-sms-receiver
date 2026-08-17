# 邮箱任务 API 对接 README

该 API 适合同一台电脑上的另一个项目调用。调用方只需传送邮箱地址，然后查询处理状态。邮箱密码、取码 URL、验证码、OAuth Token 和凭证文件都保留在 Codex Auto SMS Receiver 本机数据目录中，API 只返回布尔状态和任务进度。

## 1. 准备

1. 启动接码机，默认地址为 `http://127.0.0.1:5015`。
2. 先在接码机 WebUI 导入邮箱及取码素材。
3. 另一个项目只保存邮箱地址，不保存验证码或凭证。

## 2. 提交邮箱任务

```http
POST /api/v1/integration/accounts/submit
Content-Type: application/json

{"email":"user@example.com"}
```

curl：

```bash
curl -X POST "http://127.0.0.1:5015/api/v1/integration/accounts/submit" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com"}'
```

首次启动任务返回 HTTP `202`：

```json
{
  "ok": true,
  "started": true,
  "account": {
    "email": "user@example.com",
    "state": "queued",
    "credential_ready": false,
    "phone_verified": false,
    "updated_at": "2026-08-17T02:00:00+00:00",
    "task": {
      "id": "job-id",
      "status": "queued"
    }
  }
}
```

已有任务正在处理，或本机已有凭证时，返回 HTTP `200` 且 `started=false`，不会重复启动。

## 3. 查询状态

```http
GET /api/v1/integration/accounts/status?email=user%40example.com
```

curl：

```bash
curl --get "http://127.0.0.1:5015/api/v1/integration/accounts/status" \
  --data-urlencode "email=user@example.com"
```

## 4. 状态值

| `state` | 含义 |
| --- | --- |
| `idle` | 邮箱已导入，尚未启动任务 |
| `queued` | 已入队 |
| `running` | 正在处理 |
| `retry_wait` | 等待重试 |
| `paused` | 已暂停 |
| `completed` | 任务已完成，正在确认凭证归档 |
| `ready` | 凭证已归档在接码机本地 |
| `failed` | 任务失败 |
| `stopped` | 任务已停止 |

对接方的成功判断建议为：

```text
account.state == "ready" && account.credential_ready == true
```

## 5. Python 对接示例

```python
import time
import requests

BASE_URL = "http://127.0.0.1:5015"
EMAIL = "user@example.com"

submit = requests.post(
    f"{BASE_URL}/api/v1/integration/accounts/submit",
    json={"email": EMAIL},
    timeout=10,
)
submit.raise_for_status()

while True:
    response = requests.get(
        f"{BASE_URL}/api/v1/integration/accounts/status",
        params={"email": EMAIL},
        timeout=10,
    )
    response.raise_for_status()
    account = response.json()["account"]
    if account["state"] == "ready" and account["credential_ready"]:
        print("处理完成，凭证已保存在接码机")
        break
    if account["state"] in {"failed", "stopped"}:
        raise RuntimeError(account.get("task") or account["state"])
    time.sleep(3)
```

## 6. Node.js 对接示例

```javascript
const baseUrl = "http://127.0.0.1:5015";
const email = "user@example.com";

const submit = await fetch(`${baseUrl}/api/v1/integration/accounts/submit`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email }),
});
if (!submit.ok) throw new Error(`submit HTTP ${submit.status}`);

for (;;) {
  const url = new URL("/api/v1/integration/accounts/status", baseUrl);
  url.searchParams.set("email", email);
  const response = await fetch(url);
  if (!response.ok) throw new Error(`status HTTP ${response.status}`);
  const { account } = await response.json();
  if (account.state === "ready" && account.credential_ready) break;
  if (["failed", "stopped"].includes(account.state)) {
    throw new Error(JSON.stringify(account.task || account.state));
  }
  await new Promise(resolve => setTimeout(resolve, 3000));
}
```

## 7. 错误码

| HTTP | `error` | 含义 |
| --- | --- | --- |
| 400 | `invalid_email` | 邮箱缺失或格式错误 |
| 404 | `account_not_found` | 该邮箱尚未导入接码机 |
| 409 | `job_start_failed` | 任务管理器未接受该任务 |

## 8. 数据边界

API 响应中不包含以下字段：

- 邮箱密码、Refresh Token、TOTP 密钥、取码 URL、验证码。
- OAuth Access Token、OAuth Refresh Token、凭证文件路径或凭证原文。
- 手机号码原文。

对接服务默认只监听 `127.0.0.1`，另一个项目应与它运行在同一台电脑上。
