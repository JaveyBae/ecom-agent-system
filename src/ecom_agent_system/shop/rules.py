"""平台规则的参数。模拟器、政策引擎和知识库读同一份 knowledge/platform_rules.yaml。"""

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RULES_PATH = PROJECT_ROOT / "knowledge" / "platform_rules.yaml"


class PlatformRules:
    def __init__(self, document):
        self.tiers = list(document["tiers"])
        self.zones = list(document["zones"])
        self.params = {rule["id"]: dict(rule.get("params") or {}) for rule in document["rules"]}
        self.legal_basis = {rule["id"]: list(rule.get("legal_basis") or []) for rule in document["rules"]}

    @classmethod
    def load(cls, path=RULES_PATH):
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def get(self, rule_id, name):
        return self.params[rule_id][name]

    def tier_at_least(self, tier, minimum):
        return self.tiers.index(tier) >= self.tiers.index(minimum)
