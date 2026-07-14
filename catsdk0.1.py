#!/usr/bin/env python3
# ac's catsdk 0.2.0a - Claude Code-inspired fork
# Features from leaked Claude Code: tool system, coordinator, plugins, sessions, cost tracking, CLAUDE.md memory

import tkinter as tk
from tkinter import filedialog
import json
import requests
import threading
import time
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import importlib.util
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, fields
from typing import Optional, List, Dict, Any, Callable, Tuple
from collections import deque
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Configuration ────────────────────────────────────────────────────────────

class Config:
    def __init__(self):
        self.lm_studio_url = "http://localhost:1234/v1"
        self.model = "local-model"
        self.temperature = 0.0
        self.max_tokens = 2048
        self.continuous_limit = 0
        self.continuous_mode = False
        self.debug_mode = False
        self.restrict_to_workspace = True
        self.execute_local_commands = False
        self._script_dir = Path(__file__).parent.resolve()
        self.workspace_path = str(self._script_dir / "auto_gpt_workspace")
        self.ai_name = "CatGPT"
        self.ai_role = "a helpful AI assistant"
        self.ai_goals = []
        self.api_key = "not-needed"
        self.context_window = 4096
        self.memory_count = 20
        self.smart_context = True
        self.plugins_dir = str(self._script_dir / "plugins")
        self.sessions_dir = str(self._script_dir / "sessions")
        self.max_parallel_tools = 4
        self.coordinator_mode = False
        self.max_sub_agents = 3
        self.track_costs = True
        self.cost_per_input_token = 0.0
        self.cost_per_output_token = 0.0
        for d in [self.workspace_path, self.plugins_dir, self.sessions_dir]:
            os.makedirs(d, exist_ok=True)

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)


# ─── Cost Tracker (from Claude Code) ──────────────────────────────────────────

