"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import json
import time
import urllib.error
import urllib.request
from http.client import RemoteDisconnected

from ..model_contract import ModelCapabilities, ModelToolCall, ModelTurn

OPENAI_COMPATIBLE_USER_AGENT = "pico/0.1"
MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024


def _positive_optional_int(value):
    if value in (None, ""):
        return None
    parsed = int(value)
    if parsed < 1:
        raise ValueError("model capability limits must be positive")
    return parsed


class ProviderResponseError(RuntimeError):
    def __init__(self, code, message, retryable=False, attempts=1):
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)
        self.attempts = int(attempts)


def _transport_error(exc, backend, attempts=1):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, TimeoutError):
        return ProviderResponseError(
            "provider_timeout",
            f"{backend} request timed out",
            retryable=True,
            attempts=attempts,
        )
    return ProviderResponseError(
        "provider_connection_error",
        f"Could not reach the {backend} backend",
        retryable=True,
        attempts=attempts,
    )


def _retryable_http_status(status):
    return int(status) in {408, 409, 429} or int(status) >= 500


def _backoff(attempt):
    # Bounded exponential backoff.  Delays remain small because the provider
    # timeout is already the dominant end-to-end deadline.
    time.sleep(0.25 * (2 ** attempt))


def _read_response_text(response, backend):
    try:
        body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except TypeError:
        # Some compatible transports and small test doubles expose read()
        # without a size argument. The post-read bound still rejects excess.
        body = response.read()
    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ProviderResponseError(
            "provider_response_too_large",
            f"{backend} response exceeded {MAX_PROVIDER_RESPONSE_BYTES} bytes",
        )
    return body.decode("utf-8")


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.capabilities = ModelCapabilities()
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.capabilities = ModelCapabilities()
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        # Ollama 当前不支持我们这里接入的 prompt cache 语义，
        # 所以 runtime 传下来的缓存参数会被忽略。
        self.last_completion_metadata = {}
        options = {
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        if max_new_tokens is not None:
            options["num_predict"] = int(max_new_tokens)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": options,
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(_read_response_text(response, "Ollama"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, RemoteDisconnected, TimeoutError) as exc:
            raise _transport_error(exc, "Ollama") from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        if data.get("done") is not True:
            raise ProviderResponseError(
                "provider_incomplete",
                "Ollama response ended without done=true",
            )
        text = str(data.get("response", ""))
        if not text.strip():
            raise ProviderResponseError("provider_empty_output", "Ollama returned an empty completed response")
        return text


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _extract_openai_text(data):
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]

    message_parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") not in {None, "message"}:
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str) and text:
                    message_parts.append(text)
    if message_parts:
        return "\n".join(message_parts)

    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_response_from_sse(body_text):
    terminal_type = ""
    terminal_response = None
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                "provider_transport_incomplete",
                "OpenAI-compatible event stream contained malformed JSON",
            ) from exc
        event_type = event.get("type", "")
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            response = event.get("response")
            if not isinstance(response, dict):
                raise ProviderResponseError(
                    "provider_invalid_envelope",
                    f"OpenAI-compatible {event_type} event omitted its response object",
                )
            terminal_type = event_type
            terminal_response = response
    if terminal_response is None:
        raise ProviderResponseError(
            "provider_transport_incomplete",
            "OpenAI-compatible event stream ended without a terminal response event",
        )
    expected_status = terminal_type.removeprefix("response.")
    actual_status = str(terminal_response.get("status", "")).strip()
    if actual_status != expected_status:
        raise ProviderResponseError(
            "provider_invalid_envelope",
            f"terminal event {terminal_type} disagrees with response status {actual_status or 'missing'}",
        )
    return terminal_response


def _validate_openai_response_envelope(data):
    if not isinstance(data, dict):
        raise ProviderResponseError("provider_invalid_envelope", "OpenAI-compatible response is not an object")
    status = str(data.get("status", "")).strip()
    if status not in {"completed", "incomplete", "failed"}:
        raise ProviderResponseError(
            "provider_invalid_envelope",
            f"OpenAI-compatible response has invalid status: {status or 'missing'}",
        )
    if status == "failed":
        error = data.get("error") or "provider reported failed"
        raise ProviderResponseError("provider_failed", f"OpenAI-compatible response failed: {error}")
    return data


