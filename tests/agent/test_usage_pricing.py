from decimal import Decimal
from types import SimpleNamespace

import pytest

from agent.usage_pricing import (
    _OFFICIAL_DOCS_PRICING,
    CanonicalUsage,
    format_cost_label,
    estimate_usage_cost,
    get_pricing_entry,
    normalize_usage,
    resolve_billing_route,
)


def test_astra_whole_request_price_tier_includes_cache_writes():
    below = estimate_usage_cost(
        "gpt-6-astra",
        CanonicalUsage(input_tokens=100_000, output_tokens=10_000, cache_read_tokens=10_000, cache_write_tokens=10_000),
        provider="openai",
    )
    above = estimate_usage_cost(
        "gpt-6-astra",
        CanonicalUsage(input_tokens=100_000, output_tokens=10_000, cache_read_tokens=100_000, cache_write_tokens=100_001),
        provider="openai",
    )

    # Whole-request tier: crossing 272K prompt tokens re-prices every component of the request,
    # including cache writes, at the *_above rates — so the cost ratio exceeds the token ratio.
    entry = _OFFICIAL_DOCS_PRICING[("openai", "gpt-6-astra")]
    assert above.amount_usd == (
        Decimal(100_000) * entry.input_cost_per_million_above
        + Decimal(10_000) * entry.output_cost_per_million_above
        + Decimal(100_000) * entry.cache_read_cost_per_million_above
        + Decimal(100_001) * entry.cache_write_cost_per_million_above
    ) / Decimal(1_000_000)
    assert below.amount_usd < above.amount_usd


_MODELS_DEV_REGISTRY = {
    "openai": {"models": {"gpt-5-nano": {"cost": {"input": 0.05, "output": 0.4, "cache_read": 0.005}}}},
    "xai": {"models": {"grok-4.3": {"cost": {"input": 1.25, "output": 2.5, "cache_read": 0.2}}}},
}
_USAGE = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)


@pytest.fixture
def models_dev_registry(monkeypatch):
    """A models.dev cache holding the vendors' rate cards; the providers' own /models carry no prices."""
    from agent import models_dev

    monkeypatch.setattr(models_dev, "_models_dev_cache", _MODELS_DEV_REGISTRY)
    monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", lambda *_a, **_k: {})


@pytest.mark.parametrize(("provider", "base_url", "model", "expected"), [
    ("openai-api", "https://api.openai.com/v1", "gpt-5-nano", ("estimated", Decimal("0.455"))),
    ("openai", "", "gpt-5-nano", ("estimated", Decimal("0.455"))),
    ("xai", "https://api.x.ai/v1", "grok-4.3", ("estimated", Decimal("3.95"))),
    # The vendor's list price needs the vendor's own API: same provider name on
    # someone else's host, a downgraded origin, a subscription route or a custom
    # endpoint keep ``unknown`` rather than inheriting it.
    ("xai", "https://grok-relay.example.com/v1", "grok-4.3", ("unknown", None)),
    ("xai", "http://api.x.ai/v1", "grok-4.3", ("unknown", None)),
    ("xai-oauth", "https://api.x.ai/v1", "grok-4.3", ("unknown", None)),
    ("custom", "https://api.x.ai/v1", "grok-4.3", ("unknown", None)),
])
def test_direct_first_party_route_prices_models_missing_from_snapshot(models_dev_registry, provider, base_url, model, expected):
    cost = estimate_usage_cost(model, _USAGE, provider=provider, base_url=base_url)

    assert (cost.status, cost.amount_usd) == expected


def test_normalize_usage_reads_deepseek_native_cache_hit_tokens():
    """DeepSeek's native API (api.deepseek.com) reports context-cache hits as
    top-level prompt_cache_hit_tokens / prompt_cache_miss_tokens (with
    prompt_tokens = hit + miss), not OpenAI's nested
    prompt_tokens_details.cached_tokens. Before this fix, direct DeepSeek
    sessions always normalized to cache_read_tokens=0 — cache hits were
    invisible in accounting and billed at the full input rate (#61871)."""
    usage = SimpleNamespace(
        prompt_tokens=2000,
        completion_tokens=400,
        prompt_cache_hit_tokens=1500,
        prompt_cache_miss_tokens=500,
    )

    normalized = normalize_usage(usage, provider="deepseek", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 1500
    # prompt_tokens includes cached; input = 2000 - 1500 = the miss bucket
    assert normalized.input_tokens == 500
    assert normalized.output_tokens == 400




def test_normalize_usage_openai_reads_top_level_anthropic_cache_fields():
    """Some OpenAI-compatible proxies (OpenRouter, Vercel AI Gateway, Cline) expose
    Anthropic-style cache token counts at the top level of the usage object when
    routing Claude models, instead of nesting them in prompt_tokens_details.

    Regression guard for the bug fixed in cline/cline#10266 — before this fix,
    the chat-completions branch of normalize_usage() only read
    prompt_tokens_details.cache_write_tokens and completely missed the
    cache_creation_input_tokens case, so cache writes showed as 0 and reflected
    inputTokens were overstated by the cache-write amount.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=500),
        cache_creation_input_tokens=300,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    # Expected: cache read from prompt_tokens_details.cached_tokens (preferred),
    # cache write from top-level cache_creation_input_tokens (fallback).
    assert normalized.cache_read_tokens == 500
    assert normalized.cache_write_tokens == 300
    # input_tokens = prompt_total - cache_read - cache_write = 1000 - 500 - 300 = 200
    assert normalized.input_tokens == 200
    assert normalized.output_tokens == 200


















def test_bundled_pricing_skips_endpoint_metadata(monkeypatch):
    """An exact bundled price must not block on the provider's /models API."""
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_endpoint_model_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("endpoint metadata should not be fetched")
        ),
    )

    entry = get_pricing_entry(
        "deepseek-chat",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
    )

    assert entry is not None
    assert entry.source == "official_docs_snapshot"


def test_unknown_model_falls_back_to_endpoint_metadata(monkeypatch):
    """Models absent from the bundled table still use endpoint pricing."""
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_endpoint_model_metadata",
        lambda *_args, **_kwargs: {
            "deepseek-future": {
                "pricing": {"prompt": "0.000001", "completion": "0.000002"}
            }
        },
    )

    entry = get_pricing_entry(
        "deepseek-future",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
    )

    assert entry is not None
    assert entry.source == "provider_models_api"
    assert entry.input_cost_per_million == Decimal(1)
    assert entry.output_cost_per_million == Decimal(2)




def test_deepseek_deprecated_aliases_price_as_flash():
    """Invariant: deepseek-v4-flash / deepseek-chat / deepseek-reasoner are retired aliases
    served by the current Flash model — they must bill at identical rates to the
    ``deepseek-flash`` entry, or sessions on the legacy names over/under-report cost."""
    flash = get_pricing_entry("deepseek-flash", provider="deepseek")
    assert flash is not None
    for alias in ("deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"):
        entry = get_pricing_entry(alias, provider="deepseek")
        assert entry is not None, alias
        assert entry.input_cost_per_million == flash.input_cost_per_million, alias
        assert entry.output_cost_per_million == flash.output_cost_per_million, alias
        assert (
            entry.cache_read_cost_per_million == flash.cache_read_cost_per_million
        ), alias