@dataclass
class CostTracker:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    session_input_tokens: int = 0
    session_output_tokens: int = 0
    session_cost: float = 0.0
    history: list = field(default_factory=list)

    def add_usage(self, input_tokens: int, output_tokens: int, cost_per_input: float = 0.0, cost_per_output: float = 0.0):
        cost = (input_tokens * cost_per_input) + (output_tokens * cost_per_output)
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost += cost
        self.session_input_tokens += input_tokens
        self.session_output_tokens += output_tokens
        self.session_cost += cost
        self.history.append({
            "timestamp": datetime.now().isoformat(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost
        })

    def reset_session(self):
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cost = 0.0

    def summary(self) -> str:
        return (
            f"Session: {self.session_input_tokens} in / {self.session_output_tokens} out"
            f" (${self.session_cost:.6f})\n"
            f"Total: {self.total_input_tokens} in / {self.total_output_tokens} out"
            f" (${self.total_cost:.6f})"
        )


# ─── CLAUDE.md Memory (from Claude Code) ──────────────────────────────────────

class ClaudeMemory:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        self.claude_md_path = os.path.join(workspace_path, "CLAUDE.md")
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.exists(self.claude_md_path):
            with open(self.claude_md_path, "w") as f:
                f.write("# CLAUDE.md - Persistent Project Memory\n\n")
                f.write("## Project Context\n\n")
                f.write("## Conventions\n\n")
                f.write("## Notes\n\n")

    def read(self) -> str:
        try:
            with open(self.claude_md_path, "r") as f:
                return f.read()
        except Exception:
            return ""

    def append(self, section: str, content: str):
        existing = self.read()
        marker = f"## {section}"
        if marker in existing:
            existing += f"\n- {content}\n"
        else:
            existing += f"\n{marker}\n\n- {content}\n"
        with open(self.claude_md_path, "w") as f:
            f.write(existing)

    def write_section(self, section: str, content: str):
        existing = self.read()
        marker = f"## {section}"
        body = content.lstrip("\n")
        if body and not body.endswith("\n"):
            body += "\n"
        pattern = rf"(## {re.escape(section)}\n)(.*?)(?=\n## |\Z)"
        if re.search(pattern, existing, re.DOTALL):
            def _repl(m):
                return m.group(1) + body
            updated = re.sub(pattern, _repl, existing, count=1, flags=re.DOTALL)
            with open(self.claude_md_path, "w") as f:
                f.write(updated)
        else:
            with open(self.claude_md_path, "a") as f:
                f.write(f"\n{marker}\n{body}")

    def get_section(self, section: str) -> str:
        content = self.read()
        pattern = rf"## {re.escape(section)}\n(.*?)(?:\n## |\Z)"
        m = re.search(pattern, content, re.DOTALL)
        return m.group(1).strip() if m else ""


# ─── Tool System (Claude Code Architecture) ───────────────────────────────────

class ToolPermission(Enum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    PROMPT = "prompt"

@dataclass
class ToolSpec:
    name: str
    description: str
    args_schema: Dict[str, Any]
    permission: ToolPermission = ToolPermission.ALLOWED
    category: str = "general"
    parallel_safe: bool = True
    cost_estimate: int = 0

class ToolContext:
    def __init__(self, workspace_path: str, config: 'Config'):
        self.workspace_path = workspace_path
        self.config = config
        self.last_results: Dict[str, str] = {}
        self._results_lock = threading.Lock()

    def _safe_path(self, rel: str) -> Optional[Path]:
        """Resolve rel under workspace; return None if it escapes."""
        try:
            ws = Path(self.workspace_path).resolve()
            if not rel or rel in (".", ""):
                return ws
            p = Path(rel)
            target = p.resolve() if p.is_absolute() else (ws / rel).resolve()
            if target == ws or ws in target.parents:
                return target
        except (OSError, ValueError, RuntimeError):
            pass
        return None

    def read_file(self, path: str) -> str:
        target = self._safe_path(path)
        if target is None:
            return f"Error: Access denied. Use relative paths within workspace ({self.workspace_path})"
        try:
            if not target.is_file():
                return f"Error: Not a file: {path}"
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            return f"Error: {e}"

    def write_file(self, path: str, content: str) -> str:
        target = self._safe_path(path)
        if target is None:
            return f"Error: Access denied. Use relative paths within workspace ({self.workspace_path})"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {path} ({target})"
        except Exception as e:
            return f"Error: {e}"

    def execute_bash(self, command: str) -> str:
        if not self.config.execute_local_commands:
            return "Shell execution disabled"
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=30,
                cwd=self.workspace_path,
            )
            out = result.stdout or ""
            err = result.stderr or ""
            combined = out if out else err
            if out and err:
                combined = out + ("\n" + err if err else "")
            return combined[:2000] if combined else f"(exit {result.returncode})"
        except subprocess.TimeoutExpired:
            return "Command timed out"
        except Exception as e:
            return f"Error: {e}"

    def list_files(self, path: str = "") -> str:
        target = self._safe_path(path or ".")
        if target is None:
            return "Error: Access denied"
        if not target.exists():
            return f"Error: Path does not exist: {path}"
        if not target.is_dir():
            return f"Error: Not a directory: {path}"
        try:
            ws = Path(self.workspace_path).resolve()
            entries = []
            for root, dirs, files in os.walk(target):
                root_p = Path(root)
                rel = root_p.relative_to(ws)
                rel_s = "" if str(rel) == "." else str(rel)
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for d in dirs:
                    entries.append(os.path.join(rel_s, d) + "/" if rel_s else f"{d}/")
                for f in sorted(files):
                    if f.startswith("."):
                        continue
                    entries.append(os.path.join(rel_s, f) if rel_s else f)
            return "\n".join(sorted(entries)) if entries else "Empty"
        except Exception as e:
            return f"Error: {e}"

class ToolRegistry:
    def __init__(self, context: ToolContext):
        self.context = context
        self._tools: Dict[str, ToolSpec] = {}
        self._handlers: Dict[str, Callable] = {}
        self._register_defaults()

    def _register_defaults(self):
        self.register(
            ToolSpec("read_file", "Read a file from workspace", {"path": {"type": "string", "required": True}}, category="filesystem"),
            lambda args: self.context.read_file(args.get("path", ""))
        )
        self.register(
            ToolSpec("write_file", "Write content to a file", {"path": {"type": "string", "required": True}, "content": {"type": "string", "required": True}}, category="filesystem"),
            lambda args: self.context.write_file(args.get("path", ""), args.get("content", ""))
        )
        self.register(
            ToolSpec("list_files", "List files in workspace", {"path": {"type": "string", "required": False}}, category="filesystem"),
            lambda args: self.context.list_files(args.get("path", ""))
        )
        self.register(
            ToolSpec("execute_bash", "Execute a shell command", {"command": {"type": "string", "required": True}}, permission=ToolPermission.PROMPT if not self.context.config.execute_local_commands else ToolPermission.ALLOWED, category="system"),
            lambda args: self.context.execute_bash(args.get("command", ""))
        )
        self.register(
            ToolSpec("web_search", "Search the web", {"query": {"type": "string", "required": True}}, category="web"),
            lambda args: self._web_search(args.get("query", ""))
        )
        self.register(
            ToolSpec("think", "Reason about the task without using a tool", {"thought": {"type": "string", "required": True}}, category="internal", parallel_safe=False),
            lambda args: f"Thought: {args.get('thought', '')[:500]}"
        )
        self.register(
            ToolSpec("finish", "Signal task completion", {"result": {"type": "string", "required": False}}, category="control", parallel_safe=False),
            lambda args: "__TASK_COMPLETE__"
        )
        self.register(
            ToolSpec("claude_md_read", "Read the project memory file", {}, category="memory"),
            lambda args: ClaudeMemory(self.context.workspace_path).read()
        )
        self.register(
            ToolSpec("claude_md_append", "Append to project memory", {"section": {"type": "string", "required": True}, "content": {"type": "string", "required": True}}, category="memory"),
            lambda args: ClaudeMemory(self.context.workspace_path).append(args.get("section", ""), args.get("content", ""))
        )

    def register(self, spec: ToolSpec, handler: Callable):
        self._tools[spec.name] = spec
        self._handlers[spec.name] = handler

    def get_spec(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def get_tools(self, category: Optional[str] = None, parallel_only: bool = False) -> List[ToolSpec]:
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        if parallel_only:
            tools = [t for t in tools if t.parallel_safe]
        return tools

    def execute(self, name: str, args: Dict) -> str:
        if name not in self._handlers:
            return f"Unknown tool: {name}"
        try:
            result = self._handlers[name](args if isinstance(args, dict) else {})
            with self.context._results_lock:
                self.context.last_results[name] = str(result)[:500]
            return str(result)
        except Exception as e:
            return f"Tool error ({name}): {e}"

    def execute_parallel(self, calls: List[Tuple[str, Dict]], max_workers: int = 4) -> List[Tuple[str, str]]:
        results: List[Tuple[str, str]] = []
        sequential = []
        parallelizable = []
        for name, args in calls:
            spec = self.get_spec(name)
            if spec and spec.parallel_safe:
                parallelizable.append((name, args))
            else:
                sequential.append((name, args))
        if parallelizable:
            workers = max(1, min(max_workers, len(parallelizable)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self.execute, name, args): name
                    for name, args in parallelizable
                }
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        results.append((name, future.result()))
                    except Exception as e:
                        results.append((name, f"Error: {e}"))
        for name, args in sequential:
            results.append((name, self.execute(name, args)))
        return results

    def list_tools(self) -> str:
        lines = ["Available tools:"]
        for name, spec in sorted(self._tools.items()):
            perm = " [requires permission]" if spec.permission == ToolPermission.PROMPT else ""
            parallel = " [parallel]" if spec.parallel_safe else " [sequential]"
            lines.append(f"  {name}{parallel}{perm}: {spec.description}")
        return "\n".join(lines)

    def _web_search(self, query: str) -> str:
        try:
            encoded = urllib.parse.quote(query)
            url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            results = []
            for topic in data.get("RelatedTopics", [])[:5]:
                if "Text" in topic:
                    results.append(topic["Text"])
                if "Topics" in topic:
                    for sub in topic["Topics"][:2]:
                        if "Text" in sub:
                            results.append(sub["Text"])
            text = data.get("AbstractText", "")
            if text:
                results.insert(0, text)
            return "\n".join(results) if results else f"No results for '{query}'"
        except Exception as e:
            return f"Search failed: {e}"


# ─── Plugin Loader (Claude Code Plugin System) ────────────────────────────────

class Plugin:
    def __init__(self, name: str, version: str, tools: Dict[str, Callable] = None):
        self.name = name
        self.version = version
        self.tools = tools or {}

class PluginManager:
    def __init__(self, plugins_dir: str):
        self.plugins_dir = Path(plugins_dir)
        self.plugins: Dict[str, Plugin] = {}
        self.plugins_dir.mkdir(exist_ok=True)

    def discover(self) -> List[str]:
        found = []
        for f in self.plugins_dir.glob("*.py"):
            if not f.name.startswith("_"):
                found.append(f.name)
        return found

    def load_plugin(self, name: str) -> Optional[Plugin]:
        path = self.plugins_dir / name
        if not path.exists():
            path = self.plugins_dir / f"{name}.py"
        if not path.exists():
            return None
        try:
            spec = importlib.util.spec_from_file_location(f"plugin_{name}", str(path))
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            plugin = Plugin(
                name=getattr(mod, "PLUGIN_NAME", name),
                version=getattr(mod, "PLUGIN_VERSION", "0.1"),
                tools=getattr(mod, "TOOLS", {}) or {}
            )
            self.plugins[name] = plugin
            return plugin
        except Exception:
            return None

    def load_all(self) -> List[str]:
        loaded = []
        for name in self.discover():
            p = self.load_plugin(name)
            if p:
                loaded.append(p.name)
        return loaded

    def get_tools(self) -> Dict[str, Callable]:
        tools = {}
        for plugin in self.plugins.values():
            tools.update(plugin.tools)
        return tools


# ─── Session Manager (Session Persistence) ────────────────────────────────────

@dataclass
class Session:
    id: str
    title: str
    created: str
    updated: str
    messages: List[Dict]
    config_snapshot: Dict
    cost_snapshot: Dict
    cycle_count: int = 0

class SessionManager:
    def __init__(self, sessions_dir: str):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self.sessions_dir / f"{sid}.json"

    def save(self, session: Session):
        data = {
            "id": session.id,
            "title": session.title,
            "created": session.created,
            "updated": datetime.now().isoformat(),
            "messages": session.messages,
            "config_snapshot": session.config_snapshot,
            "cost_snapshot": session.cost_snapshot,
            "cycle_count": session.cycle_count
        }
        self._path(session.id).write_text(json.dumps(data, indent=2))

    def load(self, sid: str) -> Optional[Session]:
        path = self._path(sid)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        known = {f.name for f in fields(Session)}
        filtered = {k: v for k, v in data.items() if k in known}
        try:
            return Session(**filtered)
        except TypeError:
            return None

    def list_sessions(self) -> List[Dict]:
        sessions = []
        for f in sorted(self.sessions_dir.glob("*.json"), key=os.path.getmtime, reverse=True):
            try:
                data = json.loads(f.read_text())
                sessions.append({
                    "id": data["id"],
                    "title": data["title"],
                    "created": data["created"],
                    "updated": data["updated"],
                    "message_count": len(data.get("messages", [])),
                    "cycle_count": data.get("cycle_count", 0)
                })
            except Exception:
                pass
        return sessions

    def delete(self, sid: str):
        path = self._path(sid)
        if path.exists():
            path.unlink()

    def new_id(self) -> str:
        return datetime.now().strftime("session_%Y%m%d_%H%M%S")


# ─── Multi-Agent Coordinator (from Claude Code) ───────────────────────────────

@dataclass
class SubAgentTask:
    id: str
    description: str
    instructions: str
    status: str = "pending"
    result: str = ""
    error: str = ""

class Coordinator:
    def __init__(self, config: Config, tool_registry: ToolRegistry, llm_client: 'LLMClient' = None):
        self.config = config
        self.tools = tool_registry
        self.llm = llm_client
        self.scratchpad_dir = os.path.join(config.workspace_path, ".scratchpad")
        os.makedirs(self.scratchpad_dir, exist_ok=True)
        self.tasks: List[SubAgentTask] = []
        self.active_workers = 0

    def plan(self, objective: str) -> List[SubAgentTask]:
        prompt = (
            f"Break down this objective into independent parallel subtasks:\n{objective}\n\n"
            "Return a JSON array of objects with 'id', 'description', and 'instructions'."
        )
        tasks = [
            SubAgentTask(id="task_1", description="Research & gather info", instructions="Gather all needed information"),
            SubAgentTask(id="task_2", description="Implement solution", instructions="Implement based on research"),
            SubAgentTask(id="task_3", description="Verify results", instructions="Test and verify"),
        ]
        self.tasks = tasks
        return tasks

    def run_parallel(self, tasks: List[SubAgentTask], max_workers: int = 3) -> List[SubAgentTask]:
        def _run_task(task: SubAgentTask) -> SubAgentTask:
            try:
                scratch_file = os.path.join(self.scratchpad_dir, f"{task.id}.md")
                with open(scratch_file, "w") as f:
                    f.write(f"# {task.id}: {task.description}\n\n{task.instructions}\n")
                task.status = "running"
                result_parts = []

                if self.llm:
                    sub_prompt = (
                        f"You are a sub-agent working on: {task.description}\n\n"
                        f"Instructions: {task.instructions}\n\n"
                        "Use available tools to complete this task. "
                        "When done, use the finish tool."
                    )
                    sub_messages = [
                        {"role": "system", "content": sub_prompt},
                        {"role": "user", "content": "Complete the assigned task using the tools available."}
                    ]
                    response = self.llm.chat_completion(sub_messages, max_tokens=1024)
                    result_parts.append(response.get("content", "")[:2000])

                result_parts.append(f"Task {task.id} completed")
                task.result = "\n".join(result_parts)
                task.status = "completed"
                with open(scratch_file, "a") as f:
                    f.write(f"\n## Result\n{task.result}\n")
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
            return task

        with ThreadPoolExecutor(max_workers=min(max_workers, self.config.max_sub_agents)) as ex:
            futures = {ex.submit(_run_task, t): t for t in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                except Exception as e:
                    task.status = "failed"
                    task.error = str(e)
        return tasks

    def aggregate_results(self, tasks: List[SubAgentTask]) -> str:
        lines = ["## Coordinator Results\n"]
        for t in tasks:
            status_icon = "✓" if t.status == "completed" else "✗" if t.status == "failed" else "⋯"
            lines.append(f"{status_icon} **{t.id}**: {t.description}")
            if t.result:
                lines.append(f"  Result: {t.result[:200]}")
            if t.error:
                lines.append(f"  Error: {t.error}")
        return "\n".join(lines)


# ─── Memory (Enhanced) ────────────────────────────────────────────────────────

class Memory:
    def __init__(self, max_size=50):
        self.max_size = max_size
        self.store = deque(maxlen=max_size)

    def add(self, item: str):
        self.store.append({
            "content": item,
            "timestamp": datetime.now().isoformat()
        })

    def get_recent(self, n=10):
        return list(self.store)[-n:]

    def get_all(self):
        return list(self.store)

    def search(self, query: str) -> List[Dict]:
        q = query.lower()
        return [m for m in self.store if q in m["content"].lower()]

    def summarize(self) -> str:
        if not self.store:
            return "No memories."
        lines = []
        for m in self.store[-10:]:
            lines.append(f"[{m['timestamp'][:19]}] {m['content'][:100]}")
        return "\n".join(lines)

    def clear(self):
        self.store.clear()


# ─── AIConfig ─────────────────────────────────────────────────────────────────

class AIConfig:
    def __init__(self, name="CatGPT", role="a helpful AI assistant", goals=None):
        self.ai_name = name
        self.ai_role = role
        self.ai_goals = goals or []

    def build_system_prompt(self, has_files=False, tool_registry: Optional[ToolRegistry] = None, claude_memory: Optional[ClaudeMemory] = None):
        prompt = f"You are {self.ai_name}, {self.ai_role}.\n"
        prompt += "Your decisions must always be made independently without seeking user assistance.\n"
        if claude_memory:
            mem = claude_memory.read()
            if len(mem) > 100:
                prompt += f"\n## Project Memory (CLAUDE.md)\n{mem[:1500]}\n"

        if claude_memory:
            prompt += f"\n## Workspace Root\n{claude_memory.workspace_path}\nAll file read/write paths are relative to this directory. Do NOT use absolute paths.\n"
        prompt += "\n## Available Tools\n"
        if tool_registry:
            for spec in tool_registry.get_tools():
                prompt += f"- {spec.name}: {spec.description}\n"

        prompt += "\n## Guidelines\n"
        prompt += "1. Use tools in parallel when they are independent.\n"
        prompt += "2. Record important information in CLAUDE.md memory.\n"
        prompt += "3. Think step by step before acting.\n"
        prompt += "4. Verify results before declaring completion.\n"
        prompt += "5. Use web_search when you need current information.\n"
        prompt += "6. Break complex tasks into smaller steps.\n"

        if self.ai_goals:
            prompt += "\n## Goals\n"
            for i, goal in enumerate(self.ai_goals, 1):
                prompt += f"{i}. {goal}\n"
        return prompt

    def build_tool_use_prompt(self) -> str:
        return (
            'Respond with a JSON object in this exact format:\n'
            '{\n'
            '  "thoughts": {\n'
            '    "text": "Your current thoughts",\n'
            '    "reasoning": "Your reasoning",\n'
            '    "plan": "Short plan (1-3 items)",\n'
            '    "criticism": "Self-criticism",\n'
            '    "speak": "What to say to user"\n'
            '  },\n'
            '  "tool_calls": [\n'
            '    {"name": "tool_name", "args": {"key": "value"}}\n'
            '  ],\n'
            '  "parallel": false\n'
            '}'
        )


# ─── LLM Client (LM Studio + OpenAI Compatible) ───────────────────────────────

class LLMClient:
    def __init__(self, config):
        self.config = config

    @property
    def base_url(self) -> str:
        return (self.config.lm_studio_url or "http://localhost:1234/v1").rstrip("/")

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    def count_tokens(self, text: str) -> int:
        return max(1, len(text.split())) if text else 0

    def chat_completion(self, messages, temperature=None, max_tokens=None, stream=False):
        # Streaming is not implemented; always request non-stream JSON.
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        try:
            resp = requests.post(self.chat_url, json=payload, headers=headers, timeout=180)
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            content = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            return {
                "content": content,
                "input_tokens": usage.get("prompt_tokens", self.count_tokens(str(messages))),
                "output_tokens": usage.get("completion_tokens", self.count_tokens(content)),
            }
        except requests.exceptions.ConnectionError:
            return {
                "content": json.dumps({
                    "thoughts": {"text": "Error: Cannot connect to LM Studio. Make sure LM Studio is running and the API server is enabled.",
                                 "reasoning": "API connection failed", "plan": "Check connection",
                                 "criticism": "Cannot proceed without LLM backend.", "speak": "Connection error."},
                    "tool_calls": [{"name": "finish", "args": {}}]
                }),
                "input_tokens": 0, "output_tokens": 0
            }
        except Exception as e:
            return {
                "content": json.dumps({
                    "thoughts": {"text": f"Error: {e}", "reasoning": "API call failed",
                                 "plan": "Check error details", "criticism": "Retry or reconfigure.",
                                 "speak": "API error."},
                    "tool_calls": [{"name": "finish", "args": {}}]
                }),
                "input_tokens": 0, "output_tokens": 0
            }

    def get_models(self):
        try:
            resp = requests.get(self.models_url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return [m.get("id", "unknown") for m in data.get("data", [])]
            return []
        except Exception:
            return []

    def is_connected(self):
        try:
            resp = requests.get(self.models_url, timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


# ─── Slash Commands (from Claude Code) ────────────────────────────────────────

class SlashCommand:
    def __init__(self, name: str, description: str, handler: Callable[[List[str]], str], aliases: List[str] = None):
        self.name = name
        self.description = description
        self.handler = handler
        self.aliases = aliases or []

    def match(self, text: str) -> bool:
        text = text.strip()
        for a in [self.name] + self.aliases:
            token = f"/{a}"
            if text == token or text.startswith(token + " "):
                return True
        return False

    def execute(self, text: str) -> str:
        args = text.strip().split()[1:]
        return self.handler(args)

class SlashCommandRegistry:
    def __init__(self):
        self.commands: Dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand):
        self.commands[cmd.name] = cmd

    def handle(self, text: str) -> Optional[str]:
        for cmd in self.commands.values():
            if cmd.match(text):
                return cmd.execute(text)
        return None

    def help_text(self) -> str:
        lines = ["Available slash commands:"]
        for cmd in self.commands.values():
            aliases = f" ({', '.join('/'+a for a in cmd.aliases)})" if cmd.aliases else ""
            lines.append(f"  /{cmd.name}{aliases}: {cmd.description}")
        return "\n".join(lines)


# ─── Agent (Enhanced with Claude Code Features) ───────────────────────────────

class Agent:
    def __init__(self, config, ai_config, tool_registry, llm_client, gui, coordinator=None, cost_tracker=None):
        self.config = config
        self.ai_config = ai_config
        self.tools = tool_registry
        self.llm = llm_client
        self.gui = gui
        self.coordinator = coordinator
        self.messages = []
        self.memory = Memory(max_size=50)
        self.claude_memory = ClaudeMemory(config.workspace_path)
        self.cost_tracker = cost_tracker or CostTracker()
        self.system_prompt = ai_config.build_system_prompt(tool_registry=self.tools, claude_memory=self.claude_memory)
        self.messages.append({"role": "system", "content": self.system_prompt})
        self.cycle_count = 0
        self.running = False
        self.paused = False
        self.task_plan = []
        self.completed_tasks = []
        self.current_task = None
        self.last_result = ""

    def add_user_input(self, text):
        self.messages.append({"role": "user", "content": text})
        self.gui.add_message("user", text)

    def _messages_for_llm(self) -> List[Dict]:
        """Trim history when smart_context is enabled to stay within budget."""
        if not self.config.smart_context:
            return list(self.messages)
        system = [m for m in self.messages if m.get("role") == "system"][:1]
        rest = [m for m in self.messages if m.get("role") != "system"]
        keep = max(4, int(self.config.memory_count) * 2)
        if len(rest) > keep:
            rest = rest[-keep:]
        return system + rest

    def think(self):
        self.cycle_count += 1

        self.gui.log(f"\n{'='*50}")
        self.gui.log(f"Cycle {self.cycle_count}")
        self.gui.log(f"{'='*50}")

        context = self._build_context()
        triggering_prompt = self.ai_config.build_tool_use_prompt()

        messages = self._messages_for_llm() + [
            {"role": "user", "content": context + "\n\n" + triggering_prompt}
        ]

        self.gui.log("\n[THINK] Sending to LM Studio...")

        response = self.llm.chat_completion(messages)

        self.cost_tracker.add_usage(
            response["input_tokens"], response["output_tokens"],
            self.config.cost_per_input_token, self.config.cost_per_output_token
        )
        try:
            self.gui.root.after(0, self.gui._update_cost_display)
        except Exception:
            pass

        content = response.get("content") or ""
        preview = content[:600]
        self.gui.log(f"[RAW RESPONSE]\n{preview}{'...' if len(content) > 600 else ''}")

        return self._parse_response(content)

    def _build_context(self):
        lines = []
        lines.append(f"Workspace: {self.config.workspace_path} (use relative paths for file operations)")
        if self.current_task:
            lines.append(f"Current task: {self.current_task}")
        if self.completed_tasks:
            lines.append(f"Completed: {', '.join(self.completed_tasks[-3:])}")
        if self.last_result:
            lines.append(f"Last result: {self.last_result[:200]}")
        mem = self.memory.summarize()
        if mem != "No memories.":
            lines.append(f"Memories:\n{mem}")
        lines.append(f"Cycle: {self.cycle_count}")
        cost = self.cost_tracker.summary()
        lines.append(f"Cost: {cost}")
        return "\n".join(lines)

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict]:
        if not text:
            return None
        fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
        if fence:
            try:
                return json.loads(fence.group(1))
            except json.JSONDecodeError:
                pass
        start = text.find("{")
        while start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(text)):
                c = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find("{", start + 1)
        return None

    def _parse_response(self, response):
        try:
            data = self._extract_json_object(response)
            if data is None:
                raise json.JSONDecodeError("No JSON object found", response, 0)

            thoughts = data.get("thoughts") or {}
            if not isinstance(thoughts, dict):
                thoughts = {"text": str(thoughts)}
            tool_calls = data.get("tool_calls", [])
            if isinstance(tool_calls, dict):
                tool_calls = [tool_calls]
            elif not isinstance(tool_calls, list):
                tool_calls = []
            parallel = bool(data.get("parallel", False))

            self.gui.log(f"\n[THOUGHTS]")
            self.gui.log(f"  Text: {thoughts.get('text', 'N/A')}")
            self.gui.log(f"  Reasoning: {thoughts.get('reasoning', 'N/A')}")
            self.gui.log(f"  Plan: {thoughts.get('plan', 'N/A')}")
            self.gui.log(f"  Criticism: {thoughts.get('criticism', 'N/A')}")
            self.gui.log(f"  Speak: {thoughts.get('speak', 'N/A')}")

            speak_text = thoughts.get("speak", "")
            if speak_text and speak_text != "N/A":
                self.gui.add_message("assistant", speak_text)
            else:
                self.gui.add_message("assistant", thoughts.get("text", "Processing..."))

            self.memory.add(f"Cycle {self.cycle_count}: {str(thoughts.get('text', ''))[:100]}")

            tool_calls_info = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name", "") or ""
                args = tc.get("args") or {}
                if not isinstance(args, dict):
                    args = {"value": args}
                if name:
                    tool_calls_info.append((name, args))
                    self.gui.log(f"\n[TOOL CALL] {name}({args})")

            return tool_calls_info, parallel
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError) as e:
            self.gui.log(f"\n[ERROR] Failed to parse response: {e}")
            self.gui.log(f"[DEBUG] Response was: {response[:200]}")
            return [], False

    def _auto_save(self):
        try:
            self.gui._save_session(f"auto_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            self.gui.log("[AUTO-SAVE] Session saved to disk.")
        except Exception:
            pass

    def execute(self, tool_calls_info, parallel=False):
        results = []
        if not tool_calls_info:
            self.gui.log("[FINISH] No tool calls - task complete.")
            self.gui.add_message("system", "Agent finished.")
            self._auto_save()
            return False

        finish_requested = any(name == "finish" for name, _ in tool_calls_info)
        work_calls = [(n, a) for n, a in tool_calls_info if n != "finish"]

        if parallel and len(work_calls) > 1:
            self.gui.log(f"[EXECUTE] Running {len(work_calls)} tools in parallel...")
            results = self.tools.execute_parallel(work_calls, self.config.max_parallel_tools)
            for name, result in results:
                self.last_result = str(result)[:500]
                self.gui.log(f"[RESULT] {name}: {str(result)[:400]}{'...' if len(str(result)) > 400 else ''}")
        else:
            for name, args in work_calls:
                self.gui.log(f"\n[EXECUTE] {name}({args})")
                result = self.tools.execute(name, args)
                self.last_result = str(result)[:500]
                self.gui.log(f"[RESULT] {result[:400]}{'...' if len(result) > 400 else ''}")
                results.append((name, result))
                if result == "__TASK_COMPLETE__":
                    finish_requested = True

        if results:
            serializable_calls = [
                {"name": n, "args": a} for n, a in tool_calls_info
            ]
            self.messages.append({
                "role": "assistant",
                "content": json.dumps({"thoughts": {"text": "Executed tools"}, "tool_calls": serializable_calls})
            })
            summary_lines = [f"Tool {name} returned: {str(result)[:300]}" for name, result in results]
            self.messages.append({"role": "user", "content": "\n".join(summary_lines)})

        if finish_requested:
            self.gui.add_message("system", "All goals completed! Agent finished.")
            self._auto_save()
            return False

        return True

    def run_step(self):
        if not self.running:
            return False
        tool_calls_info, parallel = self.think()
        should_continue = self.execute(tool_calls_info, parallel)
        if self.config.continuous_limit > 0 and self.cycle_count >= self.config.continuous_limit:
            self.gui.log(f"[INFO] Reached continuous limit ({self.config.continuous_limit}).")
            return False
        return should_continue

    def run_continuous(self, limit=0):
        self.config.continuous_limit = limit
        self.running = True
        while self.running:
            if self.paused:
                time.sleep(0.5)
                continue
            should_continue = self.run_step()
            try:
                self.gui.root.after(0, self.gui.update_cycle_label)
            except Exception:
                pass
            if not should_continue:
                break
        self.running = False

    def run_with_coordinator(self, objective: str):
        self.gui.log("[COORDINATOR] Planning and delegating tasks...")
        tasks = self.coordinator.plan(objective)
        self.gui.log(f"[COORDINATOR] Running {len(tasks)} sub-agents...")
        results = self.coordinator.run_parallel(tasks)
        summary = self.coordinator.aggregate_results(results)
        self.gui.log(f"[COORDINATOR] Results:\n{summary}")
        self.add_user_input(f"Coordinator completed. Summary:\n{summary}\nPlease finalize.")

    def stop(self):
        self.running = False

    def pause(self):
        self.paused = not self.paused
        return self.paused

    def serialize_session(self, session_title: str) -> Dict:
        return {
            "input_tokens": self.cost_tracker.session_input_tokens,
            "output_tokens": self.cost_tracker.session_output_tokens,
            "cost": self.cost_tracker.session_cost,
            "cycle_count": self.cycle_count
        }

    def get_tool_commands_text(self) -> str:
        return self.tools.list_tools()


# ─── Conversation Manager ─────────────────────────────────────────────────────

class Conversation:
    def __init__(self, title="New Chat", session_id=None):
        self.title = title
        self.session_id = session_id
        self.messages = []
        self.created = datetime.now()
        self.updated = datetime.now()

    def add_message(self, role, content):
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        self.updated = datetime.now()

    def get_context(self):
        # Only roles accepted by OpenAI-compatible chat APIs
        allowed = {"user", "assistant", "system"}
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self.messages
            if m.get("role") in allowed
        ]


# ─── GUI (AgentGPT-inspired redesign - dark blue) ─────────────────────────────

class LandingPage:
    def __init__(self, parent, callbacks):
        self.parent = parent
        self.callbacks = callbacks
        self.frame = tk.Frame(parent, bg="#0a0e27")
        self._build()

    def _build(self):
        self.frame.pack(fill=tk.BOTH, expand=True)

        center = tk.Frame(self.frame, bg="#0a0e27")
        center.place(relx=0.5, rely=0.45, anchor=tk.CENTER)

        tk.Label(
            center, text="catsdk",
            bg="#0a0e27", fg="#ffffff",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 36, "bold")
        ).pack()

        tk.Label(
            center, text="Assemble, configure, and deploy autonomous AI agents",
            bg="#0a0e27", fg="#7b8cbf",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 11)
        ).pack(pady=(6, 25))

        input_frame = tk.Frame(center, bg="#151a3a", bd=1, relief=tk.FLAT, highlightbackground="#2a3a7a", highlightthickness=1)
        input_frame.pack(fill=tk.X, padx=20, ipadx=0)

        self.entry = tk.Text(input_frame, height=2, wrap=tk.WORD,
            bg="#151a3a", fg="#c8d8ff",
            insertbackground="#4d7aff",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 11),
            relief=tk.FLAT, borderwidth=8, padx=10, pady=8,
            highlightthickness=0
        )
        self.entry.pack(fill=tk.X)
        self.entry.insert(1.0, "What do you want to achieve?")
        self.entry.config(fg="#5a6a9a")
        self.entry.bind("<FocusIn>", lambda e: self._on_focus_in())
        self.entry.bind("<FocusOut>", lambda e: self._on_focus_out())
        self.entry.bind("<Return>", self._on_return)

        btn_frame = tk.Frame(center, bg="#0a0e27")
        btn_frame.pack(pady=(15, 0))

        self.submit_btn = tk.Button(btn_frame, text="Deploy Agent",
            command=self._submit,
            bg="#1a2050", fg="#4d7aff",
            activebackground="#252e6e",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 11, "bold"),
            relief=tk.FLAT, padx=24, pady=8, cursor="hand2",
            highlightthickness=0
        )
        self.submit_btn.pack(side=tk.LEFT)

        self.coord_btn = tk.Button(btn_frame, text="Coordinator",
            command=self._coordinator,
            bg="#1a2050", fg="#4d7aff",
            activebackground="#252e6e",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 10),
            relief=tk.FLAT, padx=16, pady=8, cursor="hand2",
            highlightthickness=0
        )
        self.coord_btn.pack(side=tk.LEFT, padx=(10, 0))

        settings_btn = tk.Button(btn_frame, text="\u2699",
            command=self.callbacks.get("settings"),
            bg="#1a2050", fg="#4d7aff",
            activebackground="#252e6e",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 14),
            relief=tk.FLAT, padx=10, pady=6, cursor="hand2",
            highlightthickness=0
        )
        settings_btn.pack(side=tk.LEFT, padx=(10, 0))

        self._placeholder_active = True
        self._focused = False

    def _on_focus_in(self):
        self._focused = True
        if self._placeholder_active:
            self.entry.delete(1.0, "end-1c")
            self.entry.config(fg="#c8d8ff")
            self._placeholder_active = False

    def _on_focus_out(self):
        self._focused = False
        if not self.entry.get(1.0, "end-1c").strip():
            self.entry.delete(1.0, "end-1c")
            self.entry.insert(1.0, "What do you want to achieve?")
            self.entry.config(fg="#5a6a9a")
            self._placeholder_active = True

    def _on_return(self, event):
        # Shift+Enter inserts newline; bare Enter submits
        if event.state & 0x1:
            return None
        self._submit()
        return "break"

    def _submit(self):
        text = self.entry.get(1.0, "end-1c").strip()
        cb = self.callbacks.get("submit_goal")
        if cb and text and text != "What do you want to achieve?":
            cb(text, coordinator=False)

    def _coordinator(self):
        text = self.entry.get(1.0, "end-1c").strip()
        cb = self.callbacks.get("submit_goal")
        if cb and text and text != "What do you want to achieve?":
            cb(text, coordinator=True)

    def get_goal_text(self):
        text = self.entry.get(1.0, "end-1c").strip()
        if text == "What do you want to achieve?":
            return ""
        return text

    def destroy(self):
        self.frame.destroy()


