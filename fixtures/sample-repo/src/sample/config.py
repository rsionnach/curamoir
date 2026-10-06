"""Project configuration.

A loose module beside the subpackages, so `src/sample` is itself a gradeable
module. Three modules total is what makes the fixture exercise E5-R3's selector,
which picks modules_per_run=2 plus random_extra=1.
"""

DEFAULT_CURRENCY = "EUR"
RATES_PATH = "rates.json"


def currency_symbol(currency=DEFAULT_CURRENCY):
    return {"EUR": "€", "GBP": "£", "USD": "$"}.get(currency, currency)