def test_bedrock_claude_rows_all_carry_cache_pricing():
    """Invariant: every Bedrock Claude pricing row must carry cache-read AND
    cache-write rates, otherwise a cached session prices as ``unknown``.

    Bedrock Claude routes through the AnthropicBedrock SDK and injects
    cache_control, so cached tokens are always reported — the pricing layer
    must be able to value them.  See #50295.
    """
    from agent.usage_pricing import _OFFICIAL_DOCS_PRICING

    claude_rows = [
        (prov, model)
        for (prov, model) in _OFFICIAL_DOCS_PRICING
        if prov == "bedrock" and "claude" in model
    ]
    assert claude_rows, "expected at least one bedrock Claude pricing row"
    for key in claude_rows:
        entry = _OFFICIAL_DOCS_PRICING[key]
        assert entry.input_cost_per_million is not None, key
        assert entry.cache_read_cost_per_million is not None, key
        assert entry.cache_write_cost_per_million is not None, key
        # Cache reads are cheaper than fresh input; cache writes cost more.
        assert entry.cache_read_cost_per_million < entry.input_cost_per_million, key
        assert entry.cache_write_cost_per_million > entry.input_cost_per_million, key






def test_bedrock_versioned_inference_profile_resolves_to_bare_pricing():
    """Bedrock profile IDs may include the provider's dated version suffix.

    The pricing table intentionally uses shorter model-family IDs, so the
    lookup needs a longest-prefix fallback after stripping the region scope.
    """
    bare = get_pricing_entry("anthropic.claude-sonnet-4-6", provider="bedrock")
    assert bare is not None

    for model in (
        "us.anthropic.claude-sonnet-4-6-20250514-v1:0",
        "global.anthropic.claude-sonnet-4-6-20250514-v1:0",
    ):
        scoped = get_pricing_entry(model, provider="bedrock")
        assert scoped is not None, model
        assert scoped.input_cost_per_million == bare.input_cost_per_million
        assert scoped.output_cost_per_million == bare.output_cost_per_million
        assert scoped.cache_read_cost_per_million == bare.cache_read_cost_per_million
        assert scoped.cache_write_cost_per_million == bare.cache_write_cost_per_million






def test_bedrock_claude_cached_session_estimates_cost_not_unknown():
    """A Bedrock Claude session with cache hits must produce a dollar estimate,
    not ``unknown`` — the user-visible symptom in #50295.
    """
    bedrock_url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    usage = SimpleNamespace(
        input_tokens=55,
        output_tokens=7113,
        cache_read_input_tokens=1369379,
        cache_creation_input_tokens=42135,
    )
    canonical = normalize_usage(usage, provider="bedrock", api_mode="anthropic_messages")
    assert canonical.cache_read_tokens == 1369379
    assert canonical.cache_write_tokens == 42135

    result = estimate_usage_cost(
        "us.anthropic.claude-opus-4-6",
        canonical,
        provider="bedrock",
        base_url=bedrock_url,
    )
    assert result.status == "estimated"
    assert result.amount_usd is not None


@pytest.mark.parametrize("bare", ["claude-opus-5", "claude-opus-5-5"])
@pytest.mark.parametrize("scope", ["", "us.", "global."])
def test_bedrock_claude_opus_5_prices_like_direct_anthropic(bare, scope):
    """Regression for #100848: Bedrock bills Claude at Anthropic's per-token
    rates, so a Bedrock Opus 5 / 5.5 session must price from the snapshot at
    the direct-Anthropic rates instead of falling through to ``unknown``."""
    direct = get_pricing_entry(bare, provider="anthropic")
    scoped = get_pricing_entry(f"{scope}anthropic.{bare}", provider="bedrock")
    assert direct is not None and scoped is not None
    assert scoped.source == "official_docs_snapshot"
    for field in ("input", "output", "cache_read", "cache_write"):
        attr = f"{field}_cost_per_million"
        assert getattr(scoped, attr) == getattr(direct, attr), attr


def test_static_bedrock_catalog_claude_models_price_from_snapshot():
    """Every Claude id Hermes offers in the offline Bedrock picker must price
    from the official snapshot; a missing row records $0 / ``unknown``."""
    from hermes_cli.models_catalog_static import _PROVIDER_MODELS

    claude_ids = [m for m in _PROVIDER_MODELS["bedrock"] if "anthropic.claude" in m]
    assert claude_ids
    for model in claude_ids:
        entry = get_pricing_entry(model, provider="bedrock")
        assert entry is not None and entry.source == "official_docs_snapshot", model


def test_fireworks_router_fast_tier_prices_distinctly():
    """Fast serving tiers live under accounts/fireworks/routers/<name>-fast and
    bill at higher rates than the standard model — the routing layer's
    rsplit("/", 1) must land on the distinct fast-tier entry."""
    standard = get_pricing_entry(
        "accounts/fireworks/models/kimi-k2p6",
        provider="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
    )
    fast = get_pricing_entry(
        "accounts/fireworks/routers/kimi-k2p6-fast",
        provider="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
    )
    assert standard is not None and fast is not None
    assert fast.input_cost_per_million > standard.input_cost_per_million
    assert fast.output_cost_per_million > standard.output_cost_per_million












def test_google_and_vertex_routes_share_official_pricing_snapshot():
    """Direct Gemini, Vertex, and Vertex's OpenAI-compatible hostname must
    all normalize to the Google official-pricing route.
    """
    routes = (
        resolve_billing_route("model", provider="gemini"),
        resolve_billing_route("google/model", provider="vertex"),
        resolve_billing_route(
            "google/model",
            provider="custom",
            base_url="https://aiplatform.googleapis.com/v1/projects/example",
        ),
    )

    assert all(route.provider == "google" for route in routes)
    assert all(route.billing_mode == "official_docs_snapshot" for route in routes)


def test_vertex_default_model_estimates_cached_usage(monkeypatch):
    """The bundled Vertex profile's default auxiliary model must fall back to
    Google snapshot pricing when the OpenAI-compatible endpoint has no model
    metadata, including for cache-read accounting.
    """
    from providers import get_provider_profile

    monkeypatch.setattr(
        "agent.usage_pricing.fetch_endpoint_model_metadata",
        lambda *_args, **_kwargs: {},
    )
    vertex = get_provider_profile("vertex")
    result = estimate_usage_cost(
        vertex.default_aux_model,
        CanonicalUsage(input_tokens=100, output_tokens=100, cache_read_tokens=100),
        provider=vertex.name,
        base_url=vertex.base_url,
    )

    assert result.status == "estimated"
    assert result.amount_usd is not None and result.amount_usd > 0


