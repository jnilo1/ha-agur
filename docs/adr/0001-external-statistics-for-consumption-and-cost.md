# Consumption and cost are imported as external statistics, with user-entered price periods

Up to 0.1.5 the consumption history was imported under the id of a `total_increasing` sensor, so that a static price
could be attached to it in the Energy dashboard. The recorder also compiles statistics for any sensor with a
`state_class`, starting its own sum at 0 from short-term statistics that the import never writes, so its hourly rows
were interleaved with the imported ones and the dashboard showed a drop of the whole meter total followed by daily
jumps. That static price was also only applied by Home Assistant to live state changes of the sensor, dated when the
integration polled, so it never priced the imported history.

We import both the consumption (`agur:water_consumption_<contract>`) and the cost (`agur:water_cost_<contract>`) as
external statistics, and drop the consumption sensor. Providers expose no price per m³ (checked in the portals'
API and front-end; invoices only have a total including the subscription), so the user enters price periods as config
subentries of the account, one per contract and start date. The cost of each reading is its consumption times the price
in force on its date, recomputed over the whole history the API sends at each update.

## Considered options

- **Keep the sensor, without `state_class`**: stops the compiled rows, but the Energy dashboard flags the source and
  still prices only live changes.
- **Keep the sensor, excluded from the recorder**: the Energy cost sensor needs a recorded entity.
- **Read the price from the invoice PDFs**: the only automatic source, but depends on each provider's layout and a PDF
  dependency, and is only known after the period it applies to. Possible later, to suggest a price.
- **Price periods in an options-flow menu**: simpler to build, but periods are hidden in a form and cannot be edited
  in place; subentries are listed on the integration page with native reconfigure and delete.

## Consequences

- Users pick the two statistics in the Energy dashboard; the static price option is gone.
- The recorder cannot delete single rows: when a reading of the history loses its price (a period removed, or starting
  later), the cost statistic is cleared and rebuilt from the history the API still sends, losing older cost rows.
