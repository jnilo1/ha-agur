# Agur Integration for Home Assistant

Custom integration for Home Assistant to retrieve information about your [Agur account](https://ael.agur.fr) including your water consumption, latest invoice and account balance.

See in English | [Français](./README-FR.md)

## Installation

### Automatic (preferred)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=tbouron&repository=ha-agur)

Or:
- Go to your Home Assistant.
- Open HACS.
- Search for `Agur`.
- Click the `Download` button on the bottom right.
- Restart Home Assistant.

### Manual

- Copy the `agur` folder your `/path/to/home-assistant/custom_components/` directory.
- Restart the Home Assistant.

## Configuration

This custom integration does not support configuration through the `configuration.yaml` file as this is not recommended
anymore by Home Assistant. Instead, head over to Home Assistant -> Settings -> Devices & Services and on the integration
tab, click the `Add Integration` button

![Account setup](./img/config_flow_account_en.png)

Then click the `Submit` button. Your credentials will be validated, and you will then be presented
with various options:
- the available contracts associated to your account that you wish to add
- whether to import statistics to Home Assistant

These options can be changed at a later time to reconfigure this the account.

![Options setup](./img/config_flow_options_en.png)

## Device

For each contract added, the integration create a device corresponding to the meter. The default name used is the meter
serial number, but you can update this at your convenience.

![Integration screen](./img/integration_en.png)

## Sensors

For each contract added, the integration creates a set of 3 sensors

| Id                                       | Name                               | Description                                                                                                  |
|------------------------------------------|------------------------------------|--------------------------------------------------------------------------------------------------------------|
| `sensor.agur_last_index_<contract_id>`   | <meter_serial_number> Last index   | The last index retrieved for that meter (in Liters)                                                          |
| `sensor.agur last_invoice_<contract_id>` | <meter_serial_number> Last invoice | The last invoice generated for that meter (in Euros)                                                         |
| `sensor.agur_balance_<contract_id>`      | <meter_serial_number> Balance      | The accounting balance of the current contract. This sensor is useful to know if you have some amount to pay |

All 3 sensors also have attributes for easy automation consumption

| Id                    | Name                | Description                                                                                   |
|-----------------------|---------------------|-----------------------------------------------------------------------------------------------|
| `date`                | Date                | The actual date at which the state was changed                                                |
| `contract_id`         | Contract ID         | The ID of the associated contract                                                             |
| `contract_owner`      | Contract owner      | The name of the contract owner                                                                |
| `contract_address`    | Contract address    | The address for the associated contract                                                       |
| `meter_serial_number` | Meter serial number | The serial number of the associated meter                                                     |
| `invoice_number`      | Invoice number      | The invoice number (only for sensor `sensor.agur last_invoice_<contract_id>`)                 |
| `payment_date`        | Payment date        | The date when the last payment was made (only for sensor `sensor.agur_balance_<contract_id>`) |

![Sensors](./img/sensors_en.png) 

## Statistics

If you check the option `Import historical statistics for selected contracts` then the consumption history (the last 3
years or so) is imported as the external statistic `agur:water_consumption_<contract_id>` (in m³) and kept up to date
every day, with the actual reading dates. Select that statistic as the water source of the `Energy dashboard` (its id is
also given by the `statistic_id` attribute of `sensor.agur_last_index_<contract_id>`).

The history is deliberately not attached to a sensor: Home Assistant compiles its own statistics for every sensor with a
`state_class`, and they would be mixed with the imported ones. See [the decision record](docs/adr/0001-external-statistics-for-consumption-and-cost.md) for the details.

> [!NOTE]
> The API returns data up to 24 or 48 hours before the actual date, and only once a day. It's unfortunately impossible
> to get a more granular statistics

### Water cost

The Agur API does not give the price of water, so you enter it yourself. The cost of each reading (its consumption
times the price in force on its date) is then imported as the external statistic `agur:water_cost_<contract_id>`
(in €), which the `Energy dashboard` can display next to the consumption.

#### Set the price

1. Go to `Settings` > `Devices & services` > `Agur`.
2. Click `Add price`, at the top of the page, next to `Add entry`.
3. Fill in:
   - `Contract`: only asked if your account has several contracts.
   - `Start date`: the first day the price applies.
   - `Price per m³`: including tax and excluding the subscription, as written on your invoice (e.g. `4.23`).
4. Submit. The price is listed under your account on the same page, where you can change or delete it.

A price applies from its start date until the next price of the same contract: add a new one when the tariff changes,
usually once a year, rather than editing the previous one. Each time a price is added, changed or deleted, the cost is
recomputed for the whole history sent by the API, so a price entered late still applies from its start date. Readings
before the first price of a contract have no cost.

#### Display it on the Energy dashboard

1. Go to `Settings` > `Dashboards` > `Energy`, then the `Water` tab.
2. Under `Water consumption`, click `Add water source` (or the pencil of the existing source).
3. In `Water consumption`, select `Water consumption <contract_id>` (source `Agur`).
4. Under the cost options, choose `Use an entity tracking the total costs`, then select `Water cost <contract_id>`
   (source `Agur`) in `Entity with the total costs`.
5. Save. The cost appears in the `Energy` dashboard, in the water and cost cards.

> [!NOTE]
> `Use a static price` and `Use an entity with the current price` are not suited here: Home Assistant only applies them
> to changes of a sensor, so they would not price the history, nor the corrections made by the provider.

## Languages

This integration provides translations for:

| Language | Complete? |
|----------|-----------|
| EN       | :check:   |
| FR       | :check:   |