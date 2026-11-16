import asyncio
import contextlib
import contextvars
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import aiohttp
from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel

from config import Settings
from middlewares.analytics_middleware import current_user_id_var
from services.analytics import Analytics

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Raised when all configured LLM providers in the failover chain fail or budget expires."""


class OpenRouterHTTPError(RuntimeError):
    """Raised on non-200 HTTP responses from OpenRouter, carrying the integer HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


@dataclass
class LLMCallMeta:
    """Metadata returned alongside every routed LLM response."""

    provider: str  # "gemini" or "openrouter"
    model: str
    switched: bool = False
    attempts: list[tuple[str, str, float]] = field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


SwitchCallback = Callable[[LLMCallMeta], Awaitable[None]]


@dataclass
class RequestRoutingScope:
    """Per-request context tracking failover events and OpenRouter usage across service calls."""

    user_id: int | None = None
    on_switch: SwitchCallback | None = None
    openrouter_used: bool = False
    openrouter_model: str | None = None
    openrouter_notified: bool = False
    all_failed: bool = False
    calls: list[LLMCallMeta] = field(default_factory=list)


_routing_scope_var: contextvars.ContextVar[RequestRoutingScope | None] = (
    contextvars.ContextVar("llm_routing_scope", default=None)
)


@dataclass
class ChainEntry:
    provider: str  # "gemini" or "openrouter"
    model: str
    timeout_sec: float


@dataclass
class CircuitState:
    consecutive_failures: int = 0
    open_until_monotonic: float = 0.0
    is_open: bool = False
    last_reason: str = ""