def _incomplete_reason(data):
    details = data.get("incomplete_details") or {}
    return str(details.get("reason") or "unknown")


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": (usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


class OpenAICompatibleModelClient:
    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        reasoning_effort=None,
        context_window=None,
        max_output_tokens=None,
    ):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        configured_effort = str(reasoning_effort or "").strip().lower()
        self.reasoning_effort_explicit = bool(configured_effort)
        if configured_effort and configured_effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max"
        }:
            raise ValueError("unsupported reasoning effort")
        self.reasoning_effort = configured_effort or None
        # 当前只在明确支持 prompt cache 语义的后端上启用这条链路，
        # 避免对不支持的后端传一个“看起来统一、其实没意义”的伪参数。
        self.supports_prompt_cache = any(host in self.base_url for host in ("openai.com", "right.codes"))
        self.supports_native_tools = True
        self.capabilities = ModelCapabilities(
            context_window=_positive_optional_int(context_window),
            max_output_tokens=_positive_optional_int(max_output_tokens),
            supports_reasoning=True,
            supports_native_tools=True,
            supports_previous_response_id=False,
            # A generic OpenAI-compatible endpoint must prove this capability;
            # accepting an unknown field is not evidence of compaction support.
            supports_server_compaction=False,
        )
        self.last_completion_metadata = {}

    def _request_responses(self, payload, prompt_cache_key=None, prompt_cache_retention=None, reasoning_effort=None):
        self.last_completion_metadata = {}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        requested_effort = str(reasoning_effort or "").strip().lower()
        effective_effort = self.reasoning_effort if self.reasoning_effort_explicit else requested_effort or self.reasoning_effort
        if effective_effort is not None:
            payload["reasoning"] = {"effort": effective_effort}
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        request_body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request_metadata = {
            "request_body_bytes": len(request_body),
            "request_input_items": len(payload.get("input") or []),
            "request_input_chars": len(
                json.dumps(payload.get("input") or [], ensure_ascii=False)
            ),
            "request_tool_schema_chars": len(
                json.dumps(payload.get("tools") or [], ensure_ascii=False)
            ),
            "request_instruction_chars": len(str(payload.get("instructions") or "")),
        }
        self.last_completion_metadata = dict(request_metadata)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + "/responses",
            data=request_body,
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = _read_response_text(response, "OpenAI-compatible")
                    response_headers = getattr(response, "headers", {}) or {}
                    content_type = response_headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if _retryable_http_status(exc.code) and attempt < attempts - 1:
                    self.last_completion_metadata = {
                        **request_metadata,
                        "transport_retries": attempt + 1,
                    }
                    _backoff(attempt)
                    continue
                code = (
                    "provider_authentication_failed"
                    if exc.code in {401, 403}
                    else "provider_http_error"
                )
                raise ProviderResponseError(
                    code,
                    f"OpenAI-compatible request failed with HTTP {exc.code}: {body}",
                    retryable=_retryable_http_status(exc.code),
                    attempts=attempt + 1,
                ) from exc
            except (urllib.error.URLError, RemoteDisconnected, TimeoutError) as exc:
                if attempt < attempts - 1:
                    self.last_completion_metadata = {
                        **request_metadata,
                        "transport_retries": attempt + 1,
                    }
                    _backoff(attempt)
                    continue
                self.last_completion_metadata = {
                    **request_metadata,
                    "transport_retries": attempt,
                }
                raise _transport_error(exc, "OpenAI-compatible", attempts=attempt + 1) from exc

        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            data = _extract_openai_response_from_sse(body_text)
        else:
            try:
                data = json.loads(body_text)
            except json.JSONDecodeError as exc:
                raise ProviderResponseError(
                    "provider_invalid_envelope",
                    "OpenAI-compatible error: backend returned non-JSON content that could not be parsed"
                ) from exc
        if isinstance(data, dict) and data.get("error"):
            raise ProviderResponseError(
                "provider_failed", f"OpenAI-compatible error: {data['error']}"
            )
        data = _validate_openai_response_envelope(data)
        retries = int(self.last_completion_metadata.get("transport_retries", 0))
        self.last_completion_metadata = {
            **request_metadata,
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details(data),
            "response_status": data["status"],
            "incomplete_reason": _incomplete_reason(data) if data["status"] == "incomplete" else "",
            "reasoning_effort": effective_effort,
        }
        if retries:
            self.last_completion_metadata["transport_retries"] = retries
        return data

    def complete_turn(
        self,
        input_items,
        tools,
        max_new_tokens,
        instructions=None,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        reasoning_effort=None,
    ):
        payload = {
            "model": self.model,
            "input": list(input_items),
            "tools": list(tools),
            "stream": False,
        }
        if max_new_tokens is not None:
            payload["max_output_tokens"] = int(max_new_tokens)
        if tools:
            payload["tool_choice"] = "auto"
            # Prefer serial decisions, but treat this as a preference: some
            # compatible gateways still return batches, which must be retained.
            payload["parallel_tool_calls"] = False
        if instructions:
            payload["instructions"] = str(instructions)
        data = self._request_responses(
            payload,
            prompt_cache_key,
            prompt_cache_retention,
            reasoning_effort=reasoning_effort,
        )
        output = tuple(item for item in (data.get("output") or []) if isinstance(item, dict))
        if data["status"] == "incomplete":
            return ModelTurn(
                kind="incomplete",
                response_id=str(data.get("id", "")),
                output_items=output,
                response_status="incomplete",
                incomplete_reason=_incomplete_reason(data),
            )
        calls = [item for item in output if item.get("type") == "function_call"]
        parsed_calls = []
        call_ids = set()
        for item in calls:
            raw_args = item.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                return ModelTurn(
                    kind="invalid",
                    response_id=str(data.get("id", "")),
                    output_items=output,
                    response_status="completed",
                    protocol_error=f"function call arguments are invalid JSON: {exc}",
                )
            name = str(item.get("name", "")).strip()
            if not isinstance(args, dict):
                return ModelTurn(
                    kind="invalid", response_status="completed",
                    protocol_error="function call arguments must be an object",
                )
            if not name:
                return ModelTurn(
                    kind="invalid",
                    response_id=str(data.get("id", "")),
                    output_items=output,
                    response_status="completed",
                    protocol_error="function call omitted its name",
                )
            call_id = str(item.get("call_id") or item.get("id") or "")
            if call_id and call_id in call_ids:
                return ModelTurn(
                    kind="invalid",
                    response_id=str(data.get("id", "")),
                    output_items=output,
                    response_status="completed",
                    protocol_error="function calls have duplicate call IDs",
                )
            call_ids.add(call_id)
            parsed_calls.append(
                ModelToolCall(
                    name=name,
                    args=args,
                    call_id=call_id,
                )
            )
        if parsed_calls:
            first = parsed_calls[0]
            return ModelTurn(
                kind="tool" if len(parsed_calls) == 1 else "tool_batch",
                text=_extract_openai_text(data),
                tool_name=first.name if len(parsed_calls) == 1 else "",
                tool_args=first.args if len(parsed_calls) == 1 else {},
                call_id=first.call_id if len(parsed_calls) == 1 else "",
                tool_calls=tuple(parsed_calls),
                response_id=str(data.get("id", "")),
                output_items=output,
                response_status="completed",
            )
        text = _extract_openai_text(data)
        if text:
            return ModelTurn(
                kind="final",
                text=text,
                response_id=str(data.get("id", "")),
                output_items=output,
                response_status="completed",
            )
        return ModelTurn(
            kind="invalid",
            response_id=str(data.get("id", "")),
            output_items=output,
            response_status="completed",
            protocol_error="completed response contained no function call or final message",
        )

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        """向 OpenAI-compatible `/responses` 接口发起一次模型调用。

        为什么存在：
        runtime 不应该知道 HTTP 细节、SSE 细节、usage 字段长什么样，
        更不应该自己去判断 prompt cache 参数要不要带。这个函数把这些后端
        细节都包起来，对上层暴露统一的 `complete()` 行为。

        输入 / 输出：
        - 输入：完整 prompt、最大输出 token，以及可选的 prompt cache 参数
        - 输出：模型最终文本；同时把 usage / cached_tokens 等元数据写进
          `self.last_completion_metadata`

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的模型调用阶段，是稳定前缀缓存复用链路真正
        落到 provider API 的地方。
        """
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "stream": False,
        }
        if max_new_tokens is not None:
            payload["max_output_tokens"] = int(max_new_tokens)
        data = self._request_responses(payload, prompt_cache_key, prompt_cache_retention)
        if data["status"] == "incomplete":
            raise ProviderResponseError(
                "provider_incomplete",
                f"OpenAI-compatible response was incomplete: {_incomplete_reason(data)}",
            )
        text = _extract_openai_text(data)
        if not text.strip():
            raise ProviderResponseError(
                "provider_empty_output",
                "OpenAI-compatible completed response contained no final text",
            )
        return text


