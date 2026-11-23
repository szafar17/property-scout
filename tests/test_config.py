"""Tests for config YAML extraction and validation."""

from __future__ import annotations

import pytest

from property_scout.config import extract_yaml_block, parse_config

PAGE = """
Some runbook intro text.

```yaml
filters:
  counties: [Bergen, Passaic, Hudson]
  max_price: 750000
  min_beds: 2
weights:
  price_vs_area_value: 25
  fixer_upside: 20
  trend_20y: 15
  safety: 15
  location_convenience: 15
  carry_costs: 10
commute_anchor:
  label: NYC PABT
  address: "625 8th Ave, New York, NY 10018"
  mode: transit
```

More runbook text after.
"""


def test_extract_and_parse_roundtrip():
    cfg = parse_config(extract_yaml_block(PAGE))
    assert cfg.filters.max_price == 750000
    assert cfg.filters.min_beds == 2
    assert cfg.commute_anchor.label == "NYC PABT"
    assert sum(cfg.weights.model_dump().values()) == 100


def test_missing_yaml_block_raises():
    with pytest.raises(ValueError):
        extract_yaml_block("no fenced block here")


def test_bad_weights_rejected():
    bad = PAGE.replace("carry_costs: 10", "carry_costs: 99")
    with pytest.raises(ValueError):
        parse_config(extract_yaml_block(bad))