class LLMRouter:
    """
    Resilient multi-provider LLM router with task-dependent timeouts, total time budgets,
    per-model circuit breakers, Gemini-URL video calls, and periodic catalog validation.
    """

    OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
    OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
    VALIDATION_INTERVAL_SEC = 6 * 3600  # 6 hours

    def __init__(
        self,
        settings: Settings,
        analytics: Analytics | None = None,
    ) -> None:
        self.settings = settings
        self.analytics = analytics
        self.gemini_client = genai.Client(
            api_key=settings.GEMINI_API_KEY.get_secret_value()
        )

        raw_or_key = (
            settings.OPENROUTER_API_KEY.get_secret_value().strip()
            if settings.OPENROUTER_API_KEY
            else ""
        )
        self._openrouter_api_key: str | None = raw_or_key if raw_or_key else None
        self._openrouter_logged_disabled = False

        # Validated model lists (mutated only by catalog validation)
        self._gemini_fast: str = settings.GEMINI_MODEL_FAST.strip()
        self._gemini_fallbacks: list[str] = self._parse_csv(
            settings.GEMINI_MODEL_FALLBACKS
        )
        self._gemini_slow_backup: str = settings.GEMINI_MODEL_SLOW_BACKUP.strip()
        self._gemini_heavy: str = settings.GEMINI_MODEL.strip()
        self._openrouter_models: list[str] = self._parse_csv(
            settings.OPENROUTER_MODELS
        )
        self._openrouter_mandatory_reasoning: set[str] = set()

        self._circuits: dict[str, CircuitState] = {}
        self._session: aiohttp.ClientSession | None = None
        self._validation_task: asyncio.Task[None] | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()

        if not self._openrouter_api_key:
            logger.info(
                "OPENROUTER_API_KEY is not configured; OpenRouter fallback tier is disabled."
            )
            self._openrouter_logged_disabled = True

    @staticmethod
    def _parse_csv(raw: str) -> list[str]:
        return [item.strip() for item in raw.split(",") if item.strip()]

    @property
    def openrouter_enabled(self) -> bool:
        return bool(self._openrouter_api_key)

    @property
    def active_fast_model(self) -> str:
        if self._gemini_fast:
            return self._gemini_fast
        if self._gemini_fallbacks:
            return self._gemini_fallbacks[0]
        return self._gemini_slow_backup or "none"

    def get_open_circuits(self) -> list[str]:
        """Return human-readable list of currently open model circuit breakers for /version."""
        now = time.monotonic()
        active: list[str] = []
        for key, state in list(self._circuits.items()):
            if state.is_open:
                if now >= state.open_until_monotonic:
                    state.is_open = False
                    state.consecutive_failures = 0
                    logger.warning("Circuit breaker CLOSED for %s", key)
                else:
                    rem = int(round(state.open_until_monotonic - now))
                    active.append(f"{key} ({rem}s, {state.last_reason})")
        return active

    @contextlib.asynccontextmanager
    async def request_context(
        self,
        user_id: int | None = None,
        on_switch: SwitchCallback | None = None,
    ) -> AsyncIterator[RequestRoutingScope]:
        """Context manager used by handlers to receive live switch callbacks and inspect OpenRouter usage."""
        scope = RequestRoutingScope(user_id=user_id, on_switch=on_switch)
        token = _routing_scope_var.set(scope)
        try:
            yield scope
        finally:
            _routing_scope_var.reset(token)

    def _spawn_analytics(
        self, user_id: int | None, event_type: str, **props: Any
    ) -> None:
        if not self.analytics:
            return
        try:
            task = asyncio.create_task(
                self.analytics.log_event(user_id, event_type, **props)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:
            pass

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Cancel background validation loop and close the HTTP session."""
        if self._validation_task and not self._validation_task.done():
            self._validation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._validation_task
        if self._session and not self._session.closed:
            await self._session.close()

    # ─────────────────────────────────────────────────────────────
    # Startup & Periodic 6-Hour Model Catalog Validation
    # ─────────────────────────────────────────────────────────────

    async def start_validation_loop(self) -> None:
        """Run model validation once immediately and schedule a 6-hour background refresh."""
        await self.validate_models_once()
        if self._validation_task is None or self._validation_task.done():
            self._validation_task = asyncio.create_task(
                self._periodic_validation_runner()
            )

    async def _periodic_validation_runner(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.VALIDATION_INTERVAL_SEC)
                await self.validate_models_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Periodic model validation error: %s", exc)

    async def validate_models_once(self) -> None:
        """Validate configured Gemini and OpenRouter model IDs against live provider catalogs."""
        await self._validate_gemini_models()
        await self._validate_openrouter_models()

    async def _validate_gemini_models(self) -> None:
        try:
            def _list_sync() -> set[str]:
                names: set[str] = set()
                for m in self.gemini_client.models.list():
                    name = getattr(m, "name", "") or ""
                    if name.startswith("models/"):
                        names.add(name[len("models/") :])
                    elif name:
                        names.add(name)
                return names

            available = await asyncio.wait_for(
                asyncio.to_thread(_list_sync), timeout=15.0
            )
            if not available:
                return

            if self._gemini_fast and self._gemini_fast not in available:
                logger.warning(
                    "Configured GEMINI_MODEL_FAST '%s' not found in Gemini catalog; dropping it.",
                    self._gemini_fast,
                )
                self._gemini_fast = ""

            valid_fallbacks: list[str] = []
            for m_id in self._gemini_fallbacks:
                if m_id in available:
                    valid_fallbacks.append(m_id)
                else:
                    logger.warning(
                        "Configured GEMINI_MODEL_FALLBACKS entry '%s' not found in Gemini catalog; dropping it.",
                        m_id,
                    )
            self._gemini_fallbacks = valid_fallbacks

            if (
                self._gemini_slow_backup
                and self._gemini_slow_backup not in available
            ):
                logger.warning(
                    "Configured GEMINI_MODEL_SLOW_BACKUP '%s' not found in Gemini catalog; dropping it.",
                    self._gemini_slow_backup,
                )
                self._gemini_slow_backup = ""

            if self._gemini_heavy and self._gemini_heavy not in available:
                logger.warning(
                    "Configured GEMINI_MODEL '%s' not found in Gemini catalog; dropping it.",
                    self._gemini_heavy,
                )
                self._gemini_heavy = ""

        except Exception as exc:
            logger.warning(
                "Could not validate Gemini model catalog (keeping configured IDs): %s",
                exc,
            )

    async def _validate_openrouter_models(self) -> None:
        if not self._openrouter_api_key:
            return

        try:
            session = await self._get_http_session()
            timeout = aiohttp.ClientTimeout(total=15.0)
            async with session.get(
                self.OPENROUTER_MODELS_URL, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OpenRouter /api/v1/models returned HTTP %d; skipping validation.",
                        resp.status,
                    )
                    return
                payload = await resp.json()

            data_list = payload.get("data", [])
            if not isinstance(data_list, list) or not data_list:
                return

            catalog_by_id: dict[str, dict[str, Any]] = {}
            mandatory_set: set[str] = set()

            for item in data_list:
                if not isinstance(item, dict):
                    continue
                m_id = str(item.get("id", "")).strip()
                if not m_id:
                    continue
                catalog_by_id[m_id] = item
                reasoning_cfg = item.get("reasoning")
                if (
                    isinstance(reasoning_cfg, dict)
                    and reasoning_cfg.get("mandatory") is True
                ):
                    mandatory_set.add(m_id)

            self._openrouter_mandatory_reasoning = mandatory_set

            valid_or: list[str] = []
            for m_id in self._openrouter_models:
                if m_id in catalog_by_id:
                    valid_or.append(m_id)
                else:
                    logger.warning(
                        "Configured OPENROUTER_MODELS entry '%s' no longer exists on OpenRouter; dropping it.",
                        m_id,
                    )

            if len(valid_or) < 2:
                needed = 3 - len(valid_or)
                discovered: list[str] = []
                for m_id, item in catalog_by_id.items():
                    if m_id in valid_or or m_id == "openrouter/free":
                        continue
                    pricing = item.get("pricing") or {}
                    prompt_price = str(pricing.get("prompt", "1")).strip()
                    comp_price = str(pricing.get("completion", "1")).strip()
                    if prompt_price not in ("0", "0.0") or comp_price not in (
                        "0",
                        "0.0",
                    ):
                        continue
                    supported_params = item.get("supported_parameters") or []
                    if (
                        not isinstance(supported_params, list)
                        or "structured_outputs" not in supported_params
                    ):
                        continue
                    if m_id in mandatory_set:
                        continue

                    discovered.append(m_id)
                    valid_or.append(m_id)
                    if len(discovered) >= needed:
                        break

                if discovered:
                    logger.info(
                        "Auto-discovered free OpenRouter structured-output replacement models: %s",
                        ", ".join(discovered),
                    )

            self._openrouter_models = valid_or

        except Exception as exc:
            logger.warning(
                "OpenRouter catalog validation unreachable (keeping current models): %s",
                exc,
            )

    # ─────────────────────────────────────────────────────────────
    # Chain Construction & Circuit Breaker Logic
    # ─────────────────────────────────────────────────────────────

    def _build_gemini_fast_models(self) -> list[str]:
        """Return deduplicated list of active Gemini models in the fast chain."""
        models: list[str] = []
        for candidate in [
            self._gemini_fast,
            *self._gemini_fallbacks,
            self._gemini_slow_backup,
        ]:
            if candidate and candidate not in models:
                models.append(candidate)
        return models

    def _build_chain(self, task: str, tier: str) -> list[ChainEntry]:
        """Build the ordered failover chain with task-dependent per-attempt timeouts."""
        entries: list[ChainEntry] = []
        seen: set[tuple[str, str]] = set()

        is_batch_or_heavy = task in ("subtitle_batch", "summary", "heavy")
        fast_timeout = (
            float(self.settings.LLM_TIMEOUT_BATCH_SEC)
            if is_batch_or_heavy
            else float(self.settings.LLM_TIMEOUT_FAST_SEC)
        )
        slow_backup_timeout = (
            float(self.settings.LLM_TIMEOUT_BATCH_SLOW_SEC)
            if is_batch_or_heavy
            else float(self.settings.LLM_TIMEOUT_SLOW_BACKUP_SEC)
        )
        openrouter_timeout = (
            max(40.0, float(self.settings.LLM_TIMEOUT_OPENROUTER_SEC) * 2.0)
            if task == "subtitle_batch"
            else float(self.settings.LLM_TIMEOUT_OPENROUTER_SEC)
        )

        def _add(provider: str, model: str, timeout_sec: float) -> None:
            if not model:
                return
            key = (provider, model)
            if key not in seen:
                seen.add(key)
                entries.append(
                    ChainEntry(
                        provider=provider, model=model, timeout_sec=timeout_sec
                    )
                )

        if tier == "heavy":
            _add(
                "gemini",
                self._gemini_heavy,
                float(self.settings.LLM_TIMEOUT_HEAVY_SEC),
            )

        _add("gemini", self._gemini_fast, fast_timeout)
        for fb in self._gemini_fallbacks:
            _add("gemini", fb, fast_timeout)
        _add("gemini", self._gemini_slow_backup, slow_backup_timeout)

        if self._openrouter_api_key:
            for or_model in self._openrouter_models:
                _add("openrouter", or_model, openrouter_timeout)
            if self.settings.OPENROUTER_USE_FREE_ROUTER:
                _add("openrouter", "openrouter/free", openrouter_timeout)

        return entries

    def _circuit_key(self, provider: str, model: str) -> str:
        return f"{provider}:{model}"

    def _is_circuit_open(self, provider: str, model: str) -> bool:
        key = self._circuit_key(provider, model)
        state = self._circuits.get(key)
        if not state or not state.is_open:
            return False
        now = time.monotonic()
        if now >= state.open_until_monotonic:
            state.is_open = False
            state.consecutive_failures = 0
            logger.warning("Circuit breaker CLOSED for %s/%s", provider, model)
            return False
        return True

    def _record_success(self, provider: str, model: str) -> None:
        key = self._circuit_key(provider, model)
        state = self._circuits.get(key)
        if state and (state.consecutive_failures > 0 or state.is_open):
            was_open = state.is_open
            state.consecutive_failures = 0
            state.is_open = False
            if was_open:
                logger.warning("Circuit breaker CLOSED for %s/%s", provider, model)

    def _record_failure(
        self,
        provider: str,
        model: str,
        reason: str,
        cooldown_override_sec: float | None = None,
    ) -> None:
        key = self._circuit_key(provider, model)
        state = self._circuits.setdefault(key, CircuitState())
        state.consecutive_failures += 1
        state.last_reason = reason
        now = time.monotonic()

        if cooldown_override_sec is not None:
            state.is_open = True
            state.open_until_monotonic = max(
                state.open_until_monotonic, now + cooldown_override_sec
            )
            logger.warning(
                "Circuit breaker OPEN for %s/%s | reason=%s cooldown=%.0fs",
                provider,
                model,
                reason,
                cooldown_override_sec,
            )
        elif state.consecutive_failures >= 2:
            state.is_open = True
            state.open_until_monotonic = now + 120.0
            logger.warning(
                "Circuit breaker OPEN for %s/%s | reason=%s cooldown=120s (consecutive_failures=%d)",
                provider,
                model,
                reason,
                state.consecutive_failures,
            )

    @staticmethod
    def _classify_exception(exc: Exception) -> str:
        """
        Classify provider exceptions using structured HTTP status codes first
        (google.genai.errors.APIError.code, OpenRouterHTTPError.status, aiohttp.ClientResponseError.status),
        falling back to non-numeric message keywords only when no status attribute exists.
        """
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "timeout"

        status_code: int | None = None
        if isinstance(exc, APIError):
            raw_code = getattr(exc, "code", None)
            if isinstance(raw_code, int):
                status_code = raw_code
        elif isinstance(exc, (OpenRouterHTTPError, aiohttp.ClientResponseError)):
            raw_status = getattr(exc, "status", None)
            if isinstance(raw_status, int):
                status_code = raw_status
        else:
            for attr in ("code", "status", "status_code"):
                val = getattr(exc, attr, None)
                if isinstance(val, int) and 100 <= val < 600:
                    status_code = val
                    break

        if status_code is not None:
            if status_code == 404:
                return "404"
            if status_code in (429, 402):
                return "429"
            if 500 <= status_code < 600:
                return "5xx"
            return "invalid"

        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            return "404"
        if (
            "rate limit" in msg
            or "resource_exhausted" in msg
            or "quota" in msg
            or "too many requests" in msg
            or "insufficient credits" in msg
        ):
            return "429"
        if (
            "service unavailable" in msg
            or "internal error" in msg
            or "bad gateway" in msg
            or "gateway timeout" in msg
        ):
            return "5xx"
        return "invalid"

    # ─────────────────────────────────────────────────────────────
    # Core Execution & Provider Implementations
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_tier_and_budget(
        task: str, explicit_budget: float | None, default_budget: int
    ) -> tuple[str, float]:
        if explicit_budget is not None:
            budget = explicit_budget
        elif task in ("subtitle_batch", "heavy", "summary"):
            budget = 90.0
        else:
            budget = float(default_budget)

        tier = "heavy" if task in ("heavy", "summary") else "fast"
        return tier, budget

    async def generate_json(
        self,
        task: str,
        system: str,
        prompt: str,
        schema: type[TModel],
        timeout: float | None = None,
        on_switch: SwitchCallback | None = None,
        budget_sec: float | None = None,
    ) -> tuple[TModel, LLMCallMeta]:
        """Generate and validate a structured Pydantic model across the failover chain."""
        result, meta = await self._execute_chain(
            task=task,
            system=system,
            prompt=prompt,
            schema=schema,
            timeout_override=timeout,
            on_switch=on_switch,
            budget_sec=budget_sec,
        )
        return result, meta

    async def generate_text(
        self,
        task: str,
        prompt: str,
        timeout: float | None = None,
        system: str | None = None,
        on_switch: SwitchCallback | None = None,
        budget_sec: float | None = None,
    ) -> tuple[str, LLMCallMeta]:
        """Generate plain text across the failover chain."""
        result, meta = await self._execute_chain(
            task=task,
            system=system or "You are a helpful, accurate assistant.",
            prompt=prompt,
            schema=None,
            timeout_override=timeout,
            on_switch=on_switch,
            budget_sec=budget_sec,
        )
        return str(result), meta

    async def generate_json_with_video(
        self,
        url: str,
        start_s: int | float | None,
        end_s: int | float | None,
        system: str,
        prompt: str,
        schema: type[TModel],
        timeout: float = 75.0,
    ) -> tuple[TModel, LLMCallMeta]:
        """
        Generate structured JSON from a YouTube URL using ONLY Gemini models of the fast chain
        (never OpenRouter), with MEDIA_RESOLUTION_LOW and temperature=0.
        Retries once on the next Gemini model of the fast chain if the first model fails.
        """
        gemini_models = self._build_gemini_fast_models()
        scope = _routing_scope_var.get()
        user_id = (
            scope.user_id
            if scope and scope.user_id is not None
            else current_user_id_var.get()
        )

        if not gemini_models:
            raise LLMUnavailable("No Gemini models configured for video URL input.")

        available_models = [
            m for m in gemini_models if not self._is_circuit_open("gemini", m)
        ]
        if not available_models:
            available_models = gemini_models[:2]
        else:
            available_models = available_models[:2]

        video_meta = (
            types.VideoMetadata(
                start_offset=f"{int(start_s)}s",
                end_offset=f"{int(end_s)}s",
            )
            if start_s is not None and end_s is not None
            else None
        )
        video_part = types.Part(
            file_data=types.FileData(file_uri=url),
            video_metadata=video_meta,
        )
        text_part = types.Part.from_text(text=prompt)

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.0,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        )

        attempts: list[tuple[str, str, float]] = []
        last_failed_model: str | None = None
        last_failed_reason: str | None = None

        for idx, model_name in enumerate(available_models):
            if last_failed_model is not None and last_failed_reason is not None:
                self._spawn_analytics(
                    user_id,
                    "llm_switch",
                    task="gemini_video",
                    from_model=last_failed_model,
                    to_model=model_name,
                    reason=last_failed_reason,
                )

            t0 = time.monotonic()
            try:
                resp = await asyncio.wait_for(
                    self.gemini_client.aio.models.generate_content(
                        model=model_name,
                        contents=[video_part, text_part],
                        config=cfg,
                    ),
                    timeout=timeout,
                )

                if isinstance(resp.parsed, schema):
                    parsed_obj = resp.parsed
                else:
                    raw_text = (resp.text or "").strip()
                    if not raw_text:
                        raise ValueError("Empty JSON response from Gemini video call")
                    cleaned = self._strip_markdown_fences(raw_text)
                    parsed_obj = schema.model_validate_json(cleaned)

                dur = round(time.monotonic() - t0, 3)
                attempts.append((model_name, "ok", dur))
                self._record_success("gemini", model_name)

                logger.info(
                    "Gemini video call ok | model=%s start_s=%s end_s=%s seconds=%.2f switched=%s",
                    model_name,
                    start_s,
                    end_s,
                    dur,
                    idx > 0,
                )
                self._spawn_analytics(
                    user_id,
                    "llm_call",
                    task="gemini_video",
                    provider="gemini",
                    model=model_name,
                    seconds=dur,
                    ok=True,
                )

                meta = LLMCallMeta(
                    provider="gemini",
                    model=model_name,
                    switched=(idx > 0),
                    attempts=attempts,
                )
                if scope:
                    scope.calls.append(meta)
                return parsed_obj, meta

            except Exception as exc:
                dur = round(time.monotonic() - t0, 3)
                reason = self._classify_exception(exc)
                attempts.append((model_name, reason, dur))
                last_failed_model = model_name
                last_failed_reason = reason
                self._record_failure("gemini", model_name, reason)

                logger.warning(
                    "Gemini video call failed | model=%s start_s=%s end_s=%s seconds=%.2f error_class=%s reason=%s",
                    model_name,
                    start_s,
                    end_s,
                    dur,
                    exc.__class__.__name__,
                    reason,
                )
                self._spawn_analytics(
                    user_id,
                    "llm_call",
                    task="gemini_video",
                    provider="gemini",
                    model=model_name,
                    seconds=dur,
                    ok=False,
                    reason=reason,
                )

        raise LLMUnavailable(
            f"Gemini video call failed after {len(attempts)} attempt(s)."
        )

    async def _execute_chain(
        self,
        task: str,
        system: str,
        prompt: str,
        schema: type[TModel] | None,
        timeout_override: float | None,
        on_switch: SwitchCallback | None,
        budget_sec: float | None,
    ) -> tuple[Any, LLMCallMeta]:
        tier, total_budget = self._resolve_tier_and_budget(
            task, budget_sec, self.settings.LLM_TOTAL_BUDGET_SEC
        )
        chain = self._build_chain(task, tier)
        scope = _routing_scope_var.get()
        user_id = (
            scope.user_id if scope and scope.user_id is not None else current_user_id_var.get()
        )
        effective_on_switch = on_switch or (scope.on_switch if scope else None)

        if not chain:
            if scope:
                scope.all_failed = True
            self._spawn_analytics(user_id, "llm_all_failed", task=task)
            raise LLMUnavailable("No LLM models are available in the configured chain.")

        start_overall = time.monotonic()
        attempts: list[tuple[str, str, float]] = []
        last_failed_model: str | None = None
        last_failed_reason: str | None = None

        for idx, entry in enumerate(chain):
            elapsed_overall = time.monotonic() - start_overall
            remaining_budget = total_budget - elapsed_overall
            if remaining_budget <= 0.25:
                logger.warning(
                    "LLM total budget (%.1fs) exhausted on task='%s' before trying %s/%s",
                    total_budget,
                    task,
                    entry.provider,
                    entry.model,
                )
                break

            if self._is_circuit_open(entry.provider, entry.model):
                logger.info(
                    "Skipping %s/%s (circuit breaker open) for task='%s'",
                    entry.provider,
                    entry.model,
                    task,
                )
                attempts.append((entry.model, "circuit_open", 0.0))
                continue

            if last_failed_model is not None and last_failed_reason is not None:
                self._spawn_analytics(
                    user_id,
                    "llm_switch",
                    task=task,
                    from_model=last_failed_model,
                    to_model=entry.model,
                    reason=last_failed_reason,
                )

            if entry.provider == "openrouter":
                interim_meta = LLMCallMeta(
                    provider="openrouter",
                    model=entry.model,
                    switched=True,
                    attempts=list(attempts),
                )
                if effective_on_switch:
                    try:
                        await effective_on_switch(interim_meta)
                    except Exception as cb_exc:
                        logger.debug("on_switch callback error: %s", cb_exc)

            attempt_timeout = min(entry.timeout_sec, remaining_budget)
            if timeout_override is not None:
                attempt_timeout = min(attempt_timeout, timeout_override)

            t0 = time.monotonic()
            try:
                if entry.provider == "gemini":
                    output = await asyncio.wait_for(
                        self._call_gemini(
                            model=entry.model,
                            system=system,
                            prompt=prompt,
                            schema=schema,
                        ),
                        timeout=attempt_timeout,
                    )
                else:
                    output = await asyncio.wait_for(
                        self._call_openrouter(
                            model=entry.model,
                            system=system,
                            prompt=prompt,
                            schema=schema,
                            timeout_sec=attempt_timeout,
                        ),
                        timeout=attempt_timeout + 0.5,
                    )

                dur = round(time.monotonic() - t0, 3)
                attempts.append((entry.model, "ok", dur))
                self._record_success(entry.provider, entry.model)

                logger.info(
                    "LLM call ok | task=%s provider=%s model=%s seconds=%.2f switched=%s",
                    task,
                    entry.provider,
                    entry.model,
                    dur,
                    idx > 0,
                )
                self._spawn_analytics(
                    user_id,
                    "llm_call",
                    task=task,
                    provider=entry.provider,
                    model=entry.model,
                    seconds=dur,
                    ok=True,
                )

                final_meta = LLMCallMeta(
                    provider=entry.provider,
                    model=entry.model,
                    switched=(idx > 0),
                    attempts=attempts,
                )
                if scope:
                    scope.calls.append(final_meta)
                    if entry.provider == "openrouter":
                        scope.openrouter_used = True
                        scope.openrouter_model = entry.model

                return output, final_meta

            except Exception as exc:
                dur = round(time.monotonic() - t0, 3)
                reason = self._classify_exception(exc)
                attempts.append((entry.model, reason, dur))
                last_failed_model = entry.model
                last_failed_reason = reason

                cooldown_override: float | None = None
                if entry.provider == "openrouter" and reason == "429":
                    cooldown_override = 60.0

                self._record_failure(
                    entry.provider,
                    entry.model,
                    reason,
                    cooldown_override_sec=cooldown_override,
                )

                logger.warning(
                    "LLM call failed | task=%s provider=%s model=%s seconds=%.2f error_class=%s reason=%s",
                    task,
                    entry.provider,
                    entry.model,
                    dur,
                    exc.__class__.__name__,
                    reason,
                )
                self._spawn_analytics(
                    user_id,
                    "llm_call",
                    task=task,
                    provider=entry.provider,
                    model=entry.model,
                    seconds=dur,
                    ok=False,
                    reason=reason,
                )

        if scope:
            scope.all_failed = True
        logger.error(
            "All LLM providers failed for task='%s' after %d attempts.",
            task,
            len(attempts),
        )
        self._spawn_analytics(user_id, "llm_all_failed", task=task)
        raise LLMUnavailable(f"All LLM providers failed for task '{task}'.")

    async def _call_gemini(
        self,
        model: str,
        system: str,
        prompt: str,
        schema: type[TModel] | None,
    ) -> Any:
        if schema is not None:
            cfg = types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.1,
            )
            resp = await self.gemini_client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=cfg,
            )
            if isinstance(resp.parsed, schema):
                return resp.parsed
            raw_text = (resp.text or "").strip()
            if not raw_text:
                raise ValueError("Empty JSON response from Gemini")
            cleaned = self._strip_markdown_fences(raw_text)
            return schema.model_validate_json(cleaned)

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
        )
        resp = await self.gemini_client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=cfg,
        )
        text = (resp.text or "").strip()
        if not text:
            raise ValueError("Empty text response from Gemini")
        return text

    @classmethod
    def _enforce_strict_json_schema(cls, node: Any) -> Any:
        """Ensure object nodes specify additionalProperties=False for OpenRouter strict mode."""
        if isinstance(node, dict):
            result = {k: cls._enforce_strict_json_schema(v) for k, v in node.items()}
            if result.get("type") == "object" and "properties" in result:
                result.setdefault("additionalProperties", False)
            return result
        if isinstance(node, list):
            return [cls._enforce_strict_json_schema(item) for item in node]
        return node

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    async def _call_openrouter(
        self,
        model: str,
        system: str,
        prompt: str,
        schema: type[TModel] | None,
        timeout_sec: float,
    ) -> Any:
        if not self._openrouter_api_key:
            raise RuntimeError("OpenRouter API key not configured")

        session = await self._get_http_session()
        headers = {
            "Authorization": f"Bearer {self._openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/MehrdadNsr/video-source-finder-bot",
            "X-Title": "Video Source Finder Bot",
        }

        sys_content = system
        if schema is not None:
            raw_schema = self._enforce_strict_json_schema(
                schema.model_json_schema()
            )
            sys_content = (
                f"{system}\n\n"
                "Respond ONLY with a valid JSON object matching the requested schema. "
                "Do not include markdown code fences or extra commentary."
            )
        else:
            raw_schema = None

        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_content},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }

        if raw_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__ if schema else "output_schema",
                    "strict": True,
                    "schema": raw_schema,
                },
            }

        if model not in self._openrouter_mandatory_reasoning:
            body["reasoning"] = {"enabled": False, "exclude": True}
        else:
            body["reasoning"] = {"effort": "low", "exclude": True}

        client_timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with session.post(
            self.OPENROUTER_CHAT_URL,
            headers=headers,
            json=body,
            timeout=client_timeout,
        ) as resp:
            status = resp.status
            resp_text = await resp.text()

            if status != 200:
                raise OpenRouterHTTPError(
                    status=status,
                    message=f"OpenRouter HTTP {status}",
                )

            try:
                data = json.loads(resp_text)
            except Exception as exc:
                raise ValueError("Invalid JSON payload from OpenRouter") from exc

            if isinstance(data, dict) and "error" in data:
                err_obj = data["error"]
                err_code = (
                    int(err_obj["code"])
                    if isinstance(err_obj, dict)
                    and isinstance(err_obj.get("code"), int)
                    else 500
                )
                raise OpenRouterHTTPError(
                    status=err_code,
                    message=f"OpenRouter error payload code={err_code}",
                )

            choices = data.get("choices") or []
            if not choices or not isinstance(choices, list):
                raise ValueError("OpenRouter returned empty choices")

            message_obj = choices[0].get("message") or {}
            content = str(message_obj.get("content") or "").strip()
            if not content:
                raise ValueError("OpenRouter returned empty message content")

            if schema is not None:
                cleaned = self._strip_markdown_fences(content)
                return schema.model_validate_json(cleaned)

            return content