class MacWindowHeader(tk.Frame):
    def __init__(self, parent, title="", bg_color="#101030", callbacks=None):
        super().__init__(parent, bg=bg_color, height=36)
        self.pack(fill=tk.X)
        self.pack_propagate(False)
        self.callbacks = callbacks or {}

        dot_frame = tk.Frame(self, bg=bg_color)
        dot_frame.pack(side=tk.LEFT, padx=(12, 0), pady=0)

        for color in ("#ff5f56", "#ffbd2e", "#27c93f"):
            canvas = tk.Canvas(dot_frame, width=12, height=12, bg=bg_color, highlightthickness=0)
            canvas.pack(side=tk.LEFT, padx=(0, 6))
            canvas.create_oval(1, 1, 11, 11, fill=color, outline="")

        tk.Label(self, text=title, bg=bg_color, fg="#7b8cbf",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 9)
        ).pack(side=tk.LEFT, expand=True, fill=tk.X, pady=0)

        right_frame = tk.Frame(self, bg=bg_color)
        right_frame.pack(side=tk.RIGHT, padx=(0, 8))

        if "export" in self.callbacks:
            export_btn = tk.Label(right_frame, text="Export", bg=bg_color, fg="#4d7aff",
                font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8), cursor="hand2")
            export_btn.pack(side=tk.RIGHT, padx=(4, 0))
            export_btn.bind("<Button-1>", lambda e: self.callbacks["export"]())

    def set_title(self, text):
        for w in self.winfo_children():
            if isinstance(w, tk.Label) and w.cget("bg") == "#101030":
                w.config(text=text)
                break


