# PR-Agent 內部 GitLab 版快速啟動

這個 fork 用來在公司內部 GitLab 上執行 PR-Agent review，預設整合：

- GitLab：`https://fw-git2.phison.com`
- Ainexus OpenAI-compatible LLM：`Qwen/Qwen3.6-35B-A3B-FP8`
- Jira：`https://jira.phison.com:8443`

原始上游 README 已保留在 `README.old.md`。

## 1. 準備 Python 環境

專案要求 Python 3.12。若已經有 `pyenv` 環境 `pr-agent-env`，直接使用即可。

安裝依賴：

```bash
cd /home/sysd92/桌面/service/pr-agent
PYENV_VERSION=pr-agent-env pyenv exec python -m pip install -r requirements.txt
PYENV_VERSION=pr-agent-env pyenv exec python -m pip install -r requirements-dev.txt
```

如果公司憑證會造成 Python HTTPS 驗證問題，可額外安裝：

```bash
PYENV_VERSION=pr-agent-env pyenv exec python -m pip install pip-system-certs
```

## 2. 設定 Secrets

本機 secrets 放在：

```text
pr_agent/settings/.secrets.toml
```

必要內容範例：

```toml
[config]
git_provider = "gitlab"
model = "openai/Qwen/Qwen3.6-35B-A3B-FP8"
fallback_models = []
custom_model_max_tokens = 32768
max_model_tokens = 32768
token_encoding = "cl100k_base"

[gitlab]
url = "https://fw-git2.phison.com"
auth_type = "private_token"
personal_access_token = "你的 GitLab PAT"
shared_secret = "你的 GitLab webhook secret"

[openai]
api_base = "https://ainexus.phison.com/api/external/v1"
key_env = "AI_NEXUUS_PAT"

[litellm]
drop_params = true

[jira]
jira_base_url = "https://jira.phison.com:8443"
jira_api_token_env = "JIRA_BENNY_BOT_PAT"
```

LLM 和 Jira token 建議放環境變數，不要寫入 Git：

```bash
export AI_NEXUUS_PAT="你的 Ainexus PAT"
export JIRA_BENNY_BOT_PAT="你的 Jira PAT"
```

`pr_agent/settings/.secrets.toml` 不應 commit。

## 3. 快速測試

先測 GitLab、Jira、Ainexus 是否都能連通：

```bash
cd /home/sysd92/桌面/service/pr-agent
PYENV_VERSION=pr-agent-env PYTHONPATH=. pyenv exec python scripts/smoke_gitlab_jira_ainexus.py
```

預期會看到：

```text
GitLab MR:
  status: ok
Jira:
  status: ok
Ainexus:
  status_code: 200
```

測單一 MR 且不發 comment：

```bash
PYENV_VERSION=pr-agent-env PYTHONPATH=. pyenv exec python -m pr_agent.cli \
  --pr_url "https://fw-git2.phison.com/d9/ps5302-qlc/code/fw/x2_qlc_r5/-/merge_requests/3591" \
  review \
  --config.publish_output=false
```

確認成功後，移除 `--config.publish_output=false` 才會真的回覆 MR。

## 4. 一鍵啟動服務

啟動 GitLab webhook 服務：

```bash
cd /home/sysd92/桌面/service/pr-agent
./scripts/run_gitlab_webhook.sh
```

預設監聽 port `3000`。也可以指定 port：

```bash
PORT=8080 ./scripts/run_gitlab_webhook.sh
```

如果想啟動前先跑 smoke：

```bash
RUN_SMOKE=1 ./scripts/run_gitlab_webhook.sh
```

GitLab webhook URL 設成：

```text
http://你的機器IP:3000/webhook
```

若 GitLab 無法連到本機，請透過公司內部 reverse proxy、固定主機、或 ngrok 類工具轉發到此服務。

## 5. 開機自動啟動

建立 systemd service：

```bash
sudo tee /etc/systemd/system/pr-agent-gitlab.service >/dev/null <<'EOF'
[Unit]
Description=PR-Agent GitLab Webhook
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sysd92
WorkingDirectory=/home/sysd92/桌面/service/pr-agent
Environment=PYENV_VERSION=pr-agent-env
Environment=PYTHONPATH=.
Environment=PORT=3000
Environment=AI_NEXUUS_PAT=你的 Ainexus PAT
Environment=JIRA_BENNY_BOT_PAT=你的 Jira PAT
ExecStart=/home/sysd92/.pyenv/bin/pyenv exec python -m pr_agent.servers.gitlab_webhook
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

啟用並啟動：

```bash
sudo systemctl daemon-reload
sudo systemctl enable pr-agent-gitlab
sudo systemctl start pr-agent-gitlab
```

查看狀態：

```bash
systemctl status pr-agent-gitlab
journalctl -u pr-agent-gitlab -f
```

如果不想把 token 寫進 service 檔，可改用 `EnvironmentFile` 指向只給本機讀取的檔案。

## 6. 常用指令

查看設定是否被讀到：

```bash
PYENV_VERSION=pr-agent-env PYTHONPATH=. pyenv exec python - <<'PY'
from pr_agent.config_loader import get_settings
for key in [
    "config.git_provider",
    "config.model",
    "gitlab.url",
    "gitlab.auth_type",
    "openai.api_base",
    "jira.jira_base_url",
]:
    print(key, "=", get_settings().get(key))
PY
```

重新跑單元測試：

```bash
PYENV_VERSION=pr-agent-env PYTHONPATH=. pyenv exec python -m pytest \
  tests/unittest/test_jira_ticket_context.py \
  tests/unittest/test_litellm_api_key_guard.py \
  tests/unittest/test_gitlab_provider.py \
  -q
```