def test_curated_google_flash_models_resolve_official_snapshot_pricing(monkeypatch):
    """Every ``google/gemini-*-flash`` model curated for the OpenRouter and Nous
    pickers must also bill through the Google official-docs snapshot on the
    direct Gemini and Vertex routes — a model pickable via the aggregators but
    ``unknown`` to Google-route accounting is a catalog/pricing drift.
    """
    from hermes_cli.models_catalog_static import OPENROUTER_MODELS, _PROVIDER_MODELS

    monkeypatch.setattr(
        "agent.usage_pricing.fetch_endpoint_model_metadata",
        lambda *_args, **_kwargs: {},
    )
    curated = {m for m, _desc in OPENROUTER_MODELS} | set(_PROVIDER_MODELS["nous"])
    flash = sorted(m for m in curated if m.startswith("google/gemini-") and m.endswith("-flash"))
    assert flash, "expected curated google/gemini-*-flash picker entries"
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    for model in flash:
        bare = model.split("/", 1)[1]
        gemini = estimate_usage_cost(bare, usage, provider="gemini")
        vertex = estimate_usage_cost(model, usage, provider="vertex")
        assert gemini.status == "estimated", (model, gemini.status)
        assert gemini.source == "official_docs_snapshot", model
        assert vertex.amount_usd == gemini.amount_usd, model
        # Direct-route models the picker offers must also be pickable directly.
        assert bare in _PROVIDER_MODELS["gemini"], model
        assert model in _PROVIDER_MODELS["vertex"], model






# ---------------------------------------------------------------------------
# Cost label formatting (#79220: sub-cent costs render as $0.00)
# ---------------------------------------------------------------------------


class TestFormatCostLabel:
    """Tests for magnitude-scaled cost label formatting."""

    def test_zero_renders_as_dollar_zero(self):
        assert format_cost_label(Decimal(0)) == "$0.00"

    def test_sub_cent_renders_4dp(self):
        """Costs below $0.01 render at 4 decimal places (#79220)."""
        label = format_cost_label(Decimal("0.004640"))
        assert label == "~$0.0046"
        # Must NOT be $0.00
        assert "$0.00" != label

    def test_exactly_one_cent_renders_2dp(self):
        """$0.01 renders at 2dp."""
        assert format_cost_label(Decimal("0.01")) == "~$0.01"

    def test_normal_cost_renders_2dp(self):
        assert format_cost_label(Decimal("1.23")) == "~$1.23"



    def test_below_4dp_floor_never_reads_zero(self):
        """Amounts below $0.00005 must not render as '~$0.0000' (#79220).

        4dp truncation of a positive amount would produce a zero-looking
        label — the exact dishonesty the formatter exists to fix.
        """
        label = format_cost_label(Decimal("0.00004"))
        assert label == "~$<0.0001"
        # Exact boundary: $0.00005 rounds to 0.0000 under ROUND_HALF_EVEN
        # and must also take the fallback.
        assert format_cost_label(Decimal("0.00005")) == "~$<0.0001"



# ---------------------------------------------------------------------------
# Subscription-included cost notes
# ---------------------------------------------------------------------------


class TestSubscriptionIncludedNotes:
    """Subscription-included costs should carry a note clarifying no invoice."""

    def test_included_cost_has_note(self):
        """estimate_usage_cost for subscription-included route includes a note."""
        # openai-codex is subscription_included
        usage = CanonicalUsage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
        )
        result = estimate_usage_cost(
            "gpt-5.4-mini",
            usage,
            provider="openai-codex",
        )
        assert result.status == "included"
        assert result.amount_usd == Decimal(0)
        assert len(result.notes) > 0