class MessageBubble(tk.Frame):
    def __init__(self, parent, role, content, timestamp="", show_copy=True, on_copy=None):
        colors = {
            "user":       ("#152050", "#ffffff", "\U0001f464 You"),
            "assistant":  ("#0e1a3a", "#c8d8ff", "\U0001f916 CatGPT"),
            "system":     ("#0a0e27", "#ffaa44", "\u2699 System"),
            "error":     ("#1a0a0a", "#ff6666", "\u2716 Error"),
            "cost":       ("#0a1a0a", "#44ffaa", "\u0024 Cost"),
        }
        bg, fg, label = colors.get(role, ("#0a0e27", "#c8d8ff", role))

        super().__init__(parent, bg="#0a0e27", pady=3)
        self.pack(fill=tk.X, padx=20)

        header = tk.Frame(self, bg="#0a0e27")
        header.pack(fill=tk.X)

        role_lbl = tk.Label(header, text=label, bg=bg, fg=fg,
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 9, "bold"),
            padx=8, pady=2)
        role_lbl.pack(side=tk.LEFT)

        if timestamp:
            tk.Label(header, text=timestamp, bg="#0a0e27", fg="#5a6a9a",
                font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8)
            ).pack(side=tk.LEFT, padx=(8, 0))

        if show_copy and on_copy:
            copy_lbl = tk.Label(header, text="Copy", bg="#0a0e27", fg="#4d7aff",
                font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8), cursor="hand2")
            copy_lbl.pack(side=tk.LEFT, padx=(8, 0))
            copy_lbl.bind("<Button-1>", lambda e, c=content: on_copy(c))

        bubble = tk.Frame(self, bg=bg, bd=0)
        bubble.pack(fill=tk.X, pady=(2, 0))

        msg = tk.Message(bubble, text=content, bg=bg, fg=fg,
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 10),
            justify=tk.LEFT, width=600)
        msg.pack(padx=10, pady=8, fill=tk.X, expand=True)


