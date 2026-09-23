"""Adaptive per-turn model execution and resource policy."""

from dataclasses import asdict, dataclass
from math import ceil

from .interaction_policy import classify_interaction
from .model_contract import ModelCapabilities


def classify_request(user_message):
    """Compatibility projection of the richer per-turn interaction policy."""
    return classify_interaction(user_message).request_profile


def _positive_int(value):
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_output_exhaustion(reason):
    normalized = str(reason or "").casefold()
    return any(
        token in normalized
        for token in ("max_output", "max_tokens", "length", "token")
    )


@dataclass(frozen=True)
class TurnExecutionPolicy:
    reasoning_effort: str
    max_output_tokens: int | None
    max_input_tokens: int | None
    purpose: str
    recovering: bool = False
    recovery_permitted: bool = True
    decision_reason: str = "provider_managed"


@dataclass(frozen=True)
class UsageSample:
    purpose: str
    requested_output_tokens: int | None
    input_tokens: int | None
    output_tokens: int | None
    request_input_chars: int | None
    response_status: str
    incomplete_reason: str


class ModelExecutionPolicy:
    """Compute each turn's limits from capabilities and measured feedback.

    ``max_output_tokens=None`` deliberately means that Pico does not impose a
    smaller arbitrary ceiling. Providers that require a number must advertise
    one through ``ModelCapabilities`` or receive a user hard cap.
    """

    MODES = frozenset({"adaptive", "fast", "deep"})

    def __init__(self, mode="adaptive", usage_samples=None, max_recoveries=2):
        self.mode = str(mode or "adaptive").strip().lower()
        if self.mode not in self.MODES:
            raise ValueError(f"unsupported model execution policy: {self.mode}")
        self.max_recoveries = int(max_recoveries)
        if self.max_recoveries < 1:
            raise ValueError("max_recoveries must be positive")
        self._usage_samples = []
        self.load_usage(usage_samples or [])

    def load_usage(self, samples):
        self._usage_samples = []
        if not isinstance(samples, list):
            return
        for raw in samples[-32:]:
            if not isinstance(raw, dict):
                continue
            self._usage_samples.append(
                UsageSample(
                    purpose=str(raw.get("purpose", "action")),
                    requested_output_tokens=_positive_int(
                        raw.get("requested_output_tokens")
                    ),
                    input_tokens=_positive_int(raw.get("input_tokens")),
                    output_tokens=_positive_int(raw.get("output_tokens")),
                    request_input_chars=_positive_int(
                        raw.get("request_input_chars")
                    ),
                    response_status=str(raw.get("response_status", "")),
                    incomplete_reason=str(raw.get("incomplete_reason", "")),
                )
            )

    def usage_snapshot(self):
        return [asdict(sample) for sample in self._usage_samples[-32:]]

    def observe(self, turn_policy, completion_metadata):
        metadata = dict(completion_metadata or {})
        self._usage_samples.append(
            UsageSample(
                purpose=turn_policy.purpose,
                requested_output_tokens=turn_policy.max_output_tokens,
                input_tokens=_positive_int(metadata.get("input_tokens")),
                output_tokens=_positive_int(metadata.get("output_tokens")),
                request_input_chars=_positive_int(
                    metadata.get("request_input_chars")
                ),
                response_status=str(metadata.get("response_status", "")),
                incomplete_reason=str(metadata.get("incomplete_reason", "")),
            )
        )
        del self._usage_samples[:-32]

    def chars_per_input_token(self):
        ratios = [
            sample.request_input_chars / sample.input_tokens
            for sample in self._usage_samples
            if sample.request_input_chars and sample.input_tokens
        ]
        if not ratios:
            return None
        ratios.sort()
        return ratios[len(ratios) // 2]

    def _reasoning_effort(
        self, purpose, read_only, recovering, request_profile
    ):
        if self.mode == "fast":
            return "none"
        if self.mode == "deep":
            return "high"
        if (
            purpose == "finalization"
            or read_only
            or recovering
            or request_profile == "simple_read_only"
        ):
            return "low"
        return "medium"

    @staticmethod
    def _ceiling(hard_output_cap, capabilities):
        candidates = [
            value
            for value in (
                _positive_int(hard_output_cap),
                _positive_int(capabilities.max_output_tokens),
            )
            if value is not None
        ]
        return min(candidates) if candidates else None

    def for_turn(
        self,
        purpose,
        max_output_tokens=None,
        read_only=False,
        recovering=False,
        request_profile="standard",
        recovery_reason="",
        completion_metadata=None,
        recovery_attempt=0,
        capabilities=None,
    ):
        capabilities = capabilities or ModelCapabilities()
        hard_cap = _positive_int(max_output_tokens)
        ceiling = self._ceiling(hard_cap, capabilities)
        requested = hard_cap
        reason = "user_hard_cap" if hard_cap else "provider_managed"
        recovery_permitted = True

        if requested is None and capabilities.output_limit_required:
            requested = _positive_int(capabilities.max_output_tokens)
            reason = "provider_required_limit"
            if requested is None:
                raise ValueError(
                    "the selected provider requires an output limit; configure "
                    "--max-output-cap or an authoritative provider capability"
                )

        if recovering and _is_output_exhaustion(recovery_reason):
            metadata = dict(completion_metadata or {})
            observed = _positive_int(metadata.get("output_tokens"))
            previous = _positive_int(metadata.get("requested_output_tokens"))
            baseline = observed or previous
            if baseline is not None:
                # Exponential resource backoff starts from actual consumption,
                # not from a phase-specific token constant.
                requested = ceil(baseline * 2)
                if ceiling is not None:
                    requested = min(requested, ceiling)
                reason = "observed_output_exhaustion"
                recovery_permitted = requested > baseline
            elif ceiling is not None:
                requested = ceiling
                reason = "output_exhaustion_to_known_ceiling"
            else:
                requested = None
                reason = "output_exhaustion_without_usage"
                # Permit one same-stage regeneration when a compatible
                # provider omits usage. Repeating the same evidence-free
                # failure is then rejected instead of looping.
                recovery_permitted = int(recovery_attempt) <= 1
        elif recovering:
            reason = "protocol_recovery_without_budget_change"

        if recovering and int(recovery_attempt) > self.max_recoveries:
            recovery_permitted = False
            reason = "recovery_limit_reached"

        return TurnExecutionPolicy(
            reasoning_effort=self._reasoning_effort(
                purpose, read_only, recovering, request_profile
            ),
            max_output_tokens=requested,
            max_input_tokens=(
                max(
                    1,
                    int(capabilities.context_window)
                    - int(requested or capabilities.max_output_tokens or 0),
                )
                if capabilities.context_window
                else None
            ),
            purpose=str(purpose),
            recovering=bool(recovering),
            recovery_permitted=recovery_permitted,
            decision_reason=reason,
        )
