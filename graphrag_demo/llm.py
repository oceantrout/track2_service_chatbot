"""Thin wrapper around the Anthropic Messages API.

Everything the GraphRAG demo sends to a model goes through here, so that:

* the model, effort level and refusal fallback are configured once;
* every call is recorded with its token usage, which is what the
  "measure it" checklist item needs;
* a structured-output call always returns JSON that validates against the
  schema, or raises a clear error - never a half-parsed blob.

## Two modes

The endpoint this demo runs against is not always Anthropic's. It may be an
Anthropic-compatible proxy that serves another model and **silently
ignores** `output_config.format`. Sending a schema and getting prose back
is the symptom.

So the module probes the endpoint once, at first use, and picks a mode:

* ``api`` mode   - `output_config.format` is honoured, so the schema is
  enforced server-side. Strict, and the model cannot drift.
* ``prompt`` mode - the schema is not enforced, so the schema is put in the
  prompt instead, the reply is parsed, validated locally, and one repair
  call is allowed if validation fails.

Both modes produce the same validated dict, so nothing downstream needs to
know which one ran.

Verified API shapes (against anthropic 1.2.0):

    output_config = {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": {...}},
    }

`betas` and `fallbacks` are not modelled by anthropic 1.2.0, so they travel
as the beta header and a raw body field.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config
from .schema import validate


class LLMError(RuntimeError):
    """Base class for LLM failures in the demo."""


class LLMRefusal(LLMError):
    """The model declined the request and no fallback served it."""


class StructuredOutputError(LLMError):
    """The model returned something that is not valid against the schema."""


@dataclass
class Capabilities:
    """What the configured endpoint actually supports."""

    served_model: str = ""
    is_anthropic: bool = False
    supports_structured_output: bool = False
    supports_effort: bool = False
    probed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "served_model": self.served_model,
            "is_anthropic": self.is_anthropic,
            "supports_structured_output": self.supports_structured_output,
            "supports_effort": self.supports_effort,
            "probed_at": self.probed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Capabilities":
        return cls(
            served_model=data.get("served_model", ""),
            is_anthropic=data.get("is_anthropic", False),
            supports_structured_output=data.get("supports_structured_output", False),
            supports_effort=data.get("supports_effort", False),
            probed_at=data.get("probed_at", ""),
        )


@dataclass
class Usage:
    """Token accounting for one demo run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    by_label: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, label: str, response) -> None:
        usage = getattr(response, "usage", None)
        with self._lock:
            self.calls += 1
            if usage is None:
                return
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
            self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            bucket = self.by_label.setdefault(
                label, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            bucket["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "by_label": self.by_label,
        }

    def format_cost(self) -> str:
        """Rough cost, using Opus 5.5 list prices when the model is Anthropic.

        On a non-Anthropic endpoint the price is unknown, so the tokens are
        reported and the money column is left out rather than guessed.
        """
        if not self.is_anthropic:
            return f"{self.total_tokens:,} tokens (cost not known for this endpoint)"
        usd = (self.input_tokens / 1_000_000) * 4.0 + (self.output_tokens / 1_000_000) * 20.0
        return f"~${usd:.4f} USD (~SGD {usd * 1.35:.4f})"


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> object | None:
    """Pull a JSON value out of a model reply.

    Handles the three things models actually do: fenced code blocks, a
    preamble sentence before the JSON, and trailing commentary after it.
    """
    if not text:
        return None

    fenced = _FENCE_RE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Scan for a balanced { ... } or [ ... ] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    return None


JSON_INSTRUCTIONS = """\
Return your answer as a single JSON value that matches this JSON Schema.
Rules:
- Output raw JSON only. No markdown fences, no commentary, no explanation.
- The JSON must validate against the schema exactly: every required
  property present, no extra properties.
- Strings must be plain text. Do not include surrounding quotes inside
  string values beyond JSON's own escaping.

JSON Schema:
{schema}
"""


class LLM:
    """A configured Anthropic-compatible client with usage tracking."""

    def __init__(self, model: str | None = None, track: bool = True):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "the `anthropic` package is not installed - run: pip install anthropic"
            ) from exc

        self.model = model or os.environ.get("GRAPHRAG_MODEL") or config.MODEL
        self.usage = Usage()
        self._track = track
        self._capabilities: Capabilities | None = None
        # A bare constructor resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN
        # or the active OAuth profile, in that order.
        self.client = anthropic.Anthropic()

    # -- capabilities --------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        if self._capabilities is None:
            self._capabilities = self._probe()
        return self._capabilities

    def _probe(self) -> Capabilities:
        """Find out what the endpoint really supports.

        The result is cached on disk because the answer cannot change
        without the endpoint changing.
        """
        cache = config.ARTIFACT_DIR / "m0_capabilities.json"
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if data.get("requested_model") == self.model:
                    return Capabilities.from_dict(data)
            except (json.JSONDecodeError, OSError):
                pass

        caps = Capabilities(probed_at=datetime.now(timezone.utc).isoformat())

        probe_schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=64,
                messages=[
                    {"role": "user", "content": "Set ok to true. Output JSON only."}
                ],
                output_config={
                    "format": {"type": "json_schema", "schema": probe_schema}
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"endpoint probe failed: {type(exc).__name__}: {exc}") from exc

        caps.served_model = getattr(response, "model", "") or ""
        caps.is_anthropic = caps.served_model.lower().startswith("claude")

        for block in response.content:
            if getattr(block, "type", None) == "text":
                try:
                    parsed = json.loads(block.text)
                except json.JSONDecodeError:
                    parsed = None
                caps.supports_structured_output = (
                    isinstance(parsed, dict) and parsed.get("ok") is True
                )
                break

        # `effort` is Anthropic-specific; on other endpoints it is either
        # ignored or rejected, so only send it when the served model looks
        # like an Anthropic model.
        caps.supports_effort = caps.is_anthropic

        config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {**caps.to_dict(), "requested_model": self.model}, indent=2
            ),
            encoding="utf-8",
        )
        return caps

    # -- internals ----------------------------------------------------

    def _request(self, **kwargs):
        """One request, with retry on the transient failures."""
        delay = 2.0
        last: Exception | None = None
        for attempt in range(4):
            try:
                return self.client.messages.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status_code", None)
                transient = status is not None and (status == 429 or status >= 500)
                if not transient or attempt == 3:
                    raise
                last = exc
                time.sleep(delay)
                delay *= 2
        raise last  # pragma: no cover

    def _call(self, **kwargs):
        """Send a request, dropping the Anthropic-only fallback fields if
        the endpoint rejects them."""
        try:
            return self._request(**kwargs)
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "fallback" in message or "beta" in message or "unsupported" in message:
                kwargs.pop("extra_body", None)
                kwargs.pop("extra_headers", None)
                return self._request(**kwargs)
            raise

    def _check_response(self, response) -> str:
        """Common response handling; returns the text."""
        if getattr(response, "stop_reason", None) == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise LLMRefusal(f"model declined the request (category={category})")

        text = None
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        if text is None:
            raise StructuredOutputError(
                f"no text block in response (stop_reason={response.stop_reason})"
            )
        return text

    # -- public API ---------------------------------------------------

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict,
        label: str,
        effort: str = config.EXTRACTION_EFFORT,
        max_tokens: int = config.MAX_TOKENS_EXTRACTION,
    ) -> dict:
        """Call the model and return JSON that validates against `schema`."""
        caps = self.capabilities

        if caps.supports_structured_output:
            data = self._structured_via_api(
                system=system,
                prompt=prompt,
                schema=schema,
                effort=effort,
                max_tokens=max_tokens,
            )
        else:
            data = self._structured_via_prompt(
                system=system,
                prompt=prompt,
                schema=schema,
                effort=effort,
                max_tokens=max_tokens,
            )

        errors = validate(data, schema)
        if errors:
            raise StructuredOutputError(
                "output did not match the schema:\n  " + "\n  ".join(errors[:10])
            )
        return data

    def _structured_via_api(
        self, *, system, prompt, schema, effort, max_tokens
    ) -> dict:
        """Schema-constrained output, enforced server-side."""
        output_config = {"format": {"type": "json_schema", "schema": schema}}
        if self.capabilities.supports_effort:
            output_config["effort"] = effort

        response = self._call(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config=output_config,
            extra_headers={"anthropic-beta": config.FALLBACK_BETA},
            extra_body={"fallbacks": "default"},
        )
        if self._track:
            self.usage.record(label, response)
        return json.loads(self._check_response(response))

    def _structured_via_prompt(
        self, *, system, prompt, schema, effort, max_tokens
    ) -> dict:
        """Schema in the prompt, validated locally, one repair attempt."""
        instructions = JSON_INSTRUCTIONS.format(schema=json.dumps(schema, indent=2))

        response = self._call(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": f"{prompt}\n\n{instructions}"}],
            extra_headers={"anthropic-beta": config.FALLBACK_BETA},
            extra_body={"fallbacks": "default"},
        )
        if self._track:
            self.usage.record(label, response)

        text = self._check_response(response)
        data = extract_json(text)
        errors = validate(data, schema) if data is not None else ["no JSON found in reply"]

        if not errors:
            return data

        # One repair attempt: show the model what was wrong with its output.
        repair_response = self._call(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[
                {"role": "user", "content": f"{prompt}\n\n{instructions}"},
                {"role": "assistant", "content": text[:4000]},
                {
                    "role": "user",
                    "content": (
                        "That output was not valid:\n  "
                        + "\n  ".join(errors[:10])
                        + "\n\nReturn the corrected JSON only."
                    ),
                },
            ],
            extra_headers={"anthropic-beta": config.FALLBACK_BETA},
            extra_body={"fallbacks": "default"},
        )
        if self._track:
            self.usage.record(label + ":repair", repair_response)

        repaired_text = self._check_response(repair_response)
        repaired = extract_json(repaired_text)
        repaired_errors = (
            validate(repaired, schema) if repaired is not None else ["no JSON found in reply"]
        )
        if repaired_errors:
            raise StructuredOutputError(
                "output did not match the schema, and the repair attempt also failed:\n  "
                + "\n  ".join(repaired_errors[:10])
                + f"\n--- original ---\n{text[:600]}"
            )
        return repaired

    def text(
        self,
        *,
        system: str,
        prompt: str,
        label: str,
        effort: str = config.ANSWER_EFFORT,
        max_tokens: int = config.MAX_TOKENS_ANSWER,
    ) -> str:
        """Call the model and return plain text."""
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "extra_headers": {"anthropic-beta": config.FALLBACK_BETA},
            "extra_body": {"fallbacks": "default"},
        }
        if self.capabilities.supports_effort:
            kwargs["output_config"] = {"effort": effort}

        response = self._call(**kwargs)
        if self._track:
            self.usage.record(label, response)
        return self._check_response(response).strip()
