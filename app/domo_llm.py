"""LangChain LLM wrapper for the Domo AI Text Generation API (see setup guide, Appendix A)."""
import time
from typing import Any, List, Optional

import httpx
from langchain_core.language_models.llms import LLM

from .config import settings

AGENT_RESPONSE_SCHEMA = {
    "type": "json",
    "schema": {
        "type": "object",
        "properties": {
            "thought": {"type": "string", "description": "short reasoning"},
            "action": {"type": "string", "description": "tool name or 'final'"},
            "input": {"type": "object", "description": "tool arguments", "properties": {}},
            "answer": {"type": "string", "description": "final answer in markdown"},
        },
        "required": ["action"],
    },
}


class DomoLLMError(RuntimeError):
    pass


def domo_headers() -> dict:
    return {"X-DOMO-Developer-Token": settings.domo_token, "Content-Type": "application/json"}


def domo_url(path: str) -> str:
    return f"https://{settings.domo_subdomain}.domo.com{path}"


def _log_usage(data: dict, purpose: str, latency_ms: int, prompt: str = "", output: str = "",
               prompt_version: str | None = None) -> None:
    """Log every LLM call: model, tokens, latency, prompt version, input hash, user, full input and output.
    Never let logging break a chat."""
    try:
        import hashlib

        from .auth import current_user
        from .db import AiUsage, SessionLocal
        u = data.get("modelProviderUsage") or {}
        usr = current_user()
        with SessionLocal() as s:
            s.add(AiUsage(model_id=data.get("modelId"), purpose=purpose, latency_ms=latency_ms,
                          input_tokens=int(u.get("inputTokens") or 0), output_tokens=int(u.get("outputTokens") or 0),
                          total_tokens=int(u.get("totalTokens") or 0), prompt_version=prompt_version,
                          input_hash=hashlib.sha256(prompt.encode()).hexdigest()[:16], username=usr.username if usr else None,
                          input_text=prompt[:20000], output_text=str(output)[:20000]))
            s.commit()
    except Exception:
        pass


class DomoTextLLM(LLM):
    temperature: float = settings.domo_temperature
    max_tokens: int = settings.domo_max_tokens
    system: Optional[str] = None
    json_mode: bool = settings.domo_json_mode
    retries: int = 2
    purpose: str = "agent"          # tag for AI-usage logging (agent | narrative | ...)
    prompt_version: str = "agent-v2"  # bump whenever the prompt template changes (logged with every call)

    @property
    def _llm_type(self) -> str:
        return "domo-text-generation"

    def _call(self, prompt: str, stop: Optional[List[str]] = None,
              run_manager=None, **kwargs: Any) -> str:
        if not settings.llm_configured:
            raise DomoLLMError("DOMO_TOKEN is not set in backend/.env")
        payload: dict = {"input": prompt, "temperature": self.temperature, "maxTokens": self.max_tokens}
        if settings.domo_model:
            payload["model"] = settings.domo_model
        if self.system:
            payload["system"] = self.system
        if self.json_mode:
            payload["responseFormat"] = AGENT_RESPONSE_SCHEMA
        if kwargs.get("session_id"):
            payload["sessionId"] = kwargs["session_id"]

        last_err: Exception | None = None
        t0 = time.perf_counter()
        for attempt in range(self.retries + 1):
            try:
                r = httpx.post(domo_url("/api/ai/v1/text/generation"), json=payload,
                               headers=domo_headers(), timeout=120)
                if r.status_code == 403:
                    raise DomoLLMError("Domo returned 403: check DOMO_TOKEN and that the model is "
                                       "enabled for Text Generation.")
                if r.status_code in (404, 409) and "sessionId" in payload:
                    # Domo only accepts session IDs it created (404 DS-0079 otherwise); guide §13: retry without it
                    payload.pop("sessionId")
                    r = httpx.post(domo_url("/api/ai/v1/text/generation"), json=payload,
                                   headers=domo_headers(), timeout=120)
                if r.status_code >= 500 or r.status_code == 429:
                    raise httpx.HTTPStatusError(f"Domo {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                data = r.json()
                _log_usage(data, self.purpose, int((time.perf_counter() - t0) * 1000), prompt, data.get("output") or "",
                           self.prompt_version)
                out = data.get("output")
                if isinstance(out, (dict, list)):
                    import json
                    out = json.dumps(out)
                out = out or ""
                for s in stop or []:
                    out = out.split(s)[0]
                return out
            except DomoLLMError:
                raise
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise DomoLLMError(f"Domo AI request failed: {last_err}")
