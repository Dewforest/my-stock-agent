from stock_agent.audit import canonical_datetime, canonical_decimal, tagged_sha256
from stock_agent.domain import Bar


def bar_evidence_id_for(item: Bar) -> str:
    if type(item) is not Bar:
        raise TypeError("bar evidence requires an exact Bar")
    item = Bar.model_validate(item)
    return tagged_sha256(
        "bar",
        (
            item.market.value,
            item.symbol,
            item.session_date.isoformat(),
            canonical_decimal(item.open),
            canonical_decimal(item.high),
            canonical_decimal(item.low),
            canonical_decimal(item.close),
            canonical_decimal(item.volume),
            canonical_datetime(item.available_at),
        ),
    )
