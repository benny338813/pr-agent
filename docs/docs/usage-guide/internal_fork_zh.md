# 內部 Fork 使用說明

本 fork 基於 [The-PR-Agent/pr-agent](https://github.com/The-PR-Agent/pr-agent) / Qodo PR-Agent 開源專案延伸，主要用於公司內部 GitLab MR 自動 review。

這份文件說明本 fork 新增的 GitLab、Ainexus、Jira 與 GitNexus 使用方式。原始 PR-Agent 的通用用法仍可參考英文文件；本頁聚焦在內部部署常用流程。

## 功能總覽

- GitLab MR review：支援以環境變數設定 GitLab URL、token 與 auth type。
- Ainexus LLM：透過 OpenAI-compatible API 呼叫內部模型。
- Jira ticket context：從 MR 描述或 branch name 擷取 Jira key，並用 PAT 讀取 ticket 內容。
- GitNexus context：透過 MCP 載入 code graph context，讓 review 不只看 diff。
- GitNexus drift analysis：當 GitNexus index 來自較舊 stable/protected branch 時，檢查中間變更是否和本次 PR diff 重疊。
- Smoke script：用單一指令檢查 GitLab、Jira、Ainexus 基本連線。

## 基本環境變數

建議把 token 都放在 CI/CD masked variables 或部署環境變數，不要寫進 repository。

```bash
export GITLAB_URL="https://gitlab.example.com"
export GITLAB_TOKEN="<gitlab-personal-access-token>"
export GITLAB_AUTH_TYPE="private_token"

export AI_NEXUUS_PAT="<ainexus-token>"
export JIRA_BOT_PAT="<jira-personal-access-token>"
```

`AI_NEXUUS_PAT` 是目前 `.pr_agent.toml` 使用的環境變數名稱；若公司實際命名不同，請同步調整 `.pr_agent.toml` 的 `openai.key_env`。

## `.pr_agent.toml` 範例

```toml
[config]
git_provider = "gitlab"
model = "openai/qwen/qwen3.6-35b-a3b-fp8"
fallback_models = []
custom_model_max_tokens = 32768
max_model_tokens = 32768
token_encoding = "cl100k_base"

[openai]
api_base = "https://ainexus.example.com/api/external/v1"
key_env = "AI_NEXUUS_PAT"

[litellm]
drop_params = true

[jira]
jira_base_url = "https://jira.example.com"
jira_api_token_env = "JIRA_BOT_PAT"
```

## GitLab 使用方式

GitLab provider 會依序讀取：

- `GITLAB_URL`
- `GITLAB_PERSONAL_ACCESS_TOKEN` 或 `GITLAB_TOKEN`
- `GITLAB_AUTH_TYPE`

若環境變數不存在，才回退到設定檔中的 `gitlab.url`、`gitlab.personal_access_token`、`gitlab.auth_type`。

常見 review 指令：

```bash
python -m pr_agent.cli \
  --pr_url "https://gitlab.example.com/group/project/-/merge_requests/123" \
  review
```

## Jira Ticket Context

本 fork 會從 MR 描述與 branch name 中擷取類似 `ABC-123` 的 Jira key，最多讀取前三筆 ticket。

支援兩種 Jira auth：

- PAT bearer token：只設定 `jira_api_token` 或 `jira_api_token_env`
- Basic auth：同時設定 `jira_api_email` 與 token

範例：

```toml
[jira]
jira_base_url = "https://jira.example.com"
jira_api_token_env = "JIRA_BOT_PAT"
jira_timeout = 30
```

若 Jira token 或 base URL 未設定，PR-Agent 會略過 Jira fetch，不會中斷 review。

## GitNexus 使用模式

GitNexus 是可選功能，預設關閉。沒有 GitNexus 的環境仍可正常跑 PR-Agent。

### `detect_changes`

適合本機或小型 repo，GitNexus 直接分析目前 worktree 的 diff。

```toml
[gitnexus]
enabled = true
command = "npx"
args = ["gitnexus", "mcp"]
mode = "detect_changes"
repo = "my-project"
scope = "compare"
base_ref = "main"
fail_on_error = false
```

### `base_context`

適合公司大型 GitLab repo。CI 定期在 stable/protected branch 上跑 `gitnexus analyze`，PR-Agent review 時只查既有 index，不對每個 MR branch 重新 analyze。

```toml
[gitnexus]
enabled = true
command = "npx"
args = ["gitnexus", "mcp"]
mode = "base_context"
repo = "my-project"
index_ref = "develop-stable"
index_commit = "abc1234"
max_queries = 5
max_symbols_per_file = 2
fail_on_error = false
```

在 `base_context` 模式下，GitNexus context 代表 indexed snapshot，不代表 PR source branch 的最新狀態。PR-Agent 會明確告訴 LLM：PR diff 才是新增或修改 code 的 source of truth，GitNexus 查不到不能被當成錯誤。

## Drift Analysis

若 GitNexus index 是較舊的 stable snapshot，可以啟用 drift analysis：

```toml
[gitnexus]
mode = "base_context"
index_commit = "abc1234"
drift_check = true
drift_repo_path = "/srv/gitnexus-workspaces/my-project"
drift_target_ref = "origin/develop"
drift_max_commits = 20
drift_policy = "warn"
```

PR-Agent 會比較：

```text
index_commit -> target/base ref 的中間變更
target/base ref -> PR source branch 的本次 PR diff
```

並產生信任度：

- `HIGH`：沒有直接 file/path/symbol overlap，GitNexus snapshot context 通常可信。
- `MEDIUM`：附近路徑有變動，GitNexus context 可用但可能較舊。
- `LOW`：直接檔案或 symbol 重疊，重疊區域應視為 stale。

`drift_policy = "skip_on_overlap"` 時，若 confidence 是 `LOW`，PR-Agent 會跳過 GitNexus context 查詢，只保留 drift warning，避免 stale context 誤導 review。

## 建議的 CI 流程

不要在每個 MR review 前跑完整 `gitnexus analyze`。大 repo 會很慢，也浪費空間。

建議流程：

```text
protected branch 定期或 merge 後：
  git checkout develop
  npx gitnexus analyze .
  保存 .gitnexus 或固定 workspace

MR review：
  PR-Agent 從 GitLab API 取得 MR diff
  GitNexus MCP 讀 stable index
  drift analysis 判斷 stable index 是否仍可信
  LLM review 使用 PR diff + GitNexus context
```

## Smoke Test

PR #4 新增 `scripts/smoke_gitlab_jira_ainexus.py`，可快速檢查 GitLab、Jira、Ainexus。

```bash
PYTHONPATH=. python scripts/smoke_gitlab_jira_ainexus.py \
  --mr-url "https://gitlab.example.com/group/project/-/merge_requests/123" \
  --gitlab-url "https://gitlab.example.com" \
  --gitlab-auth-type private_token \
  --jira-url "https://jira.example.com" \
  --ainexus-url "https://ainexus.example.com/api/external/v1"
```

若只想測 GitLab 與 Ainexus，可加：

```bash
--skip-jira
```

若只想測 GitLab 與 Jira，可加：

```bash
--skip-ainexus
```

## 注意事項

- 不要 commit token 或 `.gitnexus` index。
- `.gitnexus` 建議放在固定 workspace 或 CI cache/volume。
- `index_commit` 要和 GitNexus index metadata 對齊，否則 drift analysis 的信任度沒有意義。
- 若 PR 新增全新檔案或 function，GitNexus base snapshot 可能查不到；這不是 review finding，只代表該 symbol 不在 indexed snapshot 中。