def _extract_anthropic_text(data):
    texts = []
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return "\n".join(texts)


def _extract_anthropic_metadata(data):
    content_block_types = []
    for item in data.get("content", []):
        if isinstance(item, dict):
            block_type = str(item.get("type", "")).strip()
            if block_type:
                content_block_types.append(block_type)
    usage = data.get("usage") or {}
    return {
        "stop_reason": str(data.get("stop_reason", "") or ""),
        "content_block_types": content_block_types,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout, thinking=None):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.thinking = dict(thinking) if thinking else None
        self.supports_prompt_cache = False
        self.capabilities = ModelCapabilities(output_limit_required=True)
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        # 为了保持统一接口，runtime 仍然会传缓存参数进来；
        # 这里只是显式丢弃，因为当前 Anthropic-compatible 路径没有接缓存复用。
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}
        if max_new_tokens is None:
            raise ValueError(
                "this provider requires an explicit output limit; configure --max-output-cap"
            )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.thinking is not None:
            payload["thinking"] = dict(self.thinking)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = _read_response_text(response, "Anthropic-compatible")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if _retryable_http_status(exc.code) and attempt < attempts - 1:
                    self.last_completion_metadata = {"transport_retries": attempt + 1}
                    _backoff(attempt)
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected, TimeoutError) as exc:
                if attempt < attempts - 1:
                    self.last_completion_metadata = {"transport_retries": attempt + 1}
                    _backoff(attempt)
                    continue
                self.last_completion_metadata = {"transport_retries": attempt}
                raise _transport_error(exc, "Anthropic-compatible", attempts=attempt + 1) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        retries = int(self.last_completion_metadata.get("transport_retries", 0))
        self.last_completion_metadata = _extract_anthropic_metadata(data)
        if retries:
            self.last_completion_metadata["transport_retries"] = retries
        stop_reason = self.last_completion_metadata["stop_reason"] or "unknown"
        content_types = ",".join(self.last_completion_metadata["content_block_types"]) or "none"
        if stop_reason in {"max_tokens", "pause_turn"}:
            raise ProviderResponseError(
                "provider_incomplete",
                f"Anthropic-compatible response was incomplete (stop_reason={stop_reason})",
            )
        if stop_reason not in {"end_turn", "stop_sequence", "tool_use"}:
            raise ProviderResponseError(
                "provider_invalid_envelope",
                f"Anthropic-compatible response has invalid stop_reason={stop_reason}",
            )
        text = _extract_anthropic_text(data)
        if text:
            return text
        raise RuntimeError(
            "Anthropic-compatible response ended before a text block "
            f"(stop_reason={stop_reason}, content_types={content_types})"
        )
