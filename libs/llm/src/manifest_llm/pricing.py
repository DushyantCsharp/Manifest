"""List prices, in USD per million tokens, so evals can report what a page costs.

Sources (checked 2026-10-13): platform.claude.com/docs/en/about-claude/pricing (standard,
global routing, no caching or batch discounts) and docs.voyageai.com/docs/pricing. Thinking
tokens are billed as output and are included in a response's output tokens. Update both the
table and the date when prices change; an unknown model has no price rather than a guess.
"""

from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal = Decimal(0)


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(Decimal("4"), Decimal("20")),
    "claude-sonnet-5-5": Price(Decimal("2"), Decimal("10")),
    "claude-haiku-5-5": Price(Decimal("0.10"), Decimal("0.50")),  # prompts up to 100k tokens
    "voyage-3.5": Price(Decimal("0.06")),
}


def cost(model: str, input_tokens: int, output_tokens: int = 0) -> Decimal | None:
    """USD for one call, or None when the model's price isn't known."""
    price = PRICES.get(model.rsplit("/", 1)[-1])
    if price is None:
        return None
    return (input_tokens * price.input + output_tokens * price.output) / MILLION
