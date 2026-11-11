# Agur

Home Assistant integration for the water customer portals built on Agur's platform (Agur, Grand Paris Sud, Eau de
Paris): meter readings, invoices and balance of a customer's contracts, and their water consumption and cost history.

## Language

**Account**:
A login on a provider's customer portal. Holds one or more contracts.
_Avoid_: user, customer

**Contract**:
A water subscription for one delivery point, identified by its contract number. Has one meter at a time.
_Avoid_: subscription, abonnement

**Meter**:
The physical water meter of a contract. Can be replaced, in which case its index starts again from 0.

**Index**:
The cumulative volume shown by a meter at a given time, in liters.
_Avoid_: counter, total

**Reading**:
An index of a meter at a date, as sent by the provider, 24 to 48 hours after that date. Recent readings may be
corrected by the provider afterwards.
_Avoid_: data point, relevé

**Consumption**:
The volume of water used between two consecutive readings of a contract, in m³. Survives meter replacements.

**Consumption statistic**:
The history of a contract's consumption kept by Home Assistant, used as a water source of the Energy dashboard.

**Price period**:
A price per m³ of water (including tax, excluding the subscription) of a contract, in force from a start date until
the start date of the next price period of the same contract. Entered by the user, since providers do not expose it.
_Avoid_: tariff, rate

**Cost**:
The consumption of a reading times the price of the price period in force on its date, in €. A reading before the
first price period of its contract has no cost, which is not the same as costing 0 €.

**Cost statistic**:
The history of a contract's cost kept by Home Assistant, used as the cost of that contract's water source in the
Energy dashboard.

**Re-import window**:
The most recent 30 days of readings, rewritten in the consumption statistic at every update to pick up corrections.
The cost statistic is rewritten over the whole history the provider still sends, so a price period entered late
applies back to its start date.

**Invoice**:
A bill of a contract. Its total includes the subscription and taxes, so a price per m³ cannot be derived from it.

## Relationships

- An **Account** has one or more **Contracts**
- A **Contract** has one **Meter** at a time, and a series of **Readings**
- Two consecutive **Readings** give one **Consumption**
- A **Contract** has zero or more **Price periods**, at most one per start date
- A **Consumption** and the **Price period** in force on its date give one **Cost**

## Example dialogue

> **Dev:** "The user typed last year's price only now. Do we rewrite past **Costs**?"
> **Domain expert:** "Yes, back to the **Price period**'s start date, as long as the provider still sends those
> **Readings**. The **Re-import window** only bounds the **Consumption statistic**."

## Flagged ambiguities

- "Price" in the Energy dashboard means a static price applied to live sensor changes; here a **Price period** applies
  to **Readings** by their date, including history. They are not interchangeable.
