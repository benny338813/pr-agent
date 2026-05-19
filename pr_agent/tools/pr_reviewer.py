import copy
import datetime
import json
import re
import subprocess
import traceback
from collections import OrderedDict
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.pr_processing import (add_ai_metadata_to_diff_files,
                                         get_pr_diff,
                                         retry_with_fallback_models)
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.algo.utils import (ModelType, PRReviewHeader,
                                 convert_to_markdown_v2, github_action_output,
                                 load_yaml, show_relevant_configurations)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import (get_git_provider,
                                    get_git_provider_with_context)
from pr_agent.git_providers.git_provider import (IncrementalPR,
                                                 get_main_pr_language)
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.ticket_pr_compliance_check import (
    extract_and_cache_pr_tickets, extract_tickets)


from pr_agent.tools.base import PRTool
from pr_agent.tools.registry import ToolRegistry

@ToolRegistry.register("review")
@ToolRegistry.register("review_pr")
@ToolRegistry.register("auto_review")
@ToolRegistry.register("answer")
class PRReviewer(PRTool):
    """
    The PRReviewer class is responsible for reviewing a pull request and generating feedback using an AI model.
    """
    GITNEXUS_COMMON_SYMBOLS = {
        "bool", "char", "class", "const", "else", "enum", "false", "for", "fprintf", "free", "if", "int",
        "long", "malloc", "memcpy", "memset", "printf", "return", "sizeof", "snprintf", "static", "strcmp",
        "strdup", "strlen", "strncmp", "struct", "true", "void", "while",
    }
    
    def __init__(self, pr_url: str, is_answer: bool = False, is_auto: bool = False, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        """
        Initialize the PRReviewer object with the necessary attributes and objects to review a pull request.
        """
        super().__init__(pr_url, ai_handler=ai_handler, args=args)
        self.git_provider = get_git_provider_with_context(pr_url)
        self.args = args
        self.incremental = self.parse_incremental(args)  # -i command
        if self.incremental and self.incremental.is_incremental:
            self.git_provider.get_incremental_commits(self.incremental)

        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.pr_url = pr_url
        self.is_answer = is_answer
        self.is_auto = is_auto

        if self.is_answer and not self.git_provider.is_supported("get_issue_comments"):
            raise Exception(f"Answer mode is not supported for {get_settings().config.git_provider} for now")
        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.prediction = None
        self.gitnexus_context_status = "not_used"
        self.gitnexus_context_mode = ""
        answer_str, question_str = self._get_user_answers()
        self.pr_description, self.pr_description_files = (
            self.git_provider.get_pr_description(split_changes_walkthrough=True))
        if (self.pr_description_files and get_settings().get("config.is_auto_command", False) and
                get_settings().get("config.enable_ai_metadata", False)):
            add_ai_metadata_to_diff_files(self.git_provider, self.pr_description_files)
            get_logger().debug(f"AI metadata added to the this command")
        else:
            get_settings().set("config.enable_ai_metadata", False)
            get_logger().debug(f"AI metadata is disabled for this command")

        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.pr_description,
            "language": self.main_language,
            "diff": "",  # empty diff for initial calculation
            "num_pr_files": self.git_provider.get_num_of_files(),
            "num_max_findings": get_settings().pr_reviewer.num_max_findings,
            "require_score": get_settings().pr_reviewer.require_score_review,
            "require_tests": get_settings().pr_reviewer.require_tests_review,
            "require_estimate_effort_to_review": get_settings().pr_reviewer.require_estimate_effort_to_review,
            "require_estimate_contribution_time_cost": get_settings().pr_reviewer.require_estimate_contribution_time_cost,
            'require_can_be_split_review': get_settings().pr_reviewer.require_can_be_split_review,
            'require_security_review': get_settings().pr_reviewer.require_security_review,
            'require_todo_scan': get_settings().pr_reviewer.get("require_todo_scan", False),
            'question_str': question_str,
            'answer_str': answer_str,
            "extra_instructions": get_settings().pr_reviewer.extra_instructions,
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "custom_labels": "",
            "enable_custom_labels": get_settings().config.enable_custom_labels,
            "is_ai_metadata":  get_settings().get("config.enable_ai_metadata", False),
            "related_tickets": get_settings().get('related_tickets', []),
            "context_usage_summary": "",
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
            "date": datetime.datetime.now().strftime('%Y-%m-%d'),
        }

        self.token_handler = TokenHandler(
            self.git_provider.pr,
            self.vars,
            get_settings().pr_review_prompt.system,
            get_settings().pr_review_prompt.user
        )

    def parse_incremental(self, args: List[str]):
        is_incremental = False
        if args and len(args) >= 1:
            arg = args[0]
            if arg == "-i":
                is_incremental = True
        incremental = IncrementalPR(is_incremental)
        return incremental

    async def run(self) -> None:
        try:
            if not self.git_provider.get_files():
                get_logger().info(f"PR has no files: {self.pr_url}, skipping review")
                return None

            if self.incremental.is_incremental and not self._can_run_incremental_review():
                return None

            # if isinstance(self.args, list) and self.args and self.args[0] == 'auto_approve':
            #     get_logger().info(f'Auto approve flow PR: {self.pr_url} ...')
            #     self.auto_approve_logic()
            #     return None

            get_logger().info(f'Reviewing PR: {self.pr_url} ...')
            relevant_configs = {'pr_reviewer': dict(get_settings().pr_reviewer),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            # ticket extraction if exists
            await extract_and_cache_pr_tickets(self.git_provider, self.vars)
            related_tickets = self.vars.get("related_tickets", [])
            gitnexus_config = self._get_gitnexus_config() or {}
            get_logger().info(
                "PR review context status",
                artifact={
                    "pr_url": self.pr_url,
                    "jira_ticket_count": len(related_tickets),
                    "jira_ticket_ids": [ticket.get("ticket_id") for ticket in related_tickets if isinstance(ticket, dict)],
                    "gitnexus_enabled": bool(gitnexus_config.get("enabled", False)),
                    "gitnexus_mode": gitnexus_config.get("mode", ""),
                },
            )

            if self.incremental.is_incremental and hasattr(self.git_provider, "unreviewed_files_set") and not self.git_provider.unreviewed_files_set:
                get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new files")
                previous_review_url = ""
                if hasattr(self.git_provider, "previous_review"):
                    previous_review_url = self.git_provider.previous_review.html_url
                if get_settings().config.publish_output:
                    self.git_provider.publish_comment(f"Incremental Review Skipped\n"
                                    f"No files were changed since the [previous PR Review]({previous_review_url})")
                return None

            if get_settings().config.publish_output and not get_settings().config.get('is_auto_command', False):
                self.git_provider.publish_comment("Preparing review...", is_temporary=True)

            await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            if not self.prediction:
                self.git_provider.remove_initial_comment()
                return None

            pr_review = self._prepare_pr_review()
            get_logger().debug(f"PR output", artifact=pr_review)

            should_publish = get_settings().config.publish_output and self._should_publish_review_no_suggestions(pr_review)
            if not should_publish:
                reason = "Review output is not published"
                if get_settings().config.publish_output:
                    reason += ": no major issues detected."
                get_logger().info(
                    reason,
                    artifact={
                        "pr_url": self.pr_url,
                        "publish_output": bool(get_settings().config.publish_output),
                        "publish_output_no_suggestions": bool(get_settings().pr_reviewer.get('publish_output_no_suggestions', True)),
                    },
                )
                get_settings().data = {"artifact": pr_review}
                return

            # publish the review
            if get_settings().pr_reviewer.persistent_comment and not self.incremental.is_incremental:
                final_update_message = get_settings().pr_reviewer.final_update_message
                self.git_provider.publish_persistent_comment(pr_review,
                                                            initial_header=f"{PRReviewHeader.REGULAR.value} 🔍",
                                                            update_header=True,
                                                            final_update_message=final_update_message, )
            else:
                self.git_provider.publish_comment(pr_review)

            get_logger().info(
                "PR review published",
                artifact={
                    "pr_url": self.pr_url,
                    "persistent_comment": bool(get_settings().pr_reviewer.persistent_comment),
                    "incremental": bool(self.incremental.is_incremental),
                    "jira_ticket_count": len(self.vars.get("related_tickets", [])),
                    "gitnexus_status": self.gitnexus_context_status,
                    "gitnexus_mode": self.gitnexus_context_mode,
                },
            )
            self.git_provider.remove_initial_comment()
        except Exception as e:
            get_logger().error(f"Failed to review PR: {e}")

    def _should_publish_review_no_suggestions(self, pr_review: str) -> bool:
        return get_settings().pr_reviewer.get('publish_output_no_suggestions', True) or "No major issues detected" not in pr_review

    async def _prepare_prediction(self, model: str) -> None:
        self.patches_diff = get_pr_diff(self.git_provider,
                                        self.token_handler,
                                        model,
                                        add_line_numbers_to_hunks=True,
                                        disable_extra_lines=False,)

        if self.patches_diff:
            get_logger().debug(f"PR diff", diff=self.patches_diff)
            self.prediction = await self._get_prediction(model)
        else:
            get_logger().warning(f"Empty diff for PR: {self.pr_url}")
            self.prediction = None

    async def _get_prediction(self, model: str) -> str:
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff
        gitnexus_context = await self._get_gitnexus_context()
        if gitnexus_context:
            self.gitnexus_context_status = "used"
            gitnexus_config = self._get_gitnexus_config() or {}
            self.gitnexus_context_mode = gitnexus_config.get("mode", "")
        else:
            self.gitnexus_context_status = "not_used"
            self.gitnexus_context_mode = ""
        variables["context_usage_summary"] = self._get_context_usage_summary_text()
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().pr_review_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_review_prompt.user).render(variables)
        if gitnexus_context:
            user_prompt += (
                "\n\nAdditional repository context from GitNexus MCP:\n"
                f"{gitnexus_context}\n"
                "Use this context only when it is relevant to concrete issues in the diff."
            )

        mcp_config = self._get_mcp_config()
        if mcp_config:
            from pr_agent.algo.mcp_handler import MCPHandler

            mcp_handler = MCPHandler(mcp_config["command"], mcp_config["args"])
            async with mcp_handler as handler:
                tools = await handler.get_openai_tools()
                
                # Initial call
                response, finish_reason, tool_calls = await self.ai_handler.chat_completion(
                    model=model,
                    temperature=get_settings().config.temperature,
                    system=system_prompt,
                    user=user_prompt,
                    tools=tools
                )

                if finish_reason == "tool_calls" and tool_calls:
                    # Execute MCP tools
                    for tool_call in tool_calls:
                        tool_name = tool_call.function.name
                        tool_args = json.loads(tool_call.function.arguments or "{}")
                        get_logger().info(f"Executing tool: {tool_name} with args {tool_args}")
                        tool_result = await handler.call_tool(tool_name, tool_args)
                        
                        # Add tool result to user prompt
                        user_prompt += f"\n\nTool Result ({tool_name}): {json.dumps(tool_result)}"
                    
                    # Re-run completion with tool results
                    response, finish_reason, _ = await self.ai_handler.chat_completion(
                        model=model,
                        temperature=get_settings().config.temperature,
                        system=system_prompt,
                        user=user_prompt,
                        tools=None # Don't allow recursive tool calls for now
                    )
                return response
        else:
            response, finish_reason, _ = await self.ai_handler.chat_completion(
                model=model,
                temperature=get_settings().config.temperature,
                system=system_prompt,
                user=user_prompt
            )
            return response

    def _get_mcp_config(self) -> Optional[Dict[str, Any]]:
        mcp_config = get_settings().get("mcp", None)
        if not mcp_config:
            return None

        if not mcp_config.get("enabled", False):
            return None

        command = mcp_config.get("command")
        args = mcp_config.get("args", [])
        if not command:
            raise ValueError("MCP is enabled but mcp.command is not configured")
        if not isinstance(args, list):
            raise ValueError("MCP mcp.args must be a list")

        return {"command": command, "args": args}

    def _get_gitnexus_config(self) -> Optional[Dict[str, Any]]:
        gitnexus_config = get_settings().get("gitnexus", None)
        if not gitnexus_config:
            return None

        if not gitnexus_config.get("enabled", False):
            return None

        command = gitnexus_config.get("command")
        args = gitnexus_config.get("args", [])
        if not command:
            raise ValueError("GitNexus is enabled but gitnexus.command is not configured")
        if not isinstance(args, list):
            raise ValueError("GitNexus gitnexus.args must be a list")

        return {
            "command": command,
            "args": args,
            "mode": gitnexus_config.get("mode", "detect_changes"),
            "repo": gitnexus_config.get("repo", ""),
            "base_ref": gitnexus_config.get("base_ref", ""),
            "scope": gitnexus_config.get("scope", "compare"),
            "index_ref": gitnexus_config.get("index_ref", ""),
            "index_commit": gitnexus_config.get("index_commit", ""),
            "max_queries": gitnexus_config.get("max_queries", 5),
            "max_symbols_per_file": gitnexus_config.get("max_symbols_per_file", 2),
            "drift_check": gitnexus_config.get("drift_check", False),
            "drift_repo_path": gitnexus_config.get("drift_repo_path", ""),
            "drift_target_ref": gitnexus_config.get("drift_target_ref", ""),
            "drift_max_commits": gitnexus_config.get("drift_max_commits", 20),
            "drift_policy": gitnexus_config.get("drift_policy", "warn"),
        }

    async def _get_gitnexus_context(self) -> str:
        gitnexus_config = self._get_gitnexus_config()
        if not gitnexus_config:
            return ""

        from pr_agent.algo.mcp_handler import MCPHandler

        try:
            mcp_handler = MCPHandler(gitnexus_config["command"], gitnexus_config["args"])
            async with mcp_handler as handler:
                tools = await handler.get_openai_tools()
                tool_names = {tool["function"]["name"] for tool in tools}
                mode = gitnexus_config["mode"]
                if mode == "detect_changes":
                    return await self._get_gitnexus_detect_changes_context(handler, gitnexus_config, tool_names)
                if mode == "base_context":
                    return await self._get_gitnexus_base_context(handler, gitnexus_config, tool_names)
                raise ValueError(f"Unsupported GitNexus mode: {mode}")
        except Exception as e:
            if get_settings().get("gitnexus.fail_on_error", False):
                raise
            get_logger().warning(f"Failed to fetch GitNexus context: {e}")
            return ""

    async def _get_gitnexus_detect_changes_context(self, handler, gitnexus_config: Dict[str, Any],
                                                  tool_names: set[str]) -> str:
        if "detect_changes" not in tool_names:
            raise ValueError("GitNexus MCP server does not expose the detect_changes tool")

        tool_args = {"scope": gitnexus_config["scope"]}
        if gitnexus_config["repo"]:
            tool_args["repo"] = gitnexus_config["repo"]
        base_ref = self._get_gitnexus_base_ref(gitnexus_config["base_ref"])
        if base_ref:
            tool_args["base_ref"] = base_ref

        get_logger().info(f"Fetching GitNexus detect_changes context with args {tool_args}")
        context = await handler.call_tool("detect_changes", tool_args)
        return self._format_gitnexus_context(context)

    async def _get_gitnexus_base_context(self, handler, gitnexus_config: Dict[str, Any], tool_names: set[str]) -> str:
        missing_tools = {"query", "context", "impact"} - tool_names
        if missing_tools:
            raise ValueError(f"GitNexus MCP server does not expose required tools: {sorted(missing_tools)}")

        diff_files = self.git_provider.get_diff_files()
        candidates = self._build_gitnexus_base_context_candidates(
            diff_files,
            max_files=gitnexus_config["max_queries"],
            max_symbols_per_file=gitnexus_config["max_symbols_per_file"],
        )
        if not candidates:
            return ""

        repo = gitnexus_config["repo"]
        sections = [self._format_gitnexus_base_context_header(gitnexus_config)]
        drift_analysis = self._get_gitnexus_drift_analysis(gitnexus_config, candidates)
        if drift_analysis:
            sections.append(self._format_gitnexus_drift_analysis(drift_analysis))
            if gitnexus_config["drift_policy"] == "skip_on_overlap" and drift_analysis["confidence"] == "LOW":
                sections.append(
                    "GitNexus base context queries were skipped because snapshot drift overlaps this PR. "
                    "Use only the PR diff and the drift analysis above."
                )
                return "\n\n".join(sections)

        for candidate in candidates:
            sections.append(self._format_gitnexus_candidate_header(candidate))

            should_query_symbols = candidate["edit_type"] not in {EDIT_TYPE.ADDED.name, "ADDED"}
            for symbol in candidate["symbols"] if should_query_symbols else []:
                context_args = {"name": symbol, "file_path": candidate["base_file_path"]}
                impact_args = {
                    "target": symbol,
                    "file_path": candidate["base_file_path"],
                    "direction": "upstream",
                    "maxDepth": 2,
                }
                if repo:
                    context_args["repo"] = repo
                    impact_args["repo"] = repo

                get_logger().info(f"Fetching GitNexus base context for {symbol} in {candidate['base_file_path']}")
                context = self._format_gitnexus_context(await handler.call_tool("context", context_args))
                impact = self._format_gitnexus_context(await handler.call_tool("impact", impact_args))
                sections.append(f"Symbol `{symbol}` context from indexed snapshot:\n{context}")
                sections.append(f"Symbol `{symbol}` impact from indexed snapshot:\n{impact}")

            query_args = {"query": candidate["query"], "limit": 3}
            if repo:
                query_args["repo"] = repo
            get_logger().info(f"Fetching GitNexus base query context with args {query_args}")
            query = self._format_gitnexus_context(await handler.call_tool("query", query_args))
            sections.append(f"Related indexed-snapshot query `{candidate['query']}`:\n{query}")

        return "\n\n".join(sections)

    def _get_gitnexus_drift_analysis(self, gitnexus_config: Dict[str, Any],
                                     candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not gitnexus_config["drift_check"]:
            return None
        if not gitnexus_config["index_commit"] or not gitnexus_config["drift_repo_path"]:
            return {
                "status": "skipped",
                "reason": "drift_check requires gitnexus.index_commit and gitnexus.drift_repo_path",
            }

        target_ref = (
            gitnexus_config["drift_target_ref"] or
            self._get_gitnexus_base_ref(gitnexus_config["base_ref"])
        )
        if not target_ref:
            return {
                "status": "skipped",
                "reason": "drift_check could not resolve a target/base ref",
            }

        drift_files_output = self._run_gitnexus_git_command(
            gitnexus_config["drift_repo_path"],
            ["diff", "--name-only", gitnexus_config["index_commit"], target_ref],
        )
        drift_patch = self._run_gitnexus_git_command(
            gitnexus_config["drift_repo_path"],
            ["diff", "--unified=0", gitnexus_config["index_commit"], target_ref],
        )
        commits_output = self._run_gitnexus_git_command(
            gitnexus_config["drift_repo_path"],
            ["log", "--oneline", f"--max-count={gitnexus_config['drift_max_commits']}",
             f"{gitnexus_config['index_commit']}..{target_ref}"],
        )

        drift_files = {line.strip().replace("\\", "/") for line in drift_files_output.splitlines() if line.strip()}
        pr_files = {candidate["filename"].replace("\\", "/") for candidate in candidates if candidate["filename"]}
        pr_base_files = {
            candidate["base_file_path"].replace("\\", "/")
            for candidate in candidates
            if candidate["base_file_path"]
        }
        pr_symbols = {
            symbol
            for candidate in candidates
            for symbol in candidate["symbols"]
        }
        drift_symbols = set(self._extract_gitnexus_candidate_symbols(drift_patch, "", 200))
        exact_file_overlap = sorted((pr_files | pr_base_files) & drift_files)
        related_path_overlap = self._find_gitnexus_related_paths(pr_files | pr_base_files, drift_files)
        symbol_overlap = sorted(pr_symbols & drift_symbols)

        confidence = "HIGH"
        if related_path_overlap:
            confidence = "MEDIUM"
        if exact_file_overlap or symbol_overlap:
            confidence = "LOW"

        return {
            "status": "analyzed",
            "confidence": confidence,
            "index_commit": gitnexus_config["index_commit"],
            "target_ref": target_ref,
            "drift_repo_path": gitnexus_config["drift_repo_path"],
            "drift_files_count": len(drift_files),
            "pr_files_count": len(pr_files),
            "drift_symbols_count": len(drift_symbols),
            "pr_symbols_count": len(pr_symbols),
            "exact_file_overlap": exact_file_overlap[:20],
            "related_path_overlap": related_path_overlap[:20],
            "symbol_overlap": symbol_overlap[:20],
            "recent_commits": [line for line in commits_output.splitlines() if line][:gitnexus_config["drift_max_commits"]],
        }

    @staticmethod
    def _run_gitnexus_git_command(cwd: str, args: List[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.stdout

    @staticmethod
    def _find_gitnexus_related_paths(pr_paths: set[str], drift_paths: set[str]) -> List[str]:
        related = []
        for pr_path in sorted(pr_paths):
            pr_parts = [part for part in pr_path.split("/") if part]
            if not pr_parts:
                continue
            pr_dir = "/".join(pr_parts[:-1])
            pr_top = pr_parts[0]
            for drift_path in sorted(drift_paths):
                drift_parts = [part for part in drift_path.split("/") if part]
                if not drift_parts:
                    continue
                drift_dir = "/".join(drift_parts[:-1])
                if pr_dir and pr_dir == drift_dir:
                    related.append(f"{pr_path} <-> {drift_path}")
                elif pr_top == drift_parts[0] and pr_top not in {pr_path, drift_path}:
                    related.append(f"{pr_path} <-> {drift_path}")
                if len(related) >= 20:
                    return related
        return related

    @staticmethod
    def _format_gitnexus_drift_analysis(drift_analysis: Dict[str, Any]) -> str:
        if drift_analysis["status"] == "skipped":
            return f"GitNexus snapshot drift analysis: skipped. Reason: {drift_analysis['reason']}"

        lines = [
            "GitNexus snapshot drift analysis:",
            f"- Indexed commit: {drift_analysis['index_commit']}",
            f"- Target/base ref: {drift_analysis['target_ref']}",
            f"- Drift repo path: {drift_analysis['drift_repo_path']}",
            f"- Confidence: {drift_analysis['confidence']}",
            f"- Drift files: {drift_analysis['drift_files_count']}; PR files: {drift_analysis['pr_files_count']}",
            f"- Drift symbols: {drift_analysis['drift_symbols_count']}; PR symbols: {drift_analysis['pr_symbols_count']}",
        ]
        if drift_analysis["exact_file_overlap"]:
            lines.append("- Exact file overlap: " + ", ".join(drift_analysis["exact_file_overlap"]))
        if drift_analysis["related_path_overlap"]:
            lines.append("- Related path overlap: " + "; ".join(drift_analysis["related_path_overlap"]))
        if drift_analysis["symbol_overlap"]:
            lines.append("- Symbol overlap: " + ", ".join(drift_analysis["symbol_overlap"]))
        if drift_analysis["recent_commits"]:
            lines.append("- Recent commits between indexed snapshot and target/base ref:")
            lines.extend([f"  - {commit}" for commit in drift_analysis["recent_commits"]])

        if drift_analysis["confidence"] == "HIGH":
            lines.append("Guidance: no direct path or symbol overlap was found; GitNexus snapshot context is likely reliable.")
        elif drift_analysis["confidence"] == "MEDIUM":
            lines.append("Guidance: nearby paths changed after the GitNexus snapshot; use context as helpful but possibly stale.")
        else:
            lines.append("Guidance: direct file or symbol overlap was found; treat GitNexus context as stale for the overlapping areas.")

        return "\n".join(lines)

    def _get_gitnexus_base_ref(self, configured_base_ref: str) -> str:
        if configured_base_ref:
            return configured_base_ref

        git_provider = getattr(self, "git_provider", None)
        provider_pr = getattr(git_provider, "pr", None)
        provider_mr = getattr(git_provider, "mr", None)
        for pr_object in (provider_pr, provider_mr):
            target_branch = getattr(pr_object, "target_branch", "")
            if target_branch:
                return target_branch

        settings = get_settings()
        config = getattr(settings, "config", None)
        git_provider_name = getattr(config, "git_provider", "")
        if not git_provider_name and callable(getattr(settings, "get", None)):
            git_provider_name = settings.get("config.git_provider", "")
        if git_provider_name == "local":
            return self.pr_url

        return ""

    def _format_gitnexus_base_context_header(self, gitnexus_config: Dict[str, Any]) -> str:
        target_ref = self._get_gitnexus_base_ref(gitnexus_config["base_ref"])
        metadata = [
            "GitNexus context mode: base_context.",
            "Source: indexed stable/base snapshot, not the PR source branch after this change.",
        ]
        if gitnexus_config["index_ref"]:
            metadata.append(f"Indexed ref: {gitnexus_config['index_ref']}.")
        if gitnexus_config["index_commit"]:
            metadata.append(f"Indexed commit: {gitnexus_config['index_commit']}.")
        if target_ref:
            metadata.append(f"PR target/base ref: {target_ref}.")
        metadata.extend([
            "Use the PR diff as the source of truth for new or changed code.",
            "GitNexus may not include files, symbols, renames, or call relationships added after the indexed snapshot.",
            "Do not treat missing GitNexus results as evidence that a PR symbol is unused, invalid, or absent.",
            "Use GitNexus only to understand existing base-branch relationships and likely affected areas.",
        ])
        return "\n".join(metadata)

    @staticmethod
    def _format_gitnexus_candidate_header(candidate: Dict[str, Any]) -> str:
        symbols = ", ".join(candidate["symbols"]) if candidate["symbols"] else "none"
        return (
            f"Changed file: {candidate['filename']}\n"
            f"Base snapshot lookup path: {candidate['base_file_path']}\n"
            f"Edit type: {candidate['edit_type']}\n"
            f"Candidate symbols from PR diff: {symbols}"
        )

    def _build_gitnexus_base_context_candidates(self, diff_files: List[Any], max_files: int,
                                                max_symbols_per_file: int) -> List[Dict[str, Any]]:
        candidates = []
        for diff_file in diff_files[:max_files]:
            edit_type = getattr(diff_file, "edit_type", EDIT_TYPE.UNKNOWN)
            edit_type_name = edit_type.name if isinstance(edit_type, EDIT_TYPE) else str(edit_type)
            filename = getattr(diff_file, "filename", "")
            old_filename = getattr(diff_file, "old_filename", "") or filename
            patch = getattr(diff_file, "patch", "") or ""
            base_file_path = old_filename if edit_type == EDIT_TYPE.DELETED else filename
            symbols = self._extract_gitnexus_candidate_symbols(patch, filename, max_symbols_per_file)
            candidates.append({
                "filename": filename,
                "base_file_path": base_file_path,
                "edit_type": edit_type_name,
                "symbols": symbols,
                "query": self._build_gitnexus_query(filename, old_filename, patch, symbols),
            })

        return candidates

    @staticmethod
    def _extract_gitnexus_candidate_symbols(patch: str, filename: str, max_symbols: int) -> List[str]:
        symbols = OrderedDict()
        for line in patch.splitlines():
            if line.startswith("@@"):
                header = line.split("@@", 2)[-1].strip()
                match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", header)
                if match:
                    symbols[match.group(1)] = None
                elif header:
                    header_tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", header)
                    if header_tokens:
                        symbols[header_tokens[-1]] = None
                if len(symbols) >= max_symbols:
                    return list(symbols.keys())[:max_symbols]

            if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                continue
            for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", line):
                if token.isupper() or token in PRReviewer.GITNEXUS_COMMON_SYMBOLS:
                    continue
                symbols[token] = None
                if len(symbols) >= max_symbols:
                    return list(symbols.keys())

        stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", "_")
        if stem:
            symbols[stem] = None
        return list(symbols.keys())[:max_symbols]

    @staticmethod
    def _build_gitnexus_query(filename: str, old_filename: str, patch: str, symbols: List[str]) -> str:
        added_words = []
        for line in patch.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", line):
                if token.isupper() or token in PRReviewer.GITNEXUS_COMMON_SYMBOLS:
                    continue
                if token not in added_words:
                    added_words.append(token)
                if len(added_words) >= 8:
                    break
            if len(added_words) >= 8:
                break

        parts = [filename]
        if old_filename and old_filename != filename:
            parts.append(old_filename)
        parts.extend(symbols)
        parts.extend(added_words)
        return " ".join(parts[:16])

    @staticmethod
    def _format_gitnexus_context(context: Any) -> str:
        if isinstance(context, list):
            text_parts = []
            for item in context:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    text_parts.append(item["text"])
                else:
                    text_parts.append(json.dumps(item, ensure_ascii=False))
            return "\n".join(text_parts)

        return str(context)

    def _prepare_pr_review(self) -> str:
        """
        Prepare the PR review by processing the AI prediction and generating a markdown-formatted text that summarizes
        the feedback.
        """
        first_key = 'review'
        last_key = 'security_concerns'
        data = load_yaml(self.prediction.strip(),
                         keys_fix_yaml=["ticket_compliance_check", "estimated_effort_to_review_[1-5]:", "security_concerns:", "key_issues_to_review:",
                                        "relevant_file:", "relevant_line:", "suggestion:"],
                         first_key=first_key, last_key=last_key)
        github_action_output(data, 'review')

        if 'review' not in data:
            get_logger().exception("Failed to parse review data", artifact={"data": data})
            return ""

        # move data['review'] 'key_issues_to_review' key to the end of the dictionary
        if 'key_issues_to_review' in data['review']:
            key_issues_to_review = data['review'].pop('key_issues_to_review')
            data['review']['key_issues_to_review'] = key_issues_to_review

        incremental_review_markdown_text = None
        # Add incremental review section
        if self.incremental.is_incremental:
            last_commit_url = f"{self.git_provider.get_pr_url()}/commits/" \
                              f"{self.git_provider.incremental.first_new_commit_sha}"
            incremental_review_markdown_text = f"Starting from commit {last_commit_url}"

        markdown_text = convert_to_markdown_v2(data, self.git_provider.is_supported("gfm_markdown"),
                                            incremental_review_markdown_text,
                                               git_provider=self.git_provider,
                                               files=self.git_provider.get_diff_files())

        # Add help text if gfm_markdown is supported
        if self.git_provider.is_supported("gfm_markdown") and get_settings().pr_reviewer.enable_help_text:
            markdown_text += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
            markdown_text += HelpMessage.get_review_usage_guide()
            markdown_text += "\n</details>\n"

        # Output the relevant configurations if enabled
        if get_settings().get('config', {}).get('output_relevant_configurations', False):
            markdown_text += show_relevant_configurations(relevant_section='pr_reviewer')

        # Add custom labels from the review prediction (effort, security)
        self.set_review_labels(data)

        if markdown_text == None or len(markdown_text) == 0:
            markdown_text = ""

        markdown_text = self._get_review_context_summary_markdown() + markdown_text
        return markdown_text

    def _get_context_usage_summary_text(self) -> str:
        related_tickets = self.vars.get("related_tickets", []) if hasattr(self, "vars") else []
        ticket_count = len(related_tickets)
        jira_text = f"Jira ticket context: used, {ticket_count} ticket(s)." if ticket_count else "Jira ticket context: not used."
        gitnexus_mode = getattr(self, "gitnexus_context_mode", "")
        if getattr(self, "gitnexus_context_status", "not_used") == "used":
            gitnexus_text = f"GitNexus context: used, mode={gitnexus_mode or 'unknown'}."
        else:
            gitnexus_text = "GitNexus context: not used."
        return "\n".join([
            jira_text,
            gitnexus_text,
            "The final review output must disclose this context usage to the reviewer.",
        ])

    def _get_review_context_summary_markdown(self) -> str:
        related_tickets = self.vars.get("related_tickets", []) if hasattr(self, "vars") else []
        ticket_count = len(related_tickets)
        suggested_title_line = self._get_suggested_mr_title_line(related_tickets)
        if ticket_count:
            jira_line = f"Jira：已使用 {ticket_count} 筆 ticket"
        else:
            jira_line = "Jira：未使用"

        if getattr(self, "gitnexus_context_status", "not_used") == "used":
            mode = getattr(self, "gitnexus_context_mode", "") or "unknown"
            gitnexus_line = f"GitNexus：已使用 {mode}"
        else:
            gitnexus_line = "GitNexus：未使用"

        return (
            "### 本次 Review 使用的上下文\n\n"
            f"{suggested_title_line}\n"
            f"- {jira_line}\n"
            f"- {gitnexus_line}\n\n"
        )

    def _get_suggested_mr_title_line(self, related_tickets: List[Dict[str, Any]]) -> str:
        ticket_ids = [
            str(ticket.get("ticket_id")).strip()
            for ticket in related_tickets
            if isinstance(ticket, dict) and ticket.get("ticket_id")
        ]
        if not ticket_ids:
            return "- 建議 MR Title：未產生（未找到 Jira ticket）"

        title = str(self.vars.get("title", "") if hasattr(self, "vars") else "").strip()
        if not title and hasattr(self, "git_provider"):
            try:
                title = str(self.git_provider.get_title()).strip()
            except Exception:
                title = ""
        title = re.sub(r"^\[[A-Z][A-Z0-9]{1,9}-\d{1,7}\]\s*:?\s*", "", title)
        title = title or "請補上 MR 摘要"
        return f"- 建議 MR Title：`[{ticket_ids[0]}]: {title}`"

    def _get_user_answers(self) -> Tuple[str, str]:
        """
        Retrieves the question and answer strings from the discussion messages related to a pull request.

        Returns:
            A tuple containing the question and answer strings.
        """
        question_str = ""
        answer_str = ""

        if self.is_answer:
            discussion_messages = self.git_provider.get_issue_comments()

            for message in discussion_messages.reversed:
                if "Questions to better understand the PR:" in message.body:
                    question_str = message.body
                elif '/answer' in message.body:
                    answer_str = message.body

                if answer_str and question_str:
                    break

        return question_str, answer_str

    def _get_previous_review_comment(self):
        """
        Get the previous review comment if it exists.
        """
        try:
            if hasattr(self.git_provider, "get_previous_review"):
                return self.git_provider.get_previous_review(
                    full=not self.incremental.is_incremental,
                    incremental=self.incremental.is_incremental,
                )
        except Exception as e:
            get_logger().exception(f"Failed to get previous review comment, error: {e}")

    def _remove_previous_review_comment(self, comment):
        """
        Remove the previous review comment if it exists.
        """
        try:
            if comment:
                self.git_provider.remove_comment(comment)
        except Exception as e:
            get_logger().exception(f"Failed to remove previous review comment, error: {e}")

    def _can_run_incremental_review(self) -> bool:
        """
        Checks if we can run incremental review according the various configurations and previous review.
        """
        # checking if running is auto mode but there are no new commits
        if self.is_auto and not self.incremental.first_new_commit_sha:
            get_logger().info(f"Incremental review is enabled for {self.pr_url} but there are no new commits")
            return False

        if not hasattr(self.git_provider, "get_incremental_commits"):
            get_logger().info(f"Incremental review is not supported for {get_settings().config.git_provider}")
            return False
        # checking if there are enough commits to start the review
        num_new_commits = len(self.incremental.commits_range)
        num_commits_threshold = get_settings().pr_reviewer.minimal_commits_for_incremental_review
        not_enough_commits = num_new_commits < num_commits_threshold
        # checking if the commits are not too recent to start the review
        recent_commits_threshold = datetime.datetime.now() - datetime.timedelta(
            minutes=get_settings().pr_reviewer.minimal_minutes_for_incremental_review
        )
        last_seen_commit_date = (
            self.incremental.last_seen_commit.commit.author.date if self.incremental.last_seen_commit else None
        )
        all_commits_too_recent = (
            last_seen_commit_date > recent_commits_threshold if self.incremental.last_seen_commit else False
        )
        # check all the thresholds or just one to start the review
        condition = any if get_settings().pr_reviewer.require_all_thresholds_for_incremental_review else all
        if condition((not_enough_commits, all_commits_too_recent)):
            get_logger().info(
                f"Incremental review is enabled for {self.pr_url} but didn't pass the threshold check to run:"
                f"\n* Number of new commits = {num_new_commits} (threshold is {num_commits_threshold})"
                f"\n* Last seen commit date = {last_seen_commit_date} (threshold is {recent_commits_threshold})"
            )
            return False
        return True

    def set_review_labels(self, data):
        if not get_settings().config.publish_output:
            return

        if not get_settings().pr_reviewer.require_estimate_effort_to_review:
            get_settings().pr_reviewer.enable_review_labels_effort = False # we did not generate this output
        if not get_settings().pr_reviewer.require_security_review:
            get_settings().pr_reviewer.enable_review_labels_security = False # we did not generate this output

        if (get_settings().pr_reviewer.enable_review_labels_security or
                get_settings().pr_reviewer.enable_review_labels_effort):
            try:
                review_labels = []
                if get_settings().pr_reviewer.enable_review_labels_effort:
                    estimated_effort = data['review']['estimated_effort_to_review_[1-5]']
                    estimated_effort_number = 0
                    if isinstance(estimated_effort, str):
                        try:
                            estimated_effort_number = int(estimated_effort.split(',')[0])
                        except ValueError:
                            get_logger().warning(f"Invalid estimated_effort value: {estimated_effort}")
                    elif isinstance(estimated_effort, int):
                        estimated_effort_number = estimated_effort
                    else:
                        get_logger().warning(f"Unexpected type for estimated_effort: {type(estimated_effort)}")
                    if 1 <= estimated_effort_number <= 5:  # 1, because ...
                        review_labels.append(f'Review effort {estimated_effort_number}/5')
                if get_settings().pr_reviewer.enable_review_labels_security and get_settings().pr_reviewer.require_security_review:
                    security_concerns = data['review']['security_concerns']  # yes, because ...
                    security_concerns_bool = 'yes' in security_concerns.lower() or 'true' in security_concerns.lower()
                    if security_concerns_bool:
                        review_labels.append('Possible security concern')

                current_labels = self.git_provider.get_pr_labels(update=True)
                if not current_labels:
                    current_labels = []
                get_logger().debug(f"Current labels:\n{current_labels}")
                if current_labels:
                    current_labels_filtered = [label for label in current_labels if
                                               not label.lower().startswith('review effort') and not label.lower().startswith(
                                                   'possible security concern')]
                else:
                    current_labels_filtered = []
                new_labels = review_labels + current_labels_filtered
                if (current_labels or review_labels) and sorted(new_labels) != sorted(current_labels):
                    get_logger().info(f"Setting review labels:\n{review_labels + current_labels_filtered}")
                    self.git_provider.publish_labels(new_labels)
                else:
                    get_logger().info(f"Review labels are already set:\n{review_labels + current_labels_filtered}")
            except Exception as e:
                get_logger().error(f"Failed to set review labels, error: {e}")

    def auto_approve_logic(self):
        """
        Auto-approve a pull request if it meets the conditions for auto-approval.
        """
        if get_settings().config.enable_auto_approval:
            is_auto_approved = self.git_provider.auto_approve()
            if is_auto_approved:
                get_logger().info("Auto-approved PR")
                self.git_provider.publish_comment("Auto-approved PR")
        else:
            get_logger().info("Auto-approval option is disabled")
            self.git_provider.publish_comment("Auto-approval option for PR-Agent is disabled. "
                                              "You can enable it via a [configuration file](https://github.com/Codium-ai/pr-agent/blob/main/docs/REVIEW.md#auto-approval-1)")
