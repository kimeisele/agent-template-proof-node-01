"""Head Agent — the cognitive core of a federation node.

Every federation node needs a Head Agent that perceives its domain,
judges based on rules, acts via NADI, and learns from outcomes.
Subclass this and override domain_perceive/judge/act for your node.

    node = MyHeadAgent(nadi_node)
    node.heartbeat()  # perceive → judge → act → learn → emit
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("head_agent")


class HeadAgent:
    """Base class for federation node Head Agents.

    Subclass and override:
      - domain_perceive() → observations about your domain
      - domain_judge(observations) → decisions based on rules
      - domain_act(decisions) → side effects (NADI emit, state changes)
    """

    agent_type: str = "generic"  # Override: "navigator", "legislator", etc.

    def __init__(self, nadi_node: Any) -> None:
        self.nadi = nadi_node
        self.cycle_count: int = 0
        self._last_observations: dict = {}
        self._last_decisions: list = []

    def heartbeat(self) -> dict[str, Any]:
        """Full cognitive cycle: perceive → judge → act → learn → emit."""
        self.cycle_count += 1
        t0 = time.time()

        # 1. Perceive
        observations = self.domain_perceive()
        self._last_observations = observations

        # 2. Judge (deterministic, zero LLM)
        decisions = self.domain_judge(observations)
        self._last_decisions = decisions

        # 3. Act
        actions = self.domain_act(decisions)

        # 4. Learn
        self.learn(actions)

        # 5. Emit heartbeat with head_agent field
        self.emit_heartbeat(observations, actions)

        elapsed = time.time() - t0
        log.info(
            "%s heartbeat #%d: %d observations, %d decisions, %d actions (%.1fs)",
            self.agent_type, self.cycle_count,
            len(observations), len(decisions), len(actions), elapsed,
        )
        return {
            "cycle": self.cycle_count,
            "observations": len(observations),
            "decisions": len(decisions),
            "actions": len(actions),
            "elapsed_s": round(elapsed, 2),
        }

    def domain_perceive(self) -> dict[str, Any]:
        """Override: read your domain state. Return observations dict."""
        return {}

    def domain_judge(self, observations: dict[str, Any]) -> list[dict[str, Any]]:
        """Override: apply rules to observations. Return list of decisions."""
        return []

    def domain_act(self, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Override: execute decisions. Return list of action results."""
        return []

    def learn(self, actions: list[dict[str, Any]]) -> None:
        """Override: update internal state based on action outcomes."""
        pass

    def emit_heartbeat(self, observations: dict = None, actions: list = None) -> None:
        """Emit NADI heartbeat with head_agent identification."""
        self.nadi.heartbeat(health=1.0)
        # Emit status with head_agent field
        self.nadi.emit(
            "head_agent_status",
            {
                "head_agent": self.agent_type,
                "cycle": self.cycle_count,
                "observation_count": len(observations or {}),
                "action_count": len(actions or []),
                "timestamp": time.time(),
            },
            priority=1,
        )
