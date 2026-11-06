"""A narrow local bridge from GameLearn to dedicated Codex explanation tasks."""

from collections import deque
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import threading
import time

from gamelearn.services.learning_page_service import LearningPageManifestError


logger = logging.getLogger(__name__)
ACCOUNT_CHECK_TIMEOUT_SECONDS = 15
LEARNING_REASONING_EFFORT = "low"
LATEST_LEARNING_MODEL = "gpt-6-astra"


class LearningOptionsError(ValueError):
    """An invalid student model or reasoning selection."""


def _learning_models(client):
    models = []
    cursor = None
    for _ in range(10):
        result = client.request(
            "model/list", {"limit": 100, "includeHidden": False, "cursor": cursor},
            timeout_seconds=ACCOUNT_CHECK_TIMEOUT_SECONDS,
        )
        for entry in result.get("data", []):
            if not isinstance(entry, dict) or entry.get("hidden"):
                continue
            model = _safe_model_name(entry.get("model"))
            efforts = list(dict.fromkeys(
                option["reasoningEffort"]
                for option in entry.get("supportedReasoningEfforts", [])
                if isinstance(option, dict)
                and isinstance(option.get("reasoningEffort"), str)
                and re.fullmatch(r"[a-z]{1,20}", option["reasoningEffort"])
            ))
            if model and efforts and not any(item["model"] == model for item in models):
                models.append({"model": model, "efforts": efforts, "is_default": entry.get("isDefault") is True})
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return models


def _default_learning_model(models):
    low_models = [item for item in models if "low" in item["efforts"]]
    for item in low_models:
        if item["model"] == LATEST_LEARNING_MODEL:
            return item["model"]
    return next((item["model"] for item in low_models if item["is_default"]),
                low_models[0]["model"] if low_models else LATEST_LEARNING_MODEL)


class CodexBridgeError(RuntimeError):
    """A safe, user-facing Codex bridge failure."""