def test_normalize_usage_reads_kimi_top_level_cached_tokens():
    """Kimi/Moonshot's native API reports context-cache hits as a top-level
    usage.cached_tokens, not OpenAI's nested
    prompt_tokens_details.cached_tokens and not DeepSeek's
    prompt_cache_hit_tokens. Neither existing fallback matches that name, so
    direct Kimi sessions normalized to cache_read_tokens=0 — the hits were
    invisible in accounting and billed at the full input rate (#65722)."""
    usage = SimpleNamespace(
        prompt_tokens=3000,
        completion_tokens=250,
        cached_tokens=1800,
    )

    normalized = normalize_usage(usage, provider="kimi", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 1800
    # prompt_tokens includes the cached prefix: 3000 - 1800 = fresh input
    assert normalized.input_tokens == 1200
    assert normalized.output_tokens == 250


def test_kimi_fallback_does_not_override_the_nested_openai_shape():
    """A provider that reports BOTH shapes must keep the nested value.

    The new branch is last in the chain, so it only fills a genuine zero.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=400),
        cached_tokens=999,  # must be ignored
    )

    normalized = normalize_usage(usage, provider="kimi", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 400


def test_kimi_fallback_does_not_override_deepseek_hit_tokens():
    usage = SimpleNamespace(
        prompt_tokens=2000,
        completion_tokens=100,
        prompt_cache_hit_tokens=1500,
        cached_tokens=999,  # must be ignored
    )

    normalized = normalize_usage(usage, provider="deepseek", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 1500


def test_usage_without_any_cache_fields_still_normalizes():
    usage = SimpleNamespace(prompt_tokens=500, completion_tokens=50)

    normalized = normalize_usage(usage, provider="kimi", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 0
    assert normalized.input_tokens == 500


def test_normalize_usage_handles_dict_shaped_usage():
    """Regression test for #74314: when the Responses API returns usage as a
    plain dict (e.g. from a middleware/proxy that deserialises JSON to dict
    instead of a typed SDK object), normalize_usage() must read the same
    token counts as it would from an attribute-style object.

    Before this fix, getattr() on a dict silently returned 0 for every field,
    so token counts and cost appeared as zero for dict-shaped usage.
    """
    # Same payload as both a dict and a SimpleNamespace
    payload = {
        "input_tokens": 100,
        "output_tokens": 20,
        "input_tokens_details": {"cached_tokens": 60, "cache_creation_tokens": 10},
    }
    ns = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=60, cache_creation_tokens=10),
    )

    dict_result = normalize_usage(payload, api_mode="codex_responses")
    ns_result = normalize_usage(ns, api_mode="codex_responses")

    assert dict_result.input_tokens == ns_result.input_tokens, f"input_tokens: dict={dict_result.input_tokens} vs ns={ns_result.input_tokens}"
    assert dict_result.output_tokens == ns_result.output_tokens, f"output_tokens: dict={dict_result.output_tokens} vs ns={ns_result.output_tokens}"
    assert dict_result.cache_read_tokens == ns_result.cache_read_tokens, f"cache_read: dict={dict_result.cache_read_tokens} vs ns={ns_result.cache_read_tokens}"
    assert dict_result.cache_write_tokens == ns_result.cache_write_tokens, f"cache_write: dict={dict_result.cache_write_tokens} vs ns={ns_result.cache_write_tokens}"
    # Sanity: values must be non-zero (the whole point of the bug)
    assert dict_result.input_tokens > 0
    assert dict_result.cache_read_tokens > 0


def test_normalize_usage_handles_dict_openai_chat_completions():
    """Dict-shaped usage must also work in the default (OpenAI chat-completions)
    branch, not just the codex_responses branch.
    """
    payload = {
        "prompt_tokens": 500,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 200},
        "completion_tokens_details": {"reasoning_tokens": 30},
    }

    result = normalize_usage(payload, api_mode="chat_completions")

    assert result.output_tokens == 100
    assert result.cache_read_tokens == 200
    assert result.input_tokens == 500 - 200  # prompt_total - cache_read
    assert result.reasoning_tokens == 30


def test_normalize_usage_openai_reads_nested_cache_creation_tokens():
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=100,
            cache_creation_input_tokens=300,
        ),
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 100
    assert normalized.cache_write_tokens == 300
    assert normalized.input_tokens == 600


def test_normalize_usage_openai_reads_mapping_cache_creation_tokens():
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cache_creation_input_tokens": 300},
    }

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.cache_write_tokens == 300
    assert normalized.input_tokens == 700


def test_normalize_usage_openai_prefers_nested_cache_write_tokens():
    usage = SimpleNamespace(
        prompt_tokens=1000,
        prompt_tokens_details=SimpleNamespace(
            cache_write_tokens=200,
            cache_creation_input_tokens=300,
        ),
        cache_creation_input_tokens=400,
        cache_write_tokens=500,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.cache_write_tokens == 200


def test_normalize_usage_mapping_preserves_reasoning_tokens():
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 40},
        "completion_tokens_details": {"reasoning_tokens": 12},
    }

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.reasoning_tokens == 12


def test_normalize_usage_mapping_anthropic_fields():
    usage = {
        "input_tokens": 80,
        "output_tokens": 20,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
        "output_tokens_details": {"reasoning_tokens": 7},
    }

    normalized = normalize_usage(usage, provider="anthropic", api_mode="anthropic_messages")

    assert normalized.cache_read_tokens == 50
    assert normalized.cache_write_tokens == 10
    assert normalized.reasoning_tokens == 7


def test_normalize_usage_mapping_codex_fields():
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "input_tokens_details": {
            "cached_tokens": 60,
            "cache_creation_tokens": 10,
        },
        "output_tokens_details": {"reasoning_tokens": 5},
    }

    normalized = normalize_usage(usage, provider="openai-codex", api_mode="codex_responses")

    assert normalized.input_tokens == 30
    assert normalized.cache_read_tokens == 60
    assert normalized.cache_write_tokens == 10
    assert normalized.reasoning_tokens == 5


def test_normalize_usage_clamps_negative_counters():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=-5,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=-10,
            cache_write_tokens=-20,
        ),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=-3),
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.input_tokens == 100
    assert normalized.output_tokens == 0
    assert normalized.cache_read_tokens == 0
    assert normalized.cache_write_tokens == 0
    assert normalized.reasoning_tokens == 0


def test_normalize_usage_clamps_inconsistent_cache_total():
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {
            "cached_tokens": 80,
            "cache_creation_input_tokens": 50,
        },
    }

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.input_tokens == 0
    assert normalized.prompt_tokens == 130


def test_normalize_usage_codex_responses_reads_cache_write_tokens():
    """GPT-5.6+ explicit prompt caching reports cache writes as
    input_tokens_details.cache_write_tokens (billed at 1.25x), per OpenAI's
    documented Responses API schema. Before this fix, the codex_responses
    branch only read the undocumented `cache_creation_tokens` name and always
    normalized cache writes to 0."""
    usage = SimpleNamespace(
        input_tokens=2006,
        output_tokens=400,
        input_tokens_details=SimpleNamespace(cached_tokens=1920, cache_write_tokens=50),
    )

    normalized = normalize_usage(usage, provider="openai", api_mode="codex_responses")

    assert normalized.cache_read_tokens == 1920
    assert normalized.cache_write_tokens == 50
    assert normalized.input_tokens == 2006 - 1920 - 50


def test_normalize_usage_codex_responses_falls_back_to_cache_creation_tokens():
    """If cache_write_tokens is absent, fall back to the legacy
    cache_creation_tokens name rather than reporting 0."""
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=100,
        input_tokens_details=SimpleNamespace(cached_tokens=200, cache_creation_tokens=80),
    )

    normalized = normalize_usage(usage, provider="openai", api_mode="codex_responses")

    assert normalized.cache_write_tokens == 80


def test_normalize_usage_reads_qwen_flat_cached_tokens():
    """Some Alibaba/Qwen regional endpoints report cache reads as a flat
    `usage.cached_tokens` field with no `prompt_tokens_details` wrapper at
    all. Before this fix, those responses fell through every branch and
    normalized to cache_read_tokens=0, undercounting cost."""
    usage = SimpleNamespace(
        prompt_tokens=2000,
        completion_tokens=300,
        cached_tokens=1200,
    )

    normalized = normalize_usage(usage, provider="qwen", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 1200
    assert normalized.input_tokens == 800


def test_normalize_usage_nested_details_win_over_qwen_flat_top_level():
    """When both shapes are present, the nested OpenAI-style value wins and
    the flat Qwen field is not double-read."""
    usage = SimpleNamespace(
        prompt_tokens=2000,
        completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=900),
        cached_tokens=1200,
    )

    normalized = normalize_usage(usage, provider="qwen", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 900
    assert normalized.input_tokens == 1100


# ── Context-tiered pricing (Gemini Pro >200k prompts, #93469) ─────────────


def test_gemini_31_pro_below_tier_threshold_uses_base_rates():
    """Prompts at or below 200k tokens bill at the base rates — the tier
    fields must not change any below-threshold estimate."""
    result = estimate_usage_cost(
        "gemini-3.1-pro",
        CanonicalUsage(input_tokens=100_000, output_tokens=10_000),
        provider="google",
    )
    # 100k * $2/M + 10k * $12/M
    assert result.amount_usd == Decimal("0.32")

    at_threshold = estimate_usage_cost(
        "gemini-3.1-pro",
        CanonicalUsage(input_tokens=200_000, output_tokens=10_000),
        provider="google",
    )
    # Exactly 200k is still the lower tier (Google bills "> 200k" higher).
    # 200k * $2/M + 10k * $12/M
    assert at_threshold.amount_usd == Decimal("0.52")


def test_gemini_31_pro_above_tier_threshold_uses_tiered_rates_whole_request():
    """Once the prompt exceeds 200k tokens the >200k rates ($4 input /
    $18 output per million) apply to the ENTIRE request, not just the
    marginal tokens — matching Google's billing semantics (#93469).

    Before the fix this request priced at 250k*$2/M + 10k*$12/M = $0.62,
    under-counting input 2x and output 1.5x."""
    result = estimate_usage_cost(
        "gemini-3.1-pro",
        CanonicalUsage(input_tokens=250_000, output_tokens=10_000),
        provider="google",
    )
    # 250k * $4/M + 10k * $18/M
    assert result.amount_usd == Decimal("1.18")
    assert result.status == "estimated"


def test_gemini_31_pro_cache_read_tokens_count_toward_tier_and_tier_rate():
    """prompt_tokens (input + cache read + cache write) drives tier selection,
    and cache reads above the threshold bill at the $0.40/M tier rate."""
    result = estimate_usage_cost(
        "gemini-3.1-pro",
        CanonicalUsage(input_tokens=150_000, cache_read_tokens=100_000),
        provider="google",
    )
    # prompt = 250k > 200k → 150k * $4/M + 100k * $0.40/M
    assert result.amount_usd == Decimal("0.64")


def test_gemini_31_pro_preview_alias_shares_tiered_pricing():
    """The provider-emitted preview id aliases the canonical row, so it must
    pick up the tier fields too."""
    result = estimate_usage_cost(
        "gemini-3.1-pro-preview",
        CanonicalUsage(input_tokens=250_000, output_tokens=10_000),
        provider="google",
    )
    assert result.amount_usd == Decimal("1.18")


def test_gemini_25_pro_tiered_rates_with_cache_read_fallback():
    """gemini-2.5-pro tiers at the same 200k threshold ($2.50 input / $15
    output above). Its snapshot has no tiered cache-read rate, so cache reads
    fall back to the base $0.125/M even above the threshold."""
    result = estimate_usage_cost(
        "gemini-2.5-pro",
        CanonicalUsage(input_tokens=250_000, output_tokens=10_000),
        provider="google",
    )
    # 250k * $2.50/M + 10k * $15/M
    assert result.amount_usd == Decimal("0.775")

    with_cache = estimate_usage_cost(
        "gemini-2.5-pro",
        CanonicalUsage(input_tokens=150_000, cache_read_tokens=100_000),
        provider="google",
    )
    # prompt = 250k > 200k → 150k * $2.50/M + 100k * $0.125/M (base fallback)
    assert with_cache.amount_usd == Decimal("0.3875")


def test_flat_entries_unaffected_by_tier_machinery():
    """Entries without tier fields keep pricing every token at the flat rate
    no matter how large the prompt is."""
    entry = get_pricing_entry("gemini-3.1-flash-lite", provider="google")
    assert entry is not None
    assert entry.tier_threshold_tokens is None

    result = estimate_usage_cost(
        "gemini-3.1-flash-lite",
        CanonicalUsage(input_tokens=250_000, output_tokens=10_000),
        provider="google",
    )
    # 250k * $0.25/M + 10k * $1.50/M
    assert result.amount_usd == Decimal("0.0775")


def _anthropic_usage(speed=None):
    """Canonical usage from an Anthropic-shaped usage payload; ``speed`` rides ``raw_usage``."""
    payload = {"input_tokens": 100_000, "output_tokens": 10_000, "cache_read_input_tokens": 200_000, "cache_creation_input_tokens": 50_000}
    if speed:
        payload["speed"] = speed
    return normalize_usage(payload, provider="anthropic", api_mode="anthropic_messages")


def test_anthropic_fast_mode_responses_price_from_the_fast_rate_row():
    from agent.model_metadata import _ANTHROPIC_FAST_MODE_MODELS
    from agent.usage_pricing import _ANTHROPIC_FAST_MODE_PRICING

    # Every model the fast-mode gate sends ``speed`` to has a fast rate and a standard rate.
    assert set(_ANTHROPIC_FAST_MODE_PRICING) == set(_ANTHROPIC_FAST_MODE_MODELS)
    for model in _ANTHROPIC_FAST_MODE_MODELS:
        standard_entry = get_pricing_entry(model, provider="anthropic")
        assert standard_entry is not None
        fast = estimate_usage_cost(model, _anthropic_usage("fast"), provider="anthropic")
        standard = estimate_usage_cost(model, _anthropic_usage(), provider="anthropic")
        assert fast.pricing_version == _ANTHROPIC_FAST_MODE_PRICING[model].pricing_version
        assert standard.pricing_version == standard_entry.pricing_version
        assert fast.amount_usd > standard.amount_usd
        # A response the API reports as standard speed is billed like one without the field.
        assert estimate_usage_cost(model, _anthropic_usage("standard"), provider="anthropic").amount_usd == standard.amount_usd
    # Vendor-prefixed, dotted ids price from the same fast row.
    assert estimate_usage_cost("anthropic/claude-opus-5.5", _anthropic_usage("fast"), provider="anthropic").amount_usd == (
        estimate_usage_cost("claude-opus-5-5", _anthropic_usage("fast"), provider="anthropic").amount_usd
    )


def test_anthropic_fast_response_without_a_fast_rate_is_unknown():
    result = estimate_usage_cost("claude-sonnet-4-6", _anthropic_usage("fast"), provider="anthropic")
    assert result.amount_usd is None
    assert result.status == "unknown"


class TestRouteReportedUsageCost:
    """``usage.cost`` a route reports itself (LiteLLM's final streamed usage chunk) prices a call only when the
    caller vouches for the route (``trust_reported_cost``: the main loop's own route), the route is an exact HTTPS
    host listed in ``pricing.reported_cost_hosts``, and Hermes has no rate of its own for the call."""

    @pytest.fixture
    def listed(self, monkeypatch):
        import hermes_cli.config as config_mod

        monkeypatch.setattr(
            config_mod, "load_config_readonly",
            lambda: {"pricing": {"reported_cost_hosts": [" LiteLLM.Example.com. ", 7, ""]}},
        )
        monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", lambda *a, **k: {})

    @staticmethod
    def _usage(cost):
        return CanonicalUsage(input_tokens=17, output_tokens=8, raw_usage={"cost": cost})

    @staticmethod
    def _price(usage, base_url="https://litellm.example.com/v1", *, trust=True, provider="custom",
               model="vendor/open-model-x"):
        return estimate_usage_cost(model, usage, provider=provider, base_url=base_url, trust_reported_cost=trust)

    def test_listed_https_host_uses_the_reported_cost(self, listed):
        result = self._price(self._usage(4.8077e-06))
        assert result.status == "actual"
        assert result.source == "provider_cost_api"
        assert result.amount_usd == Decimal.from_float(4.8077e-06)
        assert result.pricing_version == "route-reported-usage-cost"
        assert result.label == format_cost_label(Decimal("4.8077e-06"))

    def test_nothing_is_trusted_unless_the_caller_vouches_for_the_route(self, listed):
        assert self._price(self._usage(4.8077e-06), trust=False).status == "unknown"

    def test_a_rate_hermes_has_always_wins(self, listed):
        result = self._price(self._usage(0.000001), provider="anthropic", model="claude-sonnet-4-6")
        assert result.status == "estimated"
        assert result.pricing_version != "route-reported-usage-cost"

    @pytest.mark.parametrize("base_url", [
        "https://other.example.com/v1", "http://litellm.example.com/v1",
        "https://litellm.example.com.evil.test/v1", "https://sub.litellm.example.com/v1", "", None,
    ])
    def test_other_routes_ignore_the_field(self, listed, base_url):
        result = self._price(self._usage(4.8077e-06), base_url)
        assert result.status == "unknown"
        assert result.amount_usd is None

    @pytest.mark.parametrize("cost", [
        True, -1, -0.5, float("nan"), float("inf"), "abc", "0.012", "1e400", 10**400, Decimal(1), None, [1], {"usd": 1},
        10_000.01, 1e12, 2**53 + 1,
    ])
    def test_malformed_cost_is_ignored(self, listed, cost):
        assert self._price(self._usage(cost)).status == "unknown"

    def test_a_zero_quote_is_never_trusted(self, listed):
        assert self._price(self._usage(0)).status == "unknown"
        assert self._price(self._usage(0.0)).status == "unknown"
        assert self._price(CanonicalUsage(raw_usage={"cost": 0})).status == "unknown"
        # Token counts the canonical buckets miss (a raw total only) cannot make a zero quote a $0 charge.
        raw_total = normalize_usage({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 25, "cost": 0})
        assert self._price(raw_total).status == "unknown"

    def test_a_multi_request_usage_carries_no_single_reported_cost(self, listed):
        usage = CanonicalUsage(input_tokens=17, output_tokens=8, request_count=2, raw_usage={"cost": 1.0})
        assert self._price(usage).status == "unknown"

    def test_a_failed_config_read_trusts_no_host(self, monkeypatch):
        import hermes_cli.config as config_mod
        from hermes_cli.config_read_errors import FailedConfigRead

        stale = FailedConfigRead({"pricing": {"reported_cost_hosts": ["litellm.example.com"]}}, error=OSError("EIO"))
        monkeypatch.setattr(config_mod, "load_config_readonly", lambda: stale)
        monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", lambda *a, **k: {})
        assert self._price(self._usage(1.0)).status == "unknown"

    def test_nothing_is_trusted_without_the_setting(self, monkeypatch):
        import hermes_cli.config as config_mod

        monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {"pricing": {"reported_cost_hosts": "x"}})
        monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", lambda *a, **k: {})
        assert self._price(self._usage(1.0)).status == "unknown"

    def test_aggregated_usage_carries_no_reported_cost(self, listed):
        both = self._usage(1.0) + self._usage(2.0)
        assert both.raw_usage is None
        assert self._price(both).status == "unknown"

    def test_known_pricing_is_unchanged_for_a_listed_route(self, listed):
        from agent.usage_pricing import has_known_pricing

        assert not has_known_pricing("vendor/open-model-x", "custom", "https://litellm.example.com/v1")

    @staticmethod
    def _agent(tmp_path, monkeypatch, hosts):
        import hermes_yaml as yaml

        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"pricing": {"reported_cost_hosts": hosts}}), encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", lambda *a, **k: {})
        from run_agent import AIAgent

        return AIAgent(api_key="k", base_url="https://litellm.example.com/v1", provider="custom",
                       api_mode="chat_completions", model="vendor/open-model-x", session_id="t", platform="cli",
                       quiet_mode=True, skip_context_files=True, skip_memory=True, save_trajectories=False,
                       enabled_toolsets=["file"])

    @staticmethod
    def _record(agent, response):
        from agent import turn_usage

        turn_usage.record_response_usage(agent, response, messages=[{"role": "user", "content": "hi"}],
                                         api_call_count=1, api_duration=0.2, compression_attempts=0,
                                         max_compression_attempts=3)

    @staticmethod
    def _response(cost):
        return SimpleNamespace(id=None, model="vendor/open-model-x",
                               usage={"prompt_tokens": 17, "completion_tokens": 8, "total_tokens": 25, "cost": cost})

    def test_core_prices_the_call_once_and_the_hook_carries_that_amount(self, tmp_path, monkeypatch):
        agent = self._agent(tmp_path, monkeypatch, ["litellm.example.com"])
        try:
            response = self._response(4.8077e-06)
            self._record(agent, response)
            assert agent.session_cost_status == "actual"
            assert agent.session_estimated_cost_usd == pytest.approx(4.8077e-06)
            summary = agent._usage_summary_for_api_request_hook(response)
            assert summary["reported_cost"] == pytest.approx(4.8077e-06)
            assert "raw_usage" not in summary
            # Another response object (an older stash, a retry) never borrows this amount.
            assert "reported_cost" not in agent._usage_summary_for_api_request_hook(self._response(4.8077e-06))
        finally:
            agent.close()

    def test_an_unlisted_route_keeps_the_old_summary_shape(self, tmp_path, monkeypatch):
        agent = self._agent(tmp_path, monkeypatch, ["other.example.com"])
        try:
            response = self._response(4.8077e-06)
            self._record(agent, response)
            assert agent.session_cost_status == "unknown"
            assert "reported_cost" not in agent._usage_summary_for_api_request_hook(response)
        finally:
            agent.close()

    @staticmethod
    def _priced(monkeypatch):
        """Every CostResult core computes in ``record_response_usage``, in call order."""
        from agent import turn_usage

        seen, real = [], turn_usage.estimate_usage_cost
        monkeypatch.setattr(turn_usage, "estimate_usage_cost", lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])
        return seen

    @staticmethod
    def _eligible(client):
        """Make ``client`` route-eligible: it sends to the listed base URL and never follows a redirect, so only
        a MoA marker can keep its quote from being trusted."""
        client._client = SimpleNamespace(follow_redirects=False)
        client.base_url = "https://litellm.example.com/v1/"
        return client

    @pytest.mark.parametrize("marker", [
        "control", "slot", "slot_with_listed_base_url", "facade_without_slot", "moa_provider",
    ])
    def test_no_moa_response_is_priced_from_usage_cost(self, tmp_path, monkeypatch, marker):
        """No MoA response is priced from usage.cost: a recorded aggregator slot (with or without a base URL of
        its own), the MoA facade's client with no recorded slot, or the ``moa`` provider. Each case runs on a
        route-eligible client (listed base URL, no redirects), so only its MoA marker keeps the quote out; the
        control, the same client with no marker, is priced from the quote."""
        from agent.moa_loop import MoAClient

        slot = {"model": "vendor/aggregator-y"}
        client, provider = {
            "control": (SimpleNamespace(), "custom"),
            "slot": (SimpleNamespace(last_aggregator_slot=slot), "custom"),
            "slot_with_listed_base_url": (SimpleNamespace(last_aggregator_slot=dict(
                slot, provider="custom", base_url="https://litellm.example.com/v1")), "custom"),
            "facade_without_slot": (MoAClient("default"), "custom"),
            "moa_provider": (SimpleNamespace(), "moa"),
        }[marker]
        agent = self._agent(tmp_path, monkeypatch, ["litellm.example.com"])
        real_client, real_provider = agent.client, agent.provider
        seen = self._priced(monkeypatch)
        try:
            agent.client, agent.provider = self._eligible(client), provider
            if marker == "facade_without_slot":
                assert agent.client.last_aggregator_slot is None
            assert agent.base_url == "https://litellm.example.com/v1"
            response = self._response(4.8077e-06)
            self._record(agent, response)
            summary = agent._usage_summary_for_api_request_hook(response)
            assert seen, "core never priced the response"
            if marker == "control":
                assert [r.pricing_version for r in seen] == ["route-reported-usage-cost"]
                assert summary["reported_cost"] == 4.8077e-06
            else:
                assert all(r.pricing_version != "route-reported-usage-cost" for r in seen)
                assert agent.session_cost_status != "actual"
                assert agent.session_estimated_cost_usd == 0
                assert "reported_cost" not in summary
        finally:
            agent.client, agent.provider = real_client, real_provider
            agent.close()

    @pytest.mark.parametrize("quote", [0.1, 4.8077e-06, 1234.5678901234567, 10_000, 10_000.0])
    def test_a_quote_is_taken_once_as_the_float_core_persists(self, listed, quote):
        """The quote is converted once, to a float: the CostResult amount is exactly that float (fractional
        quotes, up to the bound MAX_REPORTED_COST_USD inclusive), so no later conversion changes it."""
        result = self._price(self._usage(quote))
        assert result.status == "actual"
        assert result.amount_usd == Decimal(float(quote))
        assert Decimal(float(result.amount_usd)) == result.amount_usd

    @staticmethod
    def _load_cap(tmp_path, monkeypatch):
        """nv-cost-cap loaded the way the gateway loads it, owning the post_api_request hook, plus an observer of
        the usage the real dispatch hands to plugins. Returns ``(cap, dispatched_usages)``."""
        import shutil
        from pathlib import Path

        import hermes_yaml as yaml
        from hermes_cli import plugins as plugins_mod
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

        repo = Path(__file__).resolve().parents[2]
        shutil.copytree(repo / "plugins" / "nv-cost-cap", tmp_path / "plugins" / "nv-cost-cap")
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({
            "pricing": {"reported_cost_hosts": ["litellm.example.com"]},
            "plugins": {"enabled": ["nv-cost-cap"], "entries": {"nv-cost-cap": {"settings": {}}}},
        }), encoding="utf-8")
        (tmp_path / "bundled").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "bundled"))
        monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
        manager = PluginManager()
        manager.discover_and_load()
        cap = manager._plugins["nv-cost-cap"]
        assert cap.enabled and cap.error is None, cap.error
        cap.manager = manager
        monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
        monkeypatch.setattr(plugins_mod, "_plugin_managers_by_home", {})
        dispatched = []
        PluginContext(PluginManifest(name="usage-observer", source="user"), manager).register_hook(
            "post_api_request", lambda **kw: dispatched.append(kw.get("usage")))
        return cap, dispatched

    @staticmethod
    def _account(agent, db, cap, dispatched, response, sid, request_id):
        """Run core's accounting and the real post_api_request dispatch for ``response``; return the dispatched
        ``reported_cost``, the delta persisted to state.db and the cap's accrual for the request."""
        from agent.turn_response_intake import _fire_post_api_request_hook

        agent.session_id, agent._session_db_created = sid, False
        TestRouteReportedUsageCost._record(agent, response)
        _fire_post_api_request_hook(
            agent, response, SimpleNamespace(content="ok", tool_calls=None), "stop", api_messages=[],
            api_call_count=1, api_duration=0.2, api_start_time=0.0, api_request_id=request_id,
            effective_task_id="task", turn_id="turn",
        )
        assert db.flush_token_counts()
        conn = cap.module.store._conn()
        try:
            accrued = conn.execute(
                "SELECT SUM(amount_usd) FROM calls WHERE session_id = ? AND api_request_id = ? AND priced = 1",
                (sid, request_id),
            ).fetchone()[0]
        finally:
            conn.close()
        return dispatched[-1]["reported_cost"], db.get_session(sid)["estimated_cost_usd"], accrued

    def test_core_the_hook_and_the_cap_hold_one_usd_value(self, tmp_path, monkeypatch):
        """One USD value from the quote to the cap, through the real paths: core's CostResult amount, the
        per-call delta core persists to state.db, the usage the real post_api_request dispatch hands to plugins
        and nv-cost-cap's accrual through its registered hook are the same number, for fractional quotes."""
        from hermes_state import SessionDB

        agent = self._agent(tmp_path, monkeypatch, ["litellm.example.com"])
        cap, dispatched = self._load_cap(tmp_path, monkeypatch)
        seen = self._priced(monkeypatch)
        db = SessionDB(tmp_path / "state.db")
        try:
            agent._session_db = db
            for n, quote in enumerate((0.1, 1234.5678901234567), start=1):
                reported, persisted, accrued = self._account(
                    agent, db, cap, dispatched, self._response(quote), f"one-value-{n}", f"req-{n}")
                result = seen[-1]
                assert result.pricing_version == "route-reported-usage-cost"
                assert type(reported) is float
                assert Decimal(reported) == result.amount_usd == Decimal(persisted) == Decimal(accrued), (
                    quote, reported, result.amount_usd, persisted, accrued)
        finally:
            db.close()
            agent.close()

    @staticmethod
    def _sse_body(cost):
        """A LiteLLM-style stream: content, finish, then the usage-only final chunk carrying ``usage.cost``."""
        import json

        head = {"id": "c-1", "object": "chat.completion.chunk", "created": 1, "model": "vendor/open-model-x"}
        chunks = [
            dict(head, choices=[{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]),
            dict(head, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
            dict(head, choices=[], usage={"prompt_tokens": 17, "completion_tokens": 8, "total_tokens": 25,
                                          "cost": cost}),
        ]
        return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def test_a_streamed_answer_carries_its_quote_to_core_and_the_cap(self, tmp_path, monkeypatch):
        """The deployed path: a streamed answer through Hermes's own streaming call and per-request client
        factory (only the low-level transport is a mock answering as the listed host with a LiteLLM stream whose
        final usage chunk carries usage.cost). Core prices it from the quote, and the persisted delta, the
        dispatched reported_cost and the cap's accrual are that same number."""
        import httpx

        from agent import process_bootstrap
        from hermes_state import SessionDB

        body = self._sse_body(0.0123)
        hosts = []

        def answer(request):
            hosts.append(request.url.host)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

        agent = self._agent(tmp_path, monkeypatch, ["litellm.example.com"])
        cap, dispatched = self._load_cap(tmp_path, monkeypatch)
        monkeypatch.setattr(process_bootstrap, "_get_proxy_for_base_url", lambda base_url: None)
        monkeypatch.setattr(process_bootstrap, "_get_shared_transport", lambda key, build: httpx.MockTransport(answer))
        seen = self._priced(monkeypatch)
        db = SessionDB(tmp_path / "state.db")
        try:
            agent._session_db = db
            agent._interrupt_requested = False
            response = agent._interruptible_streaming_api_call(
                {"model": "vendor/open-model-x", "messages": [{"role": "user", "content": "hi"}]})
            assert hosts == ["litellm.example.com"]
            assert response.choices[0].message.content == "ok"
            reported, persisted, accrued = self._account(agent, db, cap, dispatched, response, "streamed", "req-s")
            assert [r.pricing_version for r in seen] == ["route-reported-usage-cost"]
            assert agent.session_cost_status == "actual"
            assert reported == persisted == accrued == 0.0123
            assert Decimal(reported) == seen[-1].amount_usd
        finally:
            db.close()
            agent.close()

    def test_a_full_turn_carries_a_streamed_quote_to_core_and_the_cap(self, tmp_path, monkeypatch):
        """The production turn: run_conversation streams the answer through Hermes's own per-request client
        (only the low-level transport is a mock, which answers the usage chunk only when the request asks for it
        with stream_options.include_usage), then the loop itself records the usage and fires the real
        post_api_request dispatch, in its own order. Core prices the call from the quote, and the delta persisted
        to state.db, the dispatched reported_cost and nv-cost-cap's accrual through its own hook are that number."""
        import json

        import httpx

        from agent import process_bootstrap
        from hermes_cli.plugins import PluginContext, PluginManifest
        from hermes_state import SessionDB

        requests = []

        def answer(request):
            if request.url.host != "litellm.example.com":
                return httpx.Response(404)
            body = json.loads(request.content or b"{}")
            requests.append(body)
            if not (body.get("stream") is True and (body.get("stream_options") or {}).get("include_usage") is True):
                return httpx.Response(400, json={"error": {"message": "the test expects stream + include_usage"}})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=self._sse_body(0.0123).encode())

        agent = self._agent(tmp_path, monkeypatch, ["litellm.example.com"])
        cap, dispatched = self._load_cap(tmp_path, monkeypatch)
        request_ids = []
        PluginContext(PluginManifest(name="request-id-observer", source="user"), cap.manager).register_hook(
            "post_api_request", lambda **kw: request_ids.append(kw.get("api_request_id")))
        monkeypatch.setattr(process_bootstrap, "_get_proxy_for_base_url", lambda base_url: None)
        monkeypatch.setattr(process_bootstrap, "_get_shared_transport", lambda key, build: httpx.MockTransport(answer))
        seen = self._priced(monkeypatch)
        db = SessionDB(tmp_path / "state.db")
        try:
            agent._session_db, agent._session_db_created = db, False
            agent._cached_system_prompt = "You are helpful."
            agent.compression_enabled = False
            agent.stream_delta_callback = lambda _text: None
            monkeypatch.setattr(agent, "_save_trajectory", lambda *a, **k: None)
            result = agent.run_conversation("hi")
            assert result["final_response"] == "ok"
            # The turn's one streamed request (a best-effort side task such as a title may also call the host).
            assert len([r for r in requests if r.get("stream") is True]) == 1
            assert [r.pricing_version for r in seen] == ["route-reported-usage-cost"]
            assert db.flush_token_counts()
            sid = agent.session_id
            persisted = db.get_session(sid)["estimated_cost_usd"]
            (request_id,) = request_ids
            conn = cap.module.store._conn()
            try:
                accrued = conn.execute(
                    "SELECT SUM(amount_usd) FROM calls WHERE session_id = ? AND api_request_id = ? AND priced = 1",
                    (sid, request_id),
                ).fetchone()[0]
            finally:
                conn.close()
            reported = dispatched[-1]["reported_cost"]
            assert reported == persisted == accrued == 0.0123, (reported, persisted, accrued)
            assert Decimal(reported) == seen[-1].amount_usd
        finally:
            db.close()
            agent.close()

    @staticmethod
    def _completion_body(cost):
        return {"id": "c-1", "object": "chat.completion", "created": 1, "model": "vendor/open-model-x",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 17, "completion_tokens": 8, "total_tokens": 25, "cost": cost}}

    @pytest.mark.parametrize("follow_redirects", [True, False])
    def test_a_quote_is_trusted_only_from_a_client_that_never_follows_a_redirect(
            self, tmp_path, monkeypatch, follow_redirects):
        """A client that follows redirects may have been answered by another host than the listed base URL, so
        its quote is never trusted. A real OpenAI client over an httpx transport: with redirects followed, the
        listed host's 307 hands the request to an unlisted host whose answer carries ``usage.cost``; a client
        that never follows a redirect only gets the listed host's own answer, and that one is trusted."""
        import httpx
        from openai import OpenAI

        body = self._completion_body(4.8077e-06)

        def answer(request):
            if follow_redirects and request.url.host == "litellm.example.com":
                return httpx.Response(307, headers={"location": "https://unlisted.example.net/v1/chat/completions"})
            return httpx.Response(200, json=body)

        http = httpx.Client(transport=httpx.MockTransport(answer), follow_redirects=follow_redirects)
        client = OpenAI(api_key="k", base_url="https://litellm.example.com/v1", http_client=http, max_retries=0)
        response = client.chat.completions.create(model="vendor/open-model-x",
                                                  messages=[{"role": "user", "content": "hi"}])
        assert response.usage.cost == 4.8077e-06
        agent = self._agent(tmp_path, monkeypatch, ["litellm.example.com"])
        real_client = agent.client
        try:
            agent.client = client
            self._record(agent, response)
            summary = agent._usage_summary_for_api_request_hook(response)
            if follow_redirects:
                assert agent.session_cost_status != "actual"
                assert agent.session_estimated_cost_usd == 0
                assert "reported_cost" not in summary
            else:
                assert agent.session_cost_status == "actual"
                assert summary["reported_cost"] == 4.8077e-06
        finally:
            agent.client = real_client
            client.close()
            agent.close()

    def test_the_production_clients_carry_a_listed_route_quote(self, tmp_path, monkeypatch):
        """The clients Hermes itself builds are eligible: a completion through the production per-request client
        factory (only the low-level transport is a mock answering as the listed host) is priced from its quote,
        and the hook carries the same amount. Guards the positive path against a factory that follows redirects
        or sends elsewhere, which would leave every quote unknown."""
        import httpx

        from agent import process_bootstrap

        body = self._completion_body(4.8077e-06)
        hosts = []

        def answer(request):
            hosts.append(request.url.host)
            return httpx.Response(200, json=body)

        agent = self._agent(tmp_path, monkeypatch, ["litellm.example.com"])
        monkeypatch.setattr(process_bootstrap, "_get_proxy_for_base_url", lambda base_url: None)
        monkeypatch.setattr(process_bootstrap, "_get_shared_transport",
                            lambda key, build: httpx.MockTransport(answer))
        try:
            request_client = agent._create_request_openai_client(reason="chat_completion_request")
            assert request_client is not agent.client
            response = request_client.chat.completions.create(model="vendor/open-model-x",
                                                              messages=[{"role": "user", "content": "hi"}])
            agent._close_request_openai_client(request_client, reason="request_complete")
            assert hosts == ["litellm.example.com"]
            assert agent._request_client_cache["client"] is request_client
            self._record(agent, response)
            assert agent.session_cost_status == "actual"
            assert agent.session_estimated_cost_usd == 4.8077e-06
            assert agent._usage_summary_for_api_request_hook(response)["reported_cost"] == 4.8077e-06
        finally:
            agent.close()

    @pytest.mark.parametrize("slot", ["primary", "request"])
    def test_a_quote_is_trusted_only_from_a_client_on_the_pricing_route(self, tmp_path, monkeypatch, slot):
        """A main-loop client that sends to another origin than the URL core prices against never vouches,
        even when neither follows a redirect: the primary client, or the cached per-request client, on an
        unlisted host while the agent's base URL names the listed one."""
        import httpx
        from openai import OpenAI

        body = self._completion_body(4.8077e-06)
        http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        unlisted = OpenAI(api_key="k", base_url="https://unlisted.example.net/v1", http_client=http, max_retries=0)
        response = unlisted.chat.completions.create(model="vendor/open-model-x",
                                                    messages=[{"role": "user", "content": "hi"}])
        assert response.usage.cost == 4.8077e-06
        agent = self._agent(tmp_path, monkeypatch, ["litellm.example.com"])
        real_client, real_cache = agent.client, getattr(agent, "_request_client_cache", None)
        try:
            assert agent.base_url == "https://litellm.example.com/v1"
            if slot == "primary":
                agent.client = unlisted
            else:
                agent._request_client_cache = {"client": unlisted, "key": None, "poisoned": False, "in_use": False}
            self._record(agent, response)
            assert agent.session_cost_status != "actual"
            assert agent.session_estimated_cost_usd == 0
            assert "reported_cost" not in agent._usage_summary_for_api_request_hook(response)
        finally:
            agent.client, agent._request_client_cache = real_client, real_cache
            unlisted.close()
            agent.close()
