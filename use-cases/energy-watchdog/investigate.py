"""Reference logic for the one investigator decision that must be exactly reproducible.

Like detectors.py, this is NOT a tool and NOT called by any agent - it is the deterministic
statement of the dismissal-suppression rule the investigator applies, so the test suite can
check "a dismissed baseline suppresses the next day's alert" with no LLM in the loop. The
investigator's instructions track this rule in prose.
"""

from __future__ import annotations

from typing import Any


def dismissal_suppresses(
    metric: str,
    target_value: float,
    active_dismissal: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Decide whether an active dismissal still covers a fresh candidate for the same metric.

    A candidate is suppressed (classified `explained`) while its target-day value stays within
    `suppress_margin_pct` of the value the human dismissed at - the accepted-as-normal condition
    has not measurably worsened. Once it exceeds that margin the condition has moved on and the
    candidate is no longer covered.

    :param metric: The candidate's metric (unused today; kept so per-metric rules can diverge).
    :param target_value: The candidate's target-day value in the metric's own units
        (night baseload kW, off-schedule mean kW, or solar yield ratio).
    :param active_dismissal: The active dismissal record from the state store, or None.
    :return: (suppressed, human-readable reason).
    """
    if not active_dismissal:
        return False, "no active dismissal for this metric"

    baseline = active_dismissal.get("value_at_dismissal")
    if baseline is None:
        return False, "the dismissal recorded no value to compare against"

    margin = float(active_dismissal.get("suppress_margin_pct", 10.0)) / 100.0
    ceiling = float(baseline) * (1.0 + margin)
    reason_text = active_dismissal.get("reason", "")

    if target_value <= ceiling:
        return True, (
            f"within {margin * 100:.0f}% of the dismissed value {baseline} "
            f"(<= {ceiling:.2f}); still covered by the dismissal {reason_text!r}"
        )
    return False, (
        f"{target_value:.2f} exceeds the dismissed value {baseline} by more than "
        f"{margin * 100:.0f}% - the condition has worsened past the dismissal"
    )