def _safe_model_name(value):
    """Only expose a bounded model identifier, never arbitrary config content."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", value):
        return value
    return None


@dataclass
class ExplanationJob:
    session_id: int
    model: str | None = None
    reasoning_effort: str = LEARNING_REASONING_EFFORT
    status: str = "QUEUED"
    message: str = "Waiting to create the learning page."
    task_id: str | None = None
    task_title: str | None = None
    turn_id: str | None = None
    output_path: str | None = None
    cancel_requested: bool = False
    progress_percent: int = 0
    input_characters: int = 0
    activity_count: int = 0
    activity_log: list = field(default_factory=list)
    last_cli_activity_at: float | None = None
    token_usage: dict | None = None
    phase_durations: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def payload(self):
        payload = {
            "session_id": self.session_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "status": self.status,
            "message": self.message,
            "progress_percent": self.progress_percent,
            "activity_count": self.activity_count,
            "activity_log": list(self.activity_log),
            "estimated_input_tokens": (self.input_characters + 3) // 4,
            "token_usage": dict(self.token_usage) if self.token_usage else None,
        }
        if self.last_cli_activity_at is not None:
            payload["last_activity_seconds"] = max(
                0, int(time.time() - self.last_cli_activity_at)
            )
        if self.phase_durations:
            payload["phase_durations"] = dict(self.phase_durations)
        return payload


@dataclass
class ChatJob:
    message_id: int
    status: str = "QUEUED"
    message: str = "Waiting for Codex."
    task_id: str | None = None
    turn_id: str | None = None
    input_characters: int = 0
    activity_count: int = 0
    last_cli_activity_at: float | None = None
    token_usage: dict | None = None
    updated_at: float = field(default_factory=time.time)

    def payload(self):
        payload = {
            "status": self.status,
            "message": self.message,
            "activity_count": self.activity_count,
            "estimated_input_tokens": (self.input_characters + 3) // 4,
            "token_usage": dict(self.token_usage) if self.token_usage else None,
        }
        if self.last_cli_activity_at is not None:
            payload["last_activity_seconds"] = max(
                0, int(time.time() - self.last_cli_activity_at)
            )
        return payload


class CodexExplanationDispatcher:
    """Run one explanation turn per session without exposing a general prompt API."""

    def __init__(self, project_root, client_factory=None, mermaid_validator=None):
        self.project_root = str(Path(project_root).resolve())
        self.client_factory = client_factory or CodexAppServerClient
        self.mermaid_validator = mermaid_validator or validate_learning_page_mermaid
        self._jobs = {}
        self._clients = {}
        self._lock = threading.Lock()
        self._connection_cache = None
        self._connection_checked_at = 0.0

    @property
    def configured(self):
        return bool(find_codex_command())

    def connection_payload(self):
        if not find_codex_command():
            return {"connected": False, "message": "The Codex CLI is not available on PATH."}
        with self._lock:
            if self._connection_cache and time.monotonic() - self._connection_checked_at < 60:
                return dict(self._connection_cache)
        model = None
        models = []
        try:
            with self.client_factory() as client:
                client.initialize()
                result = client.request(
                    "account/read",
                    {"refreshToken": False},
                    timeout_seconds=ACCOUNT_CHECK_TIMEOUT_SECONDS,
                )
                try:
                    configuration = client.request(
                        "config/read",
                        {"includeLayers": False, "cwd": self.project_root},
                        timeout_seconds=ACCOUNT_CHECK_TIMEOUT_SECONDS,
                    )
                    model = _safe_model_name(configuration.get("config", {}).get("model"))
                except CodexBridgeError:
                    # Older CLIs can still generate pages without a model preview.
                    pass
                try:
                    models = _learning_models(client)
                except (CodexBridgeError, TypeError, ValueError, AttributeError):
                    logger.info("Codex model catalog is unavailable.")
        except Exception as exc:
            logger.warning("Codex App Server account check failed: %s", exc)
            payload = {
                "connected": False,
                "message": (
                    "GameLearn could not start or reach the local Codex CLI App Server. "
                    "This does not mean the CLI is signed out. Restart GameLearn from a normal "
                    "PowerShell window and try again."
                ),
            }
        else:
            account = result.get("account")
            if result.get("requiresOpenaiAuth") is True and not account:
                payload = {
                    "connected": False,
                    "message": "Codex CLI sign-in required. Run: codex login --device-auth",
                }
            else:
                payload = {
                    "connected": True,
                    "message": "Ready to create the learning page.",
                }
        payload.update(model=model, reasoning_effort=LEARNING_REASONING_EFFORT)
        payload.update(models=models, generation_model=_default_learning_model(models))
        with self._lock:
            self._connection_cache = payload
            self._connection_checked_at = time.monotonic()
        return dict(payload)

    def submit(
        self,
        session_id,
        prompt,
        task_title,
        output_path,
        artifact_builder,
        output_schema=None,
        model=None,
        reasoning_effort=LEARNING_REASONING_EFFORT,
    ):
        connection = self.connection_payload()
        if not connection["connected"]:
            raise CodexBridgeError(connection["message"])
        model = connection["generation_model"] if model is None else model
        options = connection["models"] or [
            {"model": LATEST_LEARNING_MODEL, "efforts": ["low"]}
        ]
        selected = next((item for item in options if item["model"] == model), None)
        if selected is None or reasoning_effort not in selected["efforts"]:
            raise LearningOptionsError("Choose an available model and a supported reasoning level. Refresh the page to reload choices.")
        resolved_output = Path(output_path).resolve()
        generated_root = (Path(self.project_root) / "work").resolve()
        if not resolved_output.is_relative_to(generated_root):
            raise CodexBridgeError("The learning page output path is outside GameLearn's work folder.")
        with self._lock:
            existing = self._jobs.get(session_id)
            if existing and existing.status in {"QUEUED", "RUNNING", "STOPPING"}:
                return existing
            if resolved_output.exists():
                if not resolved_output.is_file():
                    raise CodexBridgeError("The learning page output path is not a file.")
                resolved_output.unlink()
            job = ExplanationJob(
                session_id=session_id,
                model=model,
                reasoning_effort=reasoning_effort,
                task_title=task_title,
                output_path=str(resolved_output),
                input_characters=len(prompt),
            )
            self._jobs[session_id] = job
        worker = threading.Thread(
            target=self._run,
            args=(job, prompt, artifact_builder, output_schema),
            name=f"gamelearn-codex-session-{session_id}",
            daemon=True,
        )
        worker.start()
        return job

    def status(self, session_id):
        with self._lock:
            return self._jobs.get(session_id)

    def forget(self, session_id):
        """Discard finished status when its saved session is removed."""
        with self._lock:
            self._jobs.pop(session_id, None)

    def stop(self, session_id):
        """Request cancellation of one learning-page job without affecting GameLearn."""
        with self._lock:
            job = self._jobs.get(session_id)
            if not job:
                raise CodexBridgeError("No learning page is currently being created.")
            if job.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return job
            job.cancel_requested = True
            job.status = "STOPPING"
            job.message = "Stopping learning-page creation…"
            job.updated_at = time.time()
            client = self._clients.get(session_id)
            task_id = job.task_id
            turn_id = job.turn_id

        if not client:
            return job
        if not task_id or not turn_id:
            client.terminate()
            return job
        try:
            client.interrupt_turn(task_id, turn_id)
        except Exception:
            client.terminate()
            return job

        fallback = threading.Timer(5.0, self._force_stop, args=(job, client))
        fallback.daemon = True
        fallback.start()
        return job

    def _update(self, job, status, message, task_id=None, turn_id=None, progress_percent=None, model=None):
        with self._lock:
            if model is not None:
                job.model = model
            job.status = status
            job.message = message
            if task_id:
                job.task_id = task_id
            if turn_id:
                job.turn_id = turn_id
            if progress_percent is not None:
                job.progress_percent = max(job.progress_percent, progress_percent)
            job.updated_at = time.time()

    def _cancelled(self, job):
        with self._lock:
            return job.cancel_requested

    def _finish_cancelled(self, job):
        self._update(job, "CANCELLED", "Learning-page creation was stopped.")

    def _force_stop(self, job, client):
        with self._lock:
            should_stop = job.status == "STOPPING" and self._clients.get(job.session_id) is client
        if should_stop:
            client.terminate()

    def _handle_client_status(self, job, event):
        if self._cancelled(job):
            return
        if event.get("type") == "connection_retry":
            attempt = event.get("attempt", 1)
            maximum = event.get("maximum", 2)
            retry_delay = event.get("retry_delay") or "shortly"
            self._update(
                job,
                "RUNNING",
                f"Codex connection failed; retrying {attempt} of {maximum} in {retry_delay}.",
            )
            return
        if event.get("type") == "token_usage":
            with self._lock:
                job.token_usage = dict(event["usage"])
                job.updated_at = time.time()
            return
        if event.get("type") != "turn_progress":
            return
        progress = {
            "planning": ("Codex is planning the guided explanation.", 45),
            "checking": ("Codex is checking the supplied lesson evidence.", 58),
            "writing": ("Codex is assembling the lesson content.", 74),
            "validating": ("GameLearn is validating the lesson content.", 86),
            "finishing": ("Codex has returned the lesson content.", 88),
        }.get(event.get("stage"))
        self._record_activity(job, event.get("activity") or "Codex reported progress.")
        if progress and progress[1] >= job.progress_percent:
            self._update(job, "RUNNING", progress[0], progress_percent=progress[1])

    def _record_activity(self, job, message):
        now = time.time()
        with self._lock:
            job.activity_count += 1
            job.last_cli_activity_at = now
            job.activity_log.append(
                {
                    "sequence": job.activity_count,
                    "message": message,
                    "timestamp": now,
                }
            )
            job.activity_log = job.activity_log[-6:]
            job.updated_at = now

    def _record_phase_duration(self, job, phase, started_at):
        with self._lock:
            job.phase_durations[phase] = round(max(0.0, time.monotonic() - started_at), 3)
            job.updated_at = time.time()

    def _run(self, job, prompt, artifact_builder, output_schema=None):
        if not job.input_characters:
            job.input_characters = len(prompt)
        if self._cancelled(job):
            self._finish_cancelled(job)
            return
        self._update(
            job,
            "RUNNING",
            "Codex is starting learning-page creation in the background.",
            progress_percent=15,
        )
        client = None
        try:
            startup_started = time.monotonic()
            client = self.client_factory()
            if hasattr(client, "set_status_callback"):
                client.set_status_callback(lambda event: self._handle_client_status(job, event))
            with client:
                with self._lock:
                    self._clients[job.session_id] = client
                client.initialize()
                if self._cancelled(job):
                    self._finish_cancelled(job)
                    return
                start_params = {
                    "cwd": self.project_root,
                    "approvalPolicy": "never",
                    "sandbox": "readOnly",
                    "serviceName": "gamelearn",
                    "config": {"model_reasoning_effort": job.reasoning_effort},
                }
                if job.model:
                    start_params["model"] = job.model
                try:
                    started = client.request("thread/start", start_params)
                except CodexBridgeError as exc:
                    if "unknown variant `readOnly`" not in str(exc):
                        raise
                    start_params["sandbox"] = "read-only"
                    started = client.request("thread/start", start_params)
                task_id = started.get("thread", {}).get("id")
                if not task_id:
                    raise CodexBridgeError("Codex did not return a task identifier.")
                self._update(
                    job,
                    "RUNNING",
                    "Codex is preparing the learning page in the background.",
                    task_id=task_id,
                    progress_percent=28,
                    model=_safe_model_name(started.get("model")),
                )
                if self._cancelled(job):
                    self._finish_cancelled(job)
                    return
                try:
                    client.request(
                        "thread/name/set",
                        {"threadId": task_id, "name": job.task_title},
                    )
                except CodexBridgeError:
                    logger.info("This Codex CLI could not set the explanation task title.")
                self._record_phase_duration(job, "app_server_startup", startup_started)
                generation_started = time.monotonic()
                turn_params = {
                    "threadId": task_id,
                    "input": [{"type": "text", "text": prompt}],
                    "cwd": self.project_root,
                    "approvalPolicy": "never",
                    "effort": job.reasoning_effort,
                }
                if output_schema is not None:
                    turn_params["outputSchema"] = output_schema
                result = client.request("turn/start", turn_params)
                turn_id = result.get("turn", {}).get("id")
                if not turn_id:
                    raise CodexBridgeError("Codex did not return a turn identifier.")
                self._update(
                    job,
                    "RUNNING",
                    "Codex is analysing the evidence and drafting the lesson content.",
                    task_id=task_id,
                    turn_id=turn_id,
                    progress_percent=40,
                )
                try:
                    completion, final_text = client.wait_for_turn(turn_id)
                finally:
                    self._record_phase_duration(job, "codex_generation", generation_started)
                if self._cancelled(job):
                    self._finish_cancelled(job)
                    return
                final_status = completion.get("status", "completed")
                if final_status not in {"completed", "COMPLETED"}:
                    error = completion.get("error") or {}
                    detail = error.get("message") if isinstance(error, dict) else None
                    raise CodexBridgeError(detail or f"The Codex turn ended with status {final_status}.")
                output_file = Path(job.output_path)
                self._update(
                    job,
                    "RUNNING",
                    "GameLearn is validating and rendering the lesson content.",
                    task_id=task_id,
                    turn_id=turn_id,
                    progress_percent=88,
                )
                render_started = time.monotonic()
                try:
                    try:
                        artifact_builder(final_text, output_file)
                    except CodexBridgeError:
                        raise
                    except LearningPageManifestError as exc:
                        raise CodexBridgeError(str(exc)) from exc
                    except Exception as exc:
                        raise CodexBridgeError(
                            "GameLearn could not render the generated lesson content."
                        ) from exc
                finally:
                    self._record_phase_duration(job, "manifest_rendering", render_started)
                if not output_file.is_file() or output_file.stat().st_size == 0:
                    raise CodexBridgeError(
                        "GameLearn could not render the generated lesson content."
                    )
                if output_file.stat().st_size > 1_000_000:
                    raise CodexBridgeError("The generated learning page exceeded the 1 MB limit.")
                self._update(
                    job,
                    "RUNNING",
                    "GameLearn is validating the generated Mermaid diagrams.",
                    task_id=task_id,
                    turn_id=turn_id,
                    progress_percent=90,
                )
                mermaid_started = time.monotonic()
                try:
                    self.mermaid_validator(output_file, self.project_root)
                finally:
                    self._record_phase_duration(job, "mermaid_validation", mermaid_started)
            if self._cancelled(job):
                self._finish_cancelled(job)
                return
            self._update(
                job,
                "COMPLETED",
                "The learning page is ready.",
                task_id=task_id,
                turn_id=turn_id,
                progress_percent=100,
            )
        except Exception as exc:
            if self._cancelled(job):
                self._finish_cancelled(job)
            else:
                self._remove_failed_output(job)
                if not isinstance(exc, CodexBridgeError):
                    logger.exception("Unexpected Codex explanation failure for session %s", job.session_id)
                self._update(
                    job,
                    "FAILED",
                    _safe_error(exc),
                    task_id=job.task_id,
                    turn_id=job.turn_id,
                )
        finally:
            with self._lock:
                if self._clients.get(job.session_id) is client:
                    self._clients.pop(job.session_id, None)

    def _remove_failed_output(self, job):
        if not job.output_path:
            return
        output_path = Path(job.output_path).resolve()
        generated_root = (Path(self.project_root) / "work").resolve()
        if output_path.is_relative_to(generated_root) and output_path.is_file():
            output_path.unlink()


class CodexChatDispatcher:
    """Answer each saved question in a new read-only Codex task."""

    def __init__(self, project_root, client_factory=None):
        self.project_root = str(Path(project_root).resolve())
        self.client_factory = client_factory or CodexAppServerClient
        self._jobs = {}
        self._lock = threading.Lock()

    def submit(self, message_id, prompt, task_title, completion_callback):
        if not find_codex_command():
            raise CodexBridgeError("The Codex CLI is not available on PATH.")
        with self._lock:
            existing = self._jobs.get(message_id)
            if existing:
                return existing
            job = ChatJob(message_id=message_id)
            job.input_characters = len(prompt)
            self._jobs[message_id] = job
        worker = threading.Thread(
            target=self._run,
            args=(job, prompt, task_title, completion_callback),
            name=f"gamelearn-codex-chat-{message_id}",
            daemon=True,
        )
        worker.start()
        return job

    def status(self, message_id):
        with self._lock:
            return self._jobs.get(message_id)

    def forget(self, message_id):
        """Discard finished status when its saved message is removed."""
        with self._lock:
            self._jobs.pop(message_id, None)

    def _update(self, job, status, message, task_id=None, turn_id=None):
        with self._lock:
            job.status = status
            job.message = message
            if task_id:
                job.task_id = task_id
            if turn_id:
                job.turn_id = turn_id
            job.updated_at = time.time()

    def _handle_client_status(self, job, event):
        if event.get("type") == "token_usage":
            with self._lock:
                job.token_usage = dict(event["usage"])
                job.updated_at = time.time()
            return
        if event.get("type") != "turn_progress":
            return
        messages = {
            "planning": "Codex is planning the answer.",
            "checking": "Codex is checking the supplied evidence.",
            "writing": "Codex is drafting the answer.",
            "validating": "Codex is checking the answer.",
            "finishing": "Codex is finishing the answer.",
        }
        message = messages.get(event.get("stage"))
        if message:
            with self._lock:
                job.message = message
                job.activity_count += 1
                job.last_cli_activity_at = time.time()
                job.updated_at = job.last_cli_activity_at

    def _run(self, job, prompt, task_title, completion_callback):
        if not job.input_characters:
            job.input_characters = len(prompt)
        self._update(job, "RUNNING", "Codex is preparing an answer.")
        try:
            with self.client_factory() as client:
                if hasattr(client, "set_status_callback"):
                    client.set_status_callback(lambda event: self._handle_client_status(job, event))
                client.initialize()
                start_params = {
                    "cwd": self.project_root,
                    "approvalPolicy": "never",
                    "sandbox": "readOnly",
                    "serviceName": "gamelearn",
                }
                try:
                    started = client.request("thread/start", start_params)
                except CodexBridgeError as exc:
                    if "unknown variant `readOnly`" not in str(exc):
                        raise
                    start_params["sandbox"] = "read-only"
                    started = client.request("thread/start", start_params)
                task_id = started.get("thread", {}).get("id")
                if not task_id:
                    raise CodexBridgeError("Codex did not return a task identifier.")
                try:
                    client.request("thread/name/set", {"threadId": task_id, "name": task_title})
                except CodexBridgeError:
                    logger.info("This Codex CLI could not set the tutor task title.")
                result = client.request(
                    "turn/start",
                    {
                        "threadId": task_id,
                        "input": [{"type": "text", "text": prompt}],
                        "cwd": self.project_root,
                        "approvalPolicy": "never",
                        "effort": "low",
                    },
                )
                turn_id = result.get("turn", {}).get("id")
                if not turn_id:
                    raise CodexBridgeError("Codex did not return a turn identifier.")
                self._update(
                    job,
                    "RUNNING",
                    "Codex is answering the question.",
                    task_id=task_id,
                    turn_id=turn_id,
                )
                completion, final_text = client.wait_for_turn(turn_id)
                final_status = completion.get("status", "completed")
                if final_status not in {"completed", "COMPLETED"}:
                    error = completion.get("error") or {}
                    detail = error.get("message") if isinstance(error, dict) else None
                    raise CodexBridgeError(detail or f"The Codex turn ended with status {final_status}.")
                answer = final_text.strip()
                if not answer:
                    raise CodexBridgeError("Codex completed without returning an answer.")
            completion_callback("COMPLETED", answer, task_id)
            self._update(job, "COMPLETED", "Answer ready.", task_id=task_id, turn_id=turn_id)
        except Exception as exc:
            if not isinstance(exc, CodexBridgeError):
                logger.exception("Unexpected Codex chat failure for message %s", job.message_id)
            safe_message = str(exc) if isinstance(exc, CodexBridgeError) else "Codex could not answer the question."
            try:
                completion_callback("FAILED", safe_message, job.task_id)
            except Exception:
                logger.exception("Could not persist failed Codex chat message %s", job.message_id)
            self._update(job, "FAILED", safe_message, task_id=job.task_id, turn_id=job.turn_id)


class CodexAppServerClient:
    """Small JSONL client for the stable Codex app-server thread and turn methods."""

    def __init__(
        self,
        timeout_seconds=30,
        turn_timeout_seconds=900,
        max_connection_failures=2,
        process_factory=None,
    ):
        self.timeout_seconds = timeout_seconds
        self.turn_timeout_seconds = turn_timeout_seconds
        self.process_factory = process_factory or subprocess.Popen
        self.max_connection_failures = max_connection_failures
        self.process = None
        self.messages = Queue()
        self.stderr_lines = deque(maxlen=20)
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._status_callback = None
        self._connection_failures = 0
        self._forced_error = None

    def __enter__(self):
        command = find_codex_command()
        if not command:
            raise CodexBridgeError("The Codex CLI is not available on PATH.")
        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.process = self.process_factory(command, **kwargs)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self.process:
            return
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def initialize(self):
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "gamelearn",
                    "title": "GameLearn",
                    "version": "0.1.0",
                }
            },
        )
        self.notify("initialized", {})

    def request(self, method, params, timeout_seconds=None):
        request_id = self._next_request_id()
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + (timeout_seconds or self.timeout_seconds)
        while True:
            message = self._next_message(deadline)
            if message.get("id") != request_id:
                self._reject_server_request(message)
                continue
            if "error" in message:
                error = message["error"]
                raise CodexBridgeError(error.get("message", "Codex rejected the request."))
            return message.get("result", {})

    def notify(self, method, params):
        self._send({"method": method, "params": params})

    def set_status_callback(self, callback):
        self._status_callback = callback

    def interrupt_turn(self, thread_id, turn_id):
        """Send a cancellation request while wait_for_turn owns the response queue."""
        request_id = self._next_request_id()
        self._send(
            {
                "method": "turn/interrupt",
                "id": request_id,
                "params": {"threadId": thread_id, "turnId": turn_id},
            }
        )

    def terminate(self):
        process = self.process
        if process and process.poll() is None:
            process.terminate()

    def wait_for_turn(self, turn_id):
        deadline = time.monotonic() + self.turn_timeout_seconds
        final_messages = []
        while True:
            message = self._next_message(deadline)
            self._reject_server_request(message)
            self._report_turn_progress(message)
            if message.get("method") == "item/completed":
                item = message.get("params", {}).get("item", {})
                if item.get("type") == "agentMessage" and item.get("text"):
                    final_messages.append(item["text"])
            if message.get("method") != "turn/completed":
                continue
            turn = message.get("params", {}).get("turn", {})
            if turn.get("id") == turn_id:
                return turn, final_messages[-1] if final_messages else ""

    def _report_turn_progress(self, message):
        if not self._status_callback:
            return
        method = message.get("method")
        if method == "thread/tokenUsage/updated":
            usage = message.get("params", {}).get("tokenUsage", {}).get("last", {})
            safe_usage = {
                name: int(usage.get(name, 0) or 0)
                for name in (
                    "inputTokens",
                    "cachedInputTokens",
                    "outputTokens",
                    "reasoningOutputTokens",
                    "totalTokens",
                )
            }
            try:
                self._status_callback({"type": "token_usage", "usage": safe_usage})
            except Exception:
                logger.exception("Codex token-usage callback failed.")
            return
        item = message.get("params", {}).get("item", {})
        item_type = item.get("type")
        stage = None
        if item_type == "reasoning" and method == "item/started":
            stage = "planning"
            activity = "Started planning the lesson structure."
        elif item_type == "reasoning" and method == "item/completed":
            stage = "planning"
            activity = "Completed a lesson-planning step."
        elif item_type == "commandExecution":
            stage = "checking"
            activity = (
                "Started a safe workspace check."
                if method == "item/started"
                else "Completed a safe workspace check."
            )
        elif item_type == "fileChange":
            stage = "checking"
            activity = (
                "Started a lesson-content preparation step."
                if method == "item/started"
                else "Completed a lesson-content preparation step."
            )
        elif item_type == "agentMessage" and method == "item/completed":
            stage = "finishing"
            activity = "Received the task completion report."
        if stage:
            try:
                self._status_callback(
                    {"type": "turn_progress", "stage": stage, "activity": activity}
                )
            except Exception:
                logger.exception("Codex progress callback failed.")

    def _send(self, message):
        with self._send_lock:
            if not self.process or self.process.poll() is not None:
                raise CodexBridgeError(self._process_error())
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def _next_request_id(self):
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _next_message(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexBridgeError("Codex did not respond before the timeout.")
            try:
                message = self.messages.get(timeout=min(remaining, 1.0))
            except Empty:
                if self.process and self.process.poll() is not None:
                    raise CodexBridgeError(self._process_error())
                continue
            if message is None:
                raise CodexBridgeError(self._process_error())
            return message

    def _reject_server_request(self, message):
        if "id" in message and "method" in message:
            self._send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32000,
                        "message": "GameLearn cannot answer interactive Codex requests.",
                    },
                }
            )

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put(None)

    def _read_stderr(self):
        for line in self.process.stderr:
            detail = line.strip()
            self.stderr_lines.append(detail)
            retry = _connection_retry(detail)
            if not retry:
                continue
            self._connection_failures += 1
            event = {
                "type": "connection_retry",
                "attempt": self._connection_failures,
                "maximum": self.max_connection_failures,
                "retry_delay": retry.get("retry_delay"),
            }
            if self._status_callback:
                try:
                    self._status_callback(event)
                except Exception:
                    logger.exception("Codex status callback failed.")
            if self._connection_failures >= self.max_connection_failures:
                self._forced_error = (
                    f"Codex could not connect to the model service after {self.max_connection_failures} attempts. "
                    "Check the internet connection and try creating the learning page again."
                )
                self.terminate()
                return

    def _process_error(self):
        if self._forced_error:
            return self._forced_error
        detail = next((line for line in reversed(self.stderr_lines) if line), "")
        return detail or "The local Codex app server stopped unexpectedly."


def validate_learning_page_mermaid(output_path, project_root):
    """Parse generated Mermaid blocks with the exact browser bundle before publishing."""
    document_text = Path(output_path).read_text(encoding="utf-8")
    if not re.search(
        r"\bclass\s*=\s*(['\"])[^'\"]*\bmermaid\b[^'\"]*\1",
        document_text,
        re.IGNORECASE,
    ):
        return
    root = Path(project_root).resolve()
    validator = root / "tools" / "validate_mermaid.mjs"
    node = shutil.which("node")
    if not validator.is_file() or not node:
        raise CodexBridgeError(
            "GameLearn could not validate the generated Mermaid diagrams locally."
        )
    try:
        result = subprocess.run(
            [node, str(validator), str(Path(output_path).resolve())],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Local Mermaid validation could not run: %s", exc)
        raise CodexBridgeError(
            "GameLearn could not validate the generated Mermaid diagrams locally."
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            logger.warning("Generated Mermaid validation failed: %s", detail[:2_000])
        raise CodexBridgeError(
            "The generated learning page contained invalid Mermaid diagram syntax. "
            "Please recreate the learning page."
        )


def find_codex_command():
    executable = shutil.which("codex")
    if executable and os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        npm_root = Path(executable).parent
        node = npm_root / "node.exe"
        if not node.is_file():
            resolved_node = shutil.which("node")
            node = Path(resolved_node) if resolved_node else None
        script = npm_root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if node and node.is_file() and script.is_file():
            return [str(node), str(script), "app-server", "--listen", "stdio://"]
    elif executable:
        return [executable, "app-server", "--listen", "stdio://"]
    return None


def _safe_error(error):
    if isinstance(error, CodexBridgeError):
        detail = str(error)
        lowered = detail.lower()
        if "invalid_json_schema" in lowered or "invalid schema for response_format" in lowered:
            logger.warning("Codex rejected the lesson output schema: %s", detail[:2_000])
            return (
                "Codex rejected GameLearn's structured lesson format. "
                "Restart GameLearn and try creating the page again."
            )
        if detail.lstrip().startswith(("{", "[")):
            logger.warning("Codex returned a raw protocol error: %s", detail[:2_000])
            return "The local Codex service rejected the learning-page request. Please try again."
        return detail
    return "Codex could not create the learning page. Check the GameLearn terminal for details."


def _connection_retry(detail):
    try:
        payload = json.loads(detail)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"retry_delay": None} if "stream connection failed" in detail.lower() else None
    fields = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(fields, dict):
        return None
    message = str(fields.get("message", "")).lower()
    target = str(payload.get("target", ""))
    if "stream connection failed" not in message and target != "codex_core::responses_retry":
        return None
    return {"retry_delay": fields.get("retry_delay")}
