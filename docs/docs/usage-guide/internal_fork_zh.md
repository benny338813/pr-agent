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

### GitLab Webhook 自動建立 MR Head Index

如果公司專案跑一次 `gitnexus analyze` 約數分鐘且可接受，可以讓 GitLab webhook 在 MR 開啟或 push 更新後，先 clone MR source commit 並建立該 commit 專屬的 GitNexus index，再執行 PR-Agent review。

這個模式適合你希望 review 使用「PR 修改後的 branch head」作為 GitNexus context，而不是較舊的 stable/base snapshot。

```toml
[gitnexus_indexer]
enabled = true
workspace_root = "/var/lib/pr-agent/gitnexus-workspaces"
analyze_command = "npx"
analyze_args = ["gitnexus", "analyze", "."]
mcp_command = "npx"
mcp_args = ["gitnexus", "mcp"]
timeout_seconds = 300
max_parallel_jobs = 4
ttl_hours = 72
reuse_existing_index = true
per_mr_latest_only = true
cleanup_on_webhook = true
max_queries = 5
max_symbols_per_file = 2
fail_on_error = false
```

GitLab webhook 會建立類似下列路徑：

```text
/var/lib/pr-agent/gitnexus-workspaces/
  <project-id>/
    <mr-iid>/
      <source-sha>/
        repo/
          .gitnexus/
```

不同 MR、不同 commit SHA 會使用不同 workspace，因此可以平行執行 `npx gitnexus analyze`。`max_parallel_jobs` 只用來限制同一個 PR-Agent process 同時跑太多分析工作，避免機器 CPU、RAM 或磁碟 IO 被打滿。

當 index 建立完成後，PR-Agent 會在同一次 review 裡自動注入：

```toml
[gitnexus]
enabled = true
mode = "pr_head_context"
command = "npx"
args = ["gitnexus", "mcp"]
working_dir = "/var/lib/pr-agent/gitnexus-workspaces/<project-id>/<mr-iid>/<source-sha>/repo"
index_ref = "<source-branch>"
index_commit = "<source-sha>"
```

`pr_head_context` 代表 GitNexus index 來自 MR source branch head。LLM 會被告知：PR diff 仍是 review finding 的 source of truth，而 GitNexus 可用來理解目前 MR head 的 symbol、call graph 與影響範圍。

若同一個 MR 又 push 新 commit，`per_mr_latest_only = true` 會避免較舊 SHA 的分析結果被拿來做 review。若 GitNexus analyze timeout 或失敗，PR-Agent 會記錄 warning，並 fallback 成沒有 GitNexus context 的一般 review。

`ttl_hours` 與 `cleanup_on_webhook` 用來回收舊 workspace，避免 `.gitnexus` index 長期累積。

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

如果每次 MR 跑完整 `gitnexus analyze` 太慢，仍可使用 stable/base snapshot 流程。大 repo 可以先用 `gitnexus_indexer` 實測耗時；若可接受，MR head index 的準確度通常最好。若不可接受，再改用 stable snapshot + drift analysis。

stable snapshot 建議流程：

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