class ChatView:
    def __init__(self, parent, callbacks):
        self.parent = parent
        self.callbacks = callbacks
        self.frame = tk.Frame(parent, bg="#0a0e27")
        self._build()

    def _build(self):
        self.frame.pack(fill=tk.BOTH, expand=True)

        self.header = MacWindowHeader(self.frame, title="New Chat", bg_color="#101030",
            callbacks={"export": self.callbacks.get("export")})

        bar = tk.Frame(self.frame, bg="#101030", height=28)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        self.title_label = tk.Label(bar, text="New Chat", bg="#101030", fg="#ffffff",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 11, "bold"))
        self.title_label.pack(side=tk.LEFT, padx=15, pady=2)

        self.cycle_label = tk.Label(bar, text="", bg="#101030", fg="#5a6a9a",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 9))
        self.cycle_label.pack(side=tk.RIGHT, padx=15, pady=2)

        self.cost_bar = tk.Label(self.frame, text="Tokens: 0 in / 0 out",
            bg="#080c20", fg="#44ffaa",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8), anchor=tk.W, padx=15)
        self.cost_bar.pack(fill=tk.X)

        container = tk.Frame(self.frame, bg="#0a0e27")
        container.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(container, bg="#0a0e27", highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient=tk.VERTICAL, command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg="#0a0e27")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw", width=self.canvas.winfo_width())
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig("all", width=e.width))

        if sys.platform == "darwin":
            self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 100)), "units"))
        else:
            self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        input_frame = tk.Frame(self.frame, bg="#0a0e27")
        input_frame.pack(fill=tk.X, padx=15, pady=(5, 12))

        wrapper = tk.Frame(input_frame, bg="#151a3a", bd=1, relief=tk.FLAT, highlightbackground="#2a3a7a", highlightthickness=1)
        wrapper.pack(fill=tk.X)

        self.slash_hint = tk.Label(wrapper, text="", bg="#151a3a", fg="#5a6a9a",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8), anchor=tk.W, padx=8)
        self.slash_hint.pack(fill=tk.X)

        self.input_text = tk.Text(wrapper, height=2, wrap=tk.WORD,
            bg="#151a3a", fg="#c8d8ff",
            insertbackground="#4d7aff",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 10),
            relief=tk.FLAT, borderwidth=6, padx=8, pady=6,
            highlightthickness=0
        )
        self.input_text.pack(fill=tk.X)
        self._placeholder_active = False

        actions = tk.Frame(input_frame, bg="#0a0e27")
        actions.pack(fill=tk.X, pady=(6, 0))

        self.send_btn = self._make_btn(actions, "Send", self._send_msg, right=True)
        self.agent_btn = self._make_btn(actions, "Run Agent", self._run_agent, right=True, disabled=True)
        self.cont_btn = self._make_btn(actions, "Auto Run", self._run_continuous, right=True, disabled=True)
        self.stop_btn = self._make_btn(actions, "Stop", self._stop_agent, right=True, disabled=True)

        mode_frame = tk.Frame(actions, bg="#0a0e27")
        mode_frame.pack(side=tk.LEFT)

        self.agent_mode_var = tk.BooleanVar(value=False)
        self.coord_mode_var = tk.BooleanVar(value=False)

        self._add_toggle(mode_frame, "Agent", self.agent_mode_var, self._toggle_agent)
        self._add_toggle(mode_frame, "Coordinator", self.coord_mode_var, self._toggle_coord)
        self._add_toggle(mode_frame, "Copy", tk.BooleanVar(value=True), self._toggle_copy)

        self.char_count = tk.Label(actions, text="0", bg="#0a0e27", fg="#5a6a9a",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8))
        self.char_count.pack(side=tk.LEFT, padx=5)

        self.input_text.bind("<KeyRelease>", self._on_input_change)
        self.input_text.bind("<Tab>", self._on_tab)
        self.input_text.bind("<Return>", self._on_return)

        self._copy_labels = []

    def _make_btn(self, parent, text, cmd, right=False, disabled=False):
        state = tk.DISABLED if disabled else tk.NORMAL
        side = tk.RIGHT if right else tk.LEFT
        btn = tk.Button(parent, text=text, command=cmd,
            bg="#1a2050",
            fg="#4d7aff" if not disabled else "#333333",
            activebackground="#252e6e",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 9, "bold") if not disabled
                 else ("SF Mono" if sys.platform == "darwin" else "Consolas", 9),
            relief=tk.FLAT, padx=12, pady=4, cursor="hand2" if not disabled else "arrow",
            state=state, highlightthickness=0
        )
        btn.pack(side=side, padx=(0, 6))
        return btn

    def _add_toggle(self, parent, text, var, cmd):
        cb = tk.Checkbutton(parent, text=text, variable=var, command=cmd,
            bg="#0a0e27", fg="#7b8cbf",
            selectcolor="#101030",
            activebackground="#0a0e27",
            activeforeground="#c8d8ff",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8)
        )
        cb.pack(side=tk.LEFT, padx=(0, 4))

    def _send_msg(self):
        cb = self.callbacks.get("send")
        if cb:
            cb()

    def _run_agent(self):
        cb = self.callbacks.get("run_agent")
        if cb:
            cb()

    def _run_continuous(self):
        cb = self.callbacks.get("run_continuous")
        if cb:
            cb()

    def _stop_agent(self):
        cb = self.callbacks.get("stop")
        if cb:
            cb()

    def _toggle_agent(self):
        cb = self.callbacks.get("toggle_agent")
        if cb:
            cb(self.agent_mode_var.get())

    def _toggle_coord(self):
        cb = self.callbacks.get("toggle_coord")
        if cb:
            cb(self.coord_mode_var.get())

    def _toggle_copy(self):
        cb = self.callbacks.get("toggle_copy")
        if cb:
            cb()

    def _on_input_change(self, event=None):
        text = self.input_text.get(1.0, "end-1c").strip()
        self.char_count.config(text=str(len(text)))
        if text.startswith("/"):
            self._update_slash_hint(text)
        else:
            self.slash_hint.config(text="")

    def _update_slash_hint(self, text):
        rest = text[1:].strip() if text.startswith("/") else ""
        if not rest or " " in rest:
            # Show all commands for bare "/", nothing mid-args
            if rest == "" and text.startswith("/"):
                names = sorted(self.callbacks.get("slash_commands", {}).keys())
                self.slash_hint.config(text="  " + ", ".join(f"/{n}" for n in names[:8]) if names else "")
            else:
                self.slash_hint.config(text="")
            return
        partial = rest.lower()
        matches = [
            f"/{name}" for name in self.callbacks.get("slash_commands", {})
            if name.startswith(partial)
        ]
        self.slash_hint.config(text=("  " + ", ".join(matches[:5])) if matches else "")

    def _on_tab(self, event):
        return "break"

    def _on_return(self, event):
        if event.state & 0x1:
            return None
        self._send_msg()
        return "break"

    def set_title(self, text):
        self.title_label.config(text=text)
        self.header.set_title(text)

    def clear_messages(self):
        for w in self.inner.winfo_children():
            w.destroy()

    def _copy_enabled(self) -> bool:
        sc = self.callbacks.get("show_copy", True)
        return bool(sc() if callable(sc) else sc)

    def add_message(self, role, content, timestamp=""):
        MessageBubble(self.inner, role, content, timestamp,
            show_copy=self._copy_enabled(),
            on_copy=self.callbacks.get("on_copy"))
        self.canvas.after(30, lambda: self.canvas.yview_moveto(1.0))

    def update_cycle(self, text):
        self.cycle_label.config(text=text)

    def update_cost(self, text):
        self.cost_bar.config(text=text)

    def set_send_enabled(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.send_btn.config(state=state)

    def set_agent_buttons(self, running, agent_mode):
        if running:
            self.send_btn.config(state=tk.DISABLED, bg="#0a0e27", fg="#333333", cursor="arrow")
            self.agent_btn.config(state=tk.DISABLED, bg="#0a0e27", fg="#333333", cursor="arrow")
            self.cont_btn.config(state=tk.DISABLED, bg="#0a0e27", fg="#333333", cursor="arrow")
            self.stop_btn.config(state=tk.NORMAL, bg="#1a2050", fg="#4d7aff", cursor="hand2")
        else:
            self.send_btn.config(state=tk.NORMAL, bg="#1a2050", fg="#4d7aff", cursor="hand2")
            agent_state = tk.NORMAL if agent_mode else tk.DISABLED
            abg = "#1a2050"
            self.agent_btn.config(state=agent_state, bg=abg, fg="#4d7aff" if agent_mode else "#333333", cursor="hand2" if agent_mode else "arrow")
            self.cont_btn.config(state=agent_state, bg=abg, fg="#4d7aff" if agent_mode else "#333333", cursor="hand2" if agent_mode else "arrow")
            self.stop_btn.config(state=tk.DISABLED, bg="#0a0e27", fg="#333333", cursor="arrow")

    def get_input(self):
        text = self.input_text.get(1.0, "end-1c").strip()
        return text

    def clear_input(self):
        self.input_text.delete(1.0, tk.END)
        self.char_count.config(text="0")

    def destroy(self):
        self.frame.destroy()


class SidebarView:
    def __init__(self, parent, callbacks):
        self.parent = parent
        self.callbacks = callbacks
        self.frame = tk.Frame(parent, bg="#0c1028", width=220)
        self.frame.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.frame.pack_propagate(False)
        self._build()

    def _build(self):
        header = tk.Frame(self.frame, bg="#141840", height=44)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(header, text="catsdk", bg="#141840", fg="#ffffff",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 12, "bold")
        ).pack(side=tk.LEFT, padx=12, pady=10)

        new_btn = tk.Button(header, text="+ New", command=self.callbacks.get("new_chat"),
            bg="#1a2050", fg="#4d7aff",
            activebackground="#252e6e",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 9),
            relief=tk.FLAT, padx=8, pady=2, cursor="hand2", highlightthickness=0
        )
        new_btn.pack(side=tk.RIGHT, padx=8, pady=9)

        tk.Label(self.frame, text="CONVERSATIONS", bg="#0c1028", fg="#5a6a9a",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8)
        ).pack(anchor=tk.W, padx=12, pady=(12, 4))

        conv_frame = tk.Frame(self.frame, bg="#0c1028")
        conv_frame.pack(fill=tk.BOTH, expand=True)

        self.conv_canvas = tk.Canvas(conv_frame, bg="#0c1028", highlightthickness=0)
        scroll = tk.Scrollbar(conv_frame, orient=tk.VERTICAL, command=self.conv_canvas.yview)
        self.conv_inner = tk.Frame(self.conv_canvas, bg="#0c1028")
        self.conv_inner.bind("<Configure>", lambda e: self.conv_canvas.configure(scrollregion=self.conv_canvas.bbox("all")))
        self.conv_canvas.create_window((0, 0), window=self.conv_inner, anchor="nw")
        self.conv_canvas.configure(yscrollcommand=scroll.set)
        self.conv_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        bottom = tk.Frame(self.frame, bg="#0a0e27")
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        status = tk.Frame(bottom, bg="#0a0e27")
        status.pack(fill=tk.X, padx=10, pady=(6, 2))

        self.status_dot = tk.Canvas(status, width=10, height=10, bg="#0a0e27", highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(0, 5))
        self.dot = self.status_dot.create_oval(1, 1, 9, 9, fill="red", outline="")

        self.status_label = tk.Label(status, text="Disconnected", bg="#0a0e27", fg="#ff6666",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 8), anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        model_lbl = tk.Label(bottom, text="", bg="#0a0e27", fg="#5a6a9a",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 7))
        model_lbl.pack(fill=tk.X, padx=10, pady=(0, 2))
        self.model_label = model_lbl

        ws_lbl = tk.Label(bottom, text="", bg="#0a0e27", fg="#5a6a9a",
            font=("SF Mono" if sys.platform == "darwin" else "Consolas", 7))
        ws_lbl.pack(fill=tk.X, padx=10)
        self.ws_label = ws_lbl

        self.conversation_widgets = []

    def refresh_conversations(self, conversations, current_conv):
        for w in self.conv_inner.winfo_children():
            w.destroy()
        self.conversation_widgets = []
        for i, conv in enumerate(conversations):
            is_active = conv == current_conv
            bg = "#141840" if is_active else "#0c1028"
            frame = tk.Frame(self.conv_inner, bg=bg, cursor="hand2")
            frame.pack(fill=tk.X, padx=6, pady=1)

            title = conv.title[:22] + "..." if len(conv.title) > 22 else conv.title
            label = tk.Label(frame, text=title, bg=bg,
                fg="#ffffff" if is_active else "#7b8cbf",
                font=("SF Mono" if sys.platform == "darwin" else "Consolas", 9),
                anchor=tk.W, padx=8, pady=5)
            label.pack(fill=tk.X)

            for w in (frame, label):
                w.bind("<Button-1>", lambda e, c=conv: self.callbacks.get("switch")(c))
                w.bind("<Enter>", lambda e, f=frame, b=bg: f.configure(bg="#181c40"))
                w.bind("<Leave>", lambda e, f=frame, b=bg: f.configure(bg=b))

            self.conversation_widgets.append(frame)

    def update_status(self, connected, model_name="", workspace=""):
        if connected:
            self.status_dot.itemconfig(self.dot, fill="#00ff00")
            self.status_label.config(text="Connected", fg="#66ff66")
            self.model_label.config(text=model_name[:28])
        else:
            self.status_dot.itemconfig(self.dot, fill="red")
            self.status_label.config(text="Disconnected", fg="#ff6666")
            self.model_label.config(text="")
        self.ws_label.config(text=f"WS: {os.path.basename(workspace)}" if workspace else "")

    def destroy(self):
        self.frame.destroy()


class CodexGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("[c] ac holdings 1999-2026 catsdk v0.1.1a")
        self.root.configure(bg="#0a0e27")
        self.root.minsize(1000, 650)

        self.config = Config()
        self.agent = None
        self.running = False
        self.conversations = []
        self.current_conv = None
        self.use_agent_mode = False
        self.coordinator_mode = False
        self.show_copy = True

        self.plugin_manager = PluginManager(self.config.plugins_dir)
        self.session_manager = SessionManager(self.config.sessions_dir)
        self.slash_commands = SlashCommandRegistry()
        self.cost_tracker = CostTracker()
        self._setup_slash_commands()

        self._build_ui()
        self._create_new_conversation()
        self._check_lm_studio_periodic()
        self._load_plugins()

    def _setup_slash_commands(self):
        reg = self.slash_commands
        reg.register(SlashCommand("help", "Show available commands", lambda a: reg.help_text(), ["h"]))
        reg.register(SlashCommand("clear", "Clear the chat", lambda a: self._clear_chat(), ["cls"]))
        reg.register(SlashCommand("save", "Save current session", lambda a: self._save_session(a[0] if a else None)))
        reg.register(SlashCommand("load", "Load a session", lambda a: self._load_session(a[0] if a else None)))
        reg.register(SlashCommand("list", "List saved sessions", lambda a: self._list_sessions()))
        reg.register(SlashCommand("cost", "Show token usage and cost", lambda a: self.cost_tracker.summary()))
        reg.register(SlashCommand("reset_cost", "Reset session cost tracking", lambda a: self._reset_cost()))
        reg.register(SlashCommand("tools", "List all available tools", lambda a: self._list_tools()))
        reg.register(SlashCommand("plugins", "List loaded plugins", lambda a: self._list_plugins()))
        reg.register(SlashCommand("claude_md", "Read/edit CLAUDE.md", lambda a: self._handle_claude_md(a)))
        reg.register(SlashCommand("workspace", "Open workspace folder", lambda a: self._open_workspace()))
        reg.register(SlashCommand("model", "Set or show model", lambda a: self._handle_model(a)))
        reg.register(SlashCommand("temperature", "Set temperature", lambda a: self._handle_temp(a)))
        reg.register(SlashCommand("coordinator", "Toggle coordinator mode", lambda a: self._toggle_coordinator(a)))
        reg.register(SlashCommand("export", "Export conversation", lambda a: self._export_conversation(a[0] if a else None)))
        reg.register(SlashCommand("exit", "Exit the application", lambda a: self.root.quit()))

    def _build_ui(self):
        self.paned = tk.PanedWindow(self.root, bg="#0a0e27", sashwidth=2, sashrelief=tk.FLAT)
        self.paned.pack(fill=tk.BOTH, expand=True)

        self.sidebar_view = SidebarView(self.paned, {
            "new_chat": self._new_chat,
            "switch": self._switch_conversation,
        })
        self.paned.add(self.sidebar_view.frame, minsize=180, width=220, stretch="never")

        main_area = tk.Frame(self.paned, bg="#0a0e27")
        self.paned.add(main_area, stretch="always")

        self.main_container = tk.Frame(main_area, bg="#0a0e27")
        self.main_container.pack(fill=tk.BOTH, expand=True)

        self.landing = None
        self.chat_view = None
        self._show_landing()

    def _show_landing(self):
        if self.chat_view:
            self.chat_view.destroy()
            self.chat_view = None
        if not self.landing:
            self.landing = LandingPage(self.main_container, {
                "submit_goal": self._on_goal_submit,
                "settings": self._open_settings,
            })

    def _show_chat(self):
        if self.landing:
            self.landing.destroy()
            self.landing = None
        if not self.chat_view:
            slash_cmd_names = {}
            for name, cmd in self.slash_commands.commands.items():
                slash_cmd_names[name] = cmd.description
            self.chat_view = ChatView(self.main_container, {
                "send": self._send_message,
                "run_agent": self._run_agent_once,
                "run_continuous": self._run_agent_continuous,
                "stop": self._stop_agent,
                "toggle_agent": self._toggle_agent_mode,
                "toggle_coord": self._toggle_coordinator_mode,
                "toggle_copy": self._toggle_copy_mode,
                "export": lambda: self._export_conversation(),
                "show_copy": lambda: self.show_copy,
                "slash_commands": slash_cmd_names,
                "on_copy": self._copy_to_clipboard,
            })
            self._refresh_chat_messages()
            self._update_chat_buttons()

    def _on_goal_submit(self, text, coordinator=False):
        self._show_chat()
        self.coordinator_mode = coordinator
        if self.chat_view:
            self.chat_view.coord_mode_var.set(coordinator)
            self.chat_view.agent_mode_var.set(True)
        self.use_agent_mode = True
        self._toggle_agent_mode(True)
        if coordinator:
            self._send_to_coordinator(text)
        else:
            self._send_to_agent(text)

    def _toggle_agent_mode(self, enabled):
        self.use_agent_mode = enabled
        self._update_chat_buttons()
        self.log(f"[MODE] Agent mode: {'ON' if enabled else 'OFF'}")

    def _toggle_coordinator_mode(self, enabled):
        self.coordinator_mode = enabled
        self.log(f"[MODE] Coordinator mode: {'ON' if enabled else 'OFF'}")

    def _toggle_copy_mode(self):
        self.show_copy = not self.show_copy
        self.log(f"[MODE] Copy buttons: {'ON' if self.show_copy else 'OFF'}")

    def _copy_to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"[COPY] Copied {len(text)} chars to clipboard")

    def _new_chat(self):
        self._create_new_conversation()
        self.sidebar_view.refresh_conversations(self.conversations, self.current_conv)
        self._show_landing()

    def _create_new_conversation(self, session_id=None):
        num = len(self.conversations) + 1
        conv = Conversation(f"Chat {num}", session_id=session_id)
        self.conversations.append(conv)
        self.current_conv = conv
        self._reset_agent()

    def _switch_conversation(self, conv):
        self.current_conv = conv
        self.sidebar_view.refresh_conversations(self.conversations, self.current_conv)
        if self.chat_view:
            self.chat_view.set_title(conv.title)
            self._refresh_chat_messages()
        self._reset_agent()

    def _refresh_chat_messages(self):
        if not self.chat_view or not self.current_conv:
            return
        self.chat_view.clear_messages()
        for msg in self.current_conv.messages:
            ts = msg.get("timestamp", "")[:16] if msg.get("timestamp") else ""
            self.chat_view.add_message(msg["role"], msg["content"], ts)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        if self.chat_view:
            self.chat_view.canvas.yview_moveto(1.0)

    def _clear_chat_display(self):
        if self.chat_view:
            self.chat_view.clear_messages()

    def _display_message(self, role, content):
        if self.chat_view:
            self._show_chat()
            ts = datetime.now().strftime("%H:%M")
            self.chat_view.add_message(role, content, ts)
            self._scroll_to_bottom()

    def add_message(self, role, content):
        if self.current_conv:
            self.current_conv.add_message(role, content)
        self.root.after(0, lambda: self._display_message(role, content))

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.add_message("system", f"[{timestamp}] {message}")

    def add_cost_message(self, message):
        self.add_message("cost", message)

    def _update_chat_buttons(self):
        if self.chat_view:
            self.chat_view.set_agent_buttons(self.running, self.use_agent_mode)

    def update_cycle_label(self):
        """Safe to call from main thread (e.g. via root.after)."""
        try:
            if self.chat_view and self.agent:
                self.chat_view.update_cycle(f"Cycles: {self.agent.cycle_count}")
            self._update_cost_display()
        except tk.TclError:
            pass

    def _update_cost_display(self):
        try:
            if self.chat_view:
                self.chat_view.update_cost(
                    f"Tokens: {self.cost_tracker.session_input_tokens} in / {self.cost_tracker.session_output_tokens} out"
                )
        except tk.TclError:
            pass

    # ─── Slash Command Handlers ───────────────────────────────────────────

    def _clear_chat(self):
        self._clear_chat_display()
        if self.current_conv:
            self.current_conv.messages = []
        return "Chat cleared."

    def _save_session(self, name=None):
        if not self.current_conv:
            return "No active conversation."
        sid = self.current_conv.session_id or self.session_manager.new_id()
        if name:
            self.current_conv.title = name
        title = self.current_conv.title
        cost_snap = {
            "input_tokens": self.cost_tracker.session_input_tokens,
            "output_tokens": self.cost_tracker.session_output_tokens,
            "cost": self.cost_tracker.session_cost
        }
        session = Session(
            id=sid, title=title,
            created=self.current_conv.created.isoformat(),
            updated=datetime.now().isoformat(),
            messages=self.current_conv.messages,
            config_snapshot={k: getattr(self.config, k) for k in ["model", "temperature", "max_tokens"]},
            cost_snapshot=cost_snap,
            cycle_count=self.agent.cycle_count if self.agent else 0
        )
        self.session_manager.save(session)
        self.current_conv.session_id = sid
        return f"Session saved: {title} ({sid})"

    def _load_session(self, sid=None):
        if not sid:
            sessions = self.session_manager.list_sessions()
            if not sessions:
                return "No saved sessions."
            return "Sessions:\n" + "\n".join(
                f"  {s['id']}: {s['title']} ({s['message_count']} msgs)"
                for s in sessions[:10]
            )
        session = self.session_manager.load(sid)
        if not session:
            return f"Session not found: {sid}"
        self._create_new_conversation(session_id=sid)
        self.current_conv.title = session.title
        self._show_chat()
        if self.chat_view:
            self.chat_view.set_title(session.title)
        for msg in session.messages:
            self.current_conv.add_message(msg["role"], msg["content"])
            self._display_message(msg["role"], msg["content"])
        if session.cost_snapshot:
            self.cost_tracker.session_input_tokens = session.cost_snapshot.get("input_tokens", 0)
            self.cost_tracker.session_output_tokens = session.cost_snapshot.get("output_tokens", 0)
            self.cost_tracker.session_cost = session.cost_snapshot.get("cost", 0)
            self._update_cost_display()
        self.sidebar_view.refresh_conversations(self.conversations, self.current_conv)
        return f"Loaded session: {session.title} ({len(session.messages)} messages)"

    def _list_sessions(self):
        sessions = self.session_manager.list_sessions()
        if not sessions:
            return "No saved sessions."
        lines = ["Saved sessions:"]
        for s in sessions[:20]:
            lines.append(f"  {s['id']}: {s['title']} ({s['message_count']} msgs, {s['cycle_count']} cycles)")
        return "\n".join(lines)

    def _reset_cost(self):
        self.cost_tracker.reset_session()
        self._update_cost_display()
        return "Session cost tracking reset."

    def _list_tools(self):
        if self.agent and self.agent.tools:
            return self.agent.tools.list_tools()
        return "No agent initialized. Enable Agent mode first."

    def _list_plugins(self):
        plugins = self.plugin_manager.plugins
        if not plugins:
            return f"No plugins loaded. Place .py files in {self.config.plugins_dir}"
        lines = ["Loaded plugins:"]
        for name, plugin in plugins.items():
            lines.append(f"  {name} v{plugin.version} ({len(plugin.tools)} tools)")
        return "\n".join(lines)

    def _handle_claude_md(self, args):
        cm = ClaudeMemory(self.config.workspace_path)
        if not args:
            content = cm.read()
            return content[:2000] if content else "CLAUDE.md is empty."
        cmd = args[0].lower()
        if cmd == "read":
            return cm.read()[:2000]
        elif cmd == "append" and len(args) > 2:
            section = args[1]
            content = " ".join(args[2:])
            cm.append(section, content)
            return f"Appended to {section}."
        elif cmd == "section" and len(args) > 1:
            section = args[1]
            return cm.get_section(section)[:1000] or f"Section {section} not found."
        return "Usage: /claude_md [read|append|section]"

    def _open_workspace(self):
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", self.config.workspace_path])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", self.config.workspace_path])
            else:
                subprocess.Popen(["xdg-open", self.config.workspace_path])
            return f"Opened workspace: {self.config.workspace_path}"
        except Exception as e:
            return f"Could not open workspace: {e}"

    def _handle_model(self, args):
        if not args:
            return f"Current model: {self.config.model}"
        self.config.model = args[0]
        return f"Model set to: {self.config.model}"

    def _handle_temp(self, args):
        if not args:
            return f"Current temperature: {self.config.temperature}"
        try:
            self.config.temperature = float(args[0])
            return f"Temperature set to: {self.config.temperature}"
        except ValueError:
            return "Invalid temperature value."

    def _toggle_coordinator(self, args):
        self.coordinator_mode = not self.coordinator_mode
        if self.chat_view:
            self.chat_view.coord_mode_var.set(self.coordinator_mode)
        return f"Coordinator mode: {'ON' if self.coordinator_mode else 'OFF'}"

    def _export_conversation(self, path=None):
        if not self.current_conv:
            return "No active conversation."
        path = path or os.path.join(self.config.workspace_path, f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(path, "w") as f:
                json.dump({
                    "title": self.current_conv.title,
                    "created": self.current_conv.created.isoformat(),
                    "messages": self.current_conv.messages
                }, f, indent=2)
            return f"Exported to: {path}"
        except Exception as e:
            return f"Export failed: {e}"

    # ─── Sending / Agent Functions ────────────────────────────────────────

    def _send_message(self):
        if not self.chat_view:
            return
        content = self.chat_view.get_input()
        if not content:
            return

        if content.startswith("/"):
            result = self.slash_commands.handle(content)
            if result is not None:
                self.chat_view.clear_input()
                self.add_message("system", result)
                return
            # Unknown slash command falls through as a normal message

        self.chat_view.clear_input()

        if self.use_agent_mode:
            if not self.agent:
                self._init_agent()
            if self.coordinator_mode:
                self._send_to_coordinator(content)
            else:
                self._send_to_agent(content)
        else:
            self.add_message("user", content)
            self._send_to_llm(content)

    def _send_to_llm(self, content):
        if not self.current_conv:
            return
        # Include the user message just added
        messages = self.current_conv.get_context()
        self.add_message("system", "[Sending to LM Studio...]")

        def do_request():
            try:
                client = LLMClient(self.config)
                # Exclude ephemeral system status lines from the API payload
                api_messages = [
                    m for m in messages
                    if not (m.get("role") == "system" and str(m.get("content", "")).startswith("[Sending"))
                ]
                if not api_messages or api_messages[-1].get("role") != "user":
                    api_messages = list(api_messages) + [{"role": "user", "content": content}]
                response = client.chat_completion(api_messages)
                self.cost_tracker.add_usage(
                    response["input_tokens"], response["output_tokens"],
                    self.config.cost_per_input_token, self.config.cost_per_output_token
                )
                text = response.get("content") or ""
                self.root.after(0, lambda t=text: self._handle_llm_response(t))
                self.root.after(0, self._update_cost_display)
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda m=err: self.add_message("error", m))

        threading.Thread(target=do_request, daemon=True).start()

    def _handle_llm_response(self, response):
        # Persist assistant reply into conversation history + UI
        self.add_message("assistant", response)

    def _send_to_agent(self, content):
        if not self.agent:
            self._init_agent()
        # add_user_input records user turn once (agent history + GUI)
        self.agent.add_user_input(content)

        def run():
            self.running = True
            self.agent.running = True
            try:
                self.agent.run_step()
            finally:
                self.running = False
                self.agent.running = False
                try:
                    self.root.after(0, self._update_chat_buttons)
                    self.root.after(0, self._update_cost_display)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()
        self._update_chat_buttons()

    def _send_to_coordinator(self, content):
        if not self.agent:
            self._init_agent()
        self.agent.add_user_input(content)

        def run():
            self.running = True
            self.agent.running = True
            try:
                self.agent.run_with_coordinator(content)
            finally:
                self.running = False
                self.agent.running = False
                try:
                    self.root.after(0, self._update_chat_buttons)
                    self.root.after(0, self._update_cost_display)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()
        self._update_chat_buttons()

    def _run_agent_once(self):
        if self.running:
            return
        if not self.agent:
            self._init_agent()
        self.running = True
        self.agent.running = True
        self._update_chat_buttons()

        def run():
            try:
                self.agent.run_step()
            finally:
                self.running = False
                self.agent.running = False
                self.root.after(0, self._update_chat_buttons)
                self.root.after(0, self._update_cost_display)

        threading.Thread(target=run, daemon=True).start()

    def _run_agent_continuous(self):
        if self.running:
            return
        if not self.agent:
            self._init_agent()
        self.running = True
        self.agent.running = True
        self._update_chat_buttons()

        def run():
            try:
                self.agent.run_continuous()
            finally:
                self.running = False
                self.agent.running = False
                self.root.after(0, self._update_chat_buttons)
                self.root.after(0, self._update_cost_display)

        threading.Thread(target=run, daemon=True).start()

    def _stop_agent(self):
        self.running = False
        if self.agent:
            self.agent.stop()
        self.log("[STOP] Agent stopped by user.")
        self._update_chat_buttons()

    def _init_agent(self):
        goals_text = ""
        if self.current_conv:
            goals_text = self.current_conv.title

        self.config.ai_name = "CatGPT"
        self.config.ai_role = "a helpful AI assistant"
        self.config.ai_goals = [goals_text] if goals_text and goals_text != "New Chat" else ["Accomplish the user's request"]

        ai_config = AIConfig(
            name=self.config.ai_name,
            role=self.config.ai_role,
            goals=self.config.ai_goals
        )

        tool_context = ToolContext(self.config.workspace_path, self.config)
        tool_registry = ToolRegistry(tool_context)

        for plugin in self.plugin_manager.plugins.values():
            for name, handler in plugin.tools.items():
                if not callable(handler):
                    continue

                def _make_handler(h):
                    def _wrapped(args):
                        try:
                            return h(args if isinstance(args, dict) else {})
                        except TypeError:
                            try:
                                return h(**(args if isinstance(args, dict) else {}))
                            except Exception as e:
                                return f"Plugin error: {e}"
                        except Exception as e:
                            return f"Plugin error: {e}"
                    return _wrapped

                tool_registry.register(
                    ToolSpec(name, f"Plugin: {plugin.name}", {}, category="plugin"),
                    _make_handler(handler)
                )

        llm_client = LLMClient(self.config)
        coordinator = Coordinator(self.config, tool_registry, llm_client)
        self.agent = Agent(self.config, ai_config, tool_registry, llm_client, self, coordinator, cost_tracker=self.cost_tracker)

        if self.current_conv:
            for msg in self.current_conv.messages:
                if msg["role"] in ("user", "assistant"):
                    self.agent.messages.append({
                        "role": msg["role"],
                        "content": msg["content"]
                    })

        self.log(f"[AGENT] Initialized with {len(tool_registry.get_tools())} tools")
        plugins = self.plugin_manager.plugins
        if plugins:
            self.log(f"[PLUGINS] Loaded: {', '.join(plugins.keys())}")

    def _reset_agent(self):
        self.agent = None
        self.running = False
        self._update_chat_buttons()
        if self.chat_view:
            self.chat_view.update_cycle("")

    def _load_plugins(self):
        loaded = self.plugin_manager.load_all()
        if loaded:
            self.log(f"[PLUGINS] Loaded: {', '.join(loaded)}")

    # ─── Settings Window ──────────────────────────────────────────────────

    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.configure(bg="#0a0e27")
        win.geometry("550x550")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        font_family = "SF Mono" if sys.platform == "darwin" else "Consolas"
        main = tk.Frame(win, bg="#0a0e27", padx=20, pady=20)
        main.pack(fill=tk.BOTH, expand=True)

        row = 0
        tk.Label(main, text="Settings", bg="#0a0e27",
                 fg="#ffffff", font=(font_family, 13, "bold")).grid(
            row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 15))
        row += 1

        fields = [
            ("LM Studio URL:", self.config.lm_studio_url, tk.StringVar),
            ("Model:", self.config.model, tk.StringVar),
        ]
        self._settings_vars = {}
        for label, default, vtype in fields:
            tk.Label(main, text=label, bg="#0a0e27",
                     fg="#c8d8ff", font=(font_family, 10), anchor=tk.W).grid(
                row=row, column=0, sticky=tk.W, pady=3)
            var = vtype(value=default)
            entry = tk.Entry(main, textvariable=var, width=40,
                              bg="#151a3a", fg="#c8d8ff",
                              insertbackground="#4d7aff",
                              relief=tk.FLAT, highlightthickness=1,
                              highlightbackground="#2a3a7a")
            entry.grid(row=row, column=1, sticky=tk.W, padx=(10, 0), pady=3)
            self._settings_vars[label] = var
            row += 1

        tk.Label(main, text="Temperature:", bg="#0a0e27",
                 fg="#c8d8ff", font=(font_family, 10)).grid(
            row=row, column=0, sticky=tk.W, pady=3)
        temp_var = tk.DoubleVar(value=self.config.temperature)
        tk.Scale(main, from_=0.0, to=2.0, resolution=0.1,
                  orient=tk.HORIZONTAL, variable=temp_var,
                  bg="#0a0e27", fg="#c8d8ff",
                  troughcolor="#0c1028",
                  activebackground="#4d7aff",
                  highlightthickness=0, length=200).grid(
            row=row, column=1, sticky=tk.W, padx=(10, 0), pady=3)
        row += 1

        tk.Label(main, text="Max Tokens:", bg="#0a0e27",
                 fg="#c8d8ff", font=(font_family, 10)).grid(
            row=row, column=0, sticky=tk.W, pady=3)
        tokens_var = tk.IntVar(value=self.config.max_tokens)
        tk.Spinbox(main, from_=64, to=8192, increment=64,
                    textvariable=tokens_var, width=10,
                    bg="#151a3a", fg="#c8d8ff",
                    buttonbackground="#0c1028",
                    relief=tk.FLAT, highlightthickness=1,
                    highlightbackground="#2a3a7a").grid(
            row=row, column=1, sticky=tk.W, padx=(10, 0), pady=3)
        row += 1

        tk.Label(main, text="Max Parallel:", bg="#0a0e27",
                 fg="#c8d8ff", font=(font_family, 10)).grid(
            row=row, column=0, sticky=tk.W, pady=3)
        parallel_var = tk.IntVar(value=self.config.max_parallel_tools)
        tk.Spinbox(main, from_=1, to=10, increment=1,
                    textvariable=parallel_var, width=10,
                    bg="#151a3a", fg="#c8d8ff",
                    buttonbackground="#0c1028",
                    relief=tk.FLAT, highlightthickness=1,
                    highlightbackground="#2a3a7a").grid(
            row=row, column=1, sticky=tk.W, padx=(10, 0), pady=3)
        row += 1

        tk.Label(main, text="Cycle Limit:", bg="#0a0e27",
                 fg="#c8d8ff", font=(font_family, 10)).grid(
            row=row, column=0, sticky=tk.W, pady=3)
        limit_var = tk.IntVar(value=self.config.continuous_limit)
        tk.Spinbox(main, from_=0, to=100, increment=1,
                    textvariable=limit_var, width=10,
                    bg="#151a3a", fg="#c8d8ff",
                    buttonbackground="#0c1028",
                    relief=tk.FLAT, highlightthickness=1,
                    highlightbackground="#2a3a7a").grid(
            row=row, column=1, sticky=tk.W, padx=(10, 0), pady=3)
        row += 1

        shell_var = tk.BooleanVar(value=self.config.execute_local_commands)
        tk.Checkbutton(main, text="Enable Shell Commands",
                        variable=shell_var,
                        bg="#0a0e27", fg="#c8d8ff",
                        selectcolor="#0c1028",
                        activebackground="#0a0e27",
                        activeforeground="#c8d8ff").grid(
            row=row, column=0, columnspan=2, sticky=tk.W, pady=5)
        row += 1

        tk.Label(main, text="Workspace:", bg="#0a0e27",
                 fg="#c8d8ff", font=(font_family, 10)).grid(
            row=row, column=0, sticky=tk.W, pady=3)
        ws_frame = tk.Frame(main, bg="#0a0e27")
        ws_frame.grid(row=row, column=1, sticky=tk.W, padx=(10, 0), pady=3)
        ws_var = tk.StringVar(value=self.config.workspace_path)
        ws_entry = tk.Entry(ws_frame, textvariable=ws_var, width=28,
                             bg="#151a3a", fg="#c8d8ff",
                             insertbackground="#4d7aff",
                             relief=tk.FLAT, highlightthickness=1,
                             highlightbackground="#2a3a7a")
        ws_entry.pack(side=tk.LEFT)
        tk.Button(ws_frame, text="Browse", command=lambda: ws_var.set(
            filedialog.askdirectory(initialdir=self.config.workspace_path)),
            bg="#1a2050", fg="#4d7aff",
            activebackground="#252e6e",
            relief=tk.FLAT, font=(font_family, 9)).pack(side=tk.LEFT, padx=5)
        row += 1

        sep = tk.Frame(main, bg="#2a3a7a", height=1)
        sep.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=15)
        row += 1

        btn_frame = tk.Frame(main, bg="#0a0e27")
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)

        def save():
            url_var = self._settings_vars.get("LM Studio URL:")
            model_var = self._settings_vars.get("Model:")
            if url_var:
                self.config.lm_studio_url = url_var.get().strip().rstrip("/")
            if model_var:
                self.config.model = model_var.get().strip() or self.config.model
            self.config.temperature = float(temp_var.get())
            self.config.max_tokens = int(tokens_var.get())
            self.config.max_parallel_tools = max(1, int(parallel_var.get()))
            self.config.continuous_limit = max(0, int(limit_var.get()))
            self.config.execute_local_commands = bool(shell_var.get())
            new_ws = ws_var.get().strip()
            if new_ws:
                self.config.workspace_path = new_ws
                os.makedirs(self.config.workspace_path, exist_ok=True)
            # Rebuild agent so tool permissions / workspace take effect
            if self.agent:
                self._reset_agent()
            self.log(f"[SETTINGS] Updated. Workspace: {self.config.workspace_path}")
            win.destroy()

        tk.Button(btn_frame, text="Save", command=save,
                  bg="#1a2050", fg="#4d7aff",
                  activebackground="#252e6e",
                  font=(font_family, 10, "bold"), relief=tk.FLAT,
                  padx=20, pady=5).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Cancel", command=win.destroy,
                  bg="#1a2050", fg="#4d7aff",
                  activebackground="#252e6e",
                  font=(font_family, 10), relief=tk.FLAT,
                  padx=15, pady=5).pack(side=tk.LEFT, padx=5)

    # ─── LM Studio Connection Check ───────────────────────────────────────

    def _check_lm_studio_periodic(self):
        def check():
            connected = False
            model_name = ""
            try:
                url = self.config.lm_studio_url.rstrip("/")
                resp = requests.get(f"{url}/models", timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("data", [])
                    model_name = models[0].get("id", "local-model") if models else "local-model"
                    connected = True
            except Exception:
                connected = False
                model_name = ""

            ws = self.config.workspace_path

            def apply_status(ok=connected, model=model_name, workspace=ws):
                try:
                    self.sidebar_view.update_status(ok, model, workspace)
                except tk.TclError:
                    return
                try:
                    self.root.after(5000, self._check_lm_studio_periodic)
                except tk.TclError:
                    pass

            try:
                self.root.after(0, apply_status)
            except tk.TclError:
                pass

        threading.Thread(target=check, daemon=True).start()

    def _save_on_close(self):
        if self.current_conv and self.current_conv.messages:
            try:
                sid = self.current_conv.session_id or self.session_manager.new_id()
                title = self.current_conv.title
                cost_snap = {
                    "input_tokens": self.cost_tracker.session_input_tokens,
                    "output_tokens": self.cost_tracker.session_output_tokens,
                    "cost": self.cost_tracker.session_cost
                }
                session = Session(
                    id=sid, title=title,
                    created=self.current_conv.created.isoformat(),
                    updated=datetime.now().isoformat(),
                    messages=self.current_conv.messages,
                    config_snapshot={k: getattr(self.config, k) for k in ["model", "temperature", "max_tokens"]},
                    cost_snapshot=cost_snap,
                    cycle_count=self.agent.cycle_count if self.agent else 0
                )
                self.session_manager.save(session)
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.geometry("1200x750")
        self.root.protocol("WM_DELETE_WINDOW", self._save_on_close)
        self.root.mainloop()


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = CodexGUI()
    app.run()
