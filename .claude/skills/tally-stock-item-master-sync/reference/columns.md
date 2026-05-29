# Output Schema — Tally Stock Item Master Sheet

This file is the **single source of truth** for the columns written to the
destination Google Sheet by the `tally-stock-item-master-sync` skill. Both
`tools/fetch_tally_stock_item_master.py` and
`tools/push_stock_item_master_to_sheet.py` parse this file at runtime via
`tools/_schema.py`.

To add, remove, or reorder a column:
1. Edit the table below.
2. Run the skill — no Python changes needed.

The first table (`## Columns`) is the one parsed. Order of rows = order of
columns in the sheet. Column names = exact text written to row 1 of the sheet.

The `key` column is the JSON key used inside the intermediate
`.tmp/stock_item_master_*.json` file (lowercase, snake_case, no special chars).
It must be unique.

## Columns

| key                      | column                   | tally_source                                                                                |
|--------------------------|--------------------------|---------------------------------------------------------------------------------------------|
| company                  | Company                  | Mapped from raw Tally name (see companies.md)                                               |
| location                 | Location                 | Mapped from raw Tally name (see companies.md)                                               |
| item_id                  | item_id                  | STOCKITEM → GUID (Tally's internal globally-unique ID, stable on rename)                    |
| item_name                | item_name                | STOCKITEM NAME                                                                              |
| alias_name               | alias_name               | STOCKITEM → second LANGUAGENAME.LIST/NAME.LIST/NAME entry (first is the primary name)       |
| sku_code                 | sku_code                 | STOCKITEM → PARTNO (Tally's "Part No.")                                                     |
| category                 | category                 | STOCKITEM → CATEGORY (Tally's separate Stock Category dimension; blank if not used)         |
| sub_category             | sub_category             | STOCKITEM → PARENT (immediate Stock Group)                                                  |
| unit                     | unit                     | STOCKITEM → BASEUNITS                                                                       |
| alternate_unit           | alternate_unit           | STOCKITEM → ADDITIONALUNITS (blank if no alt unit configured)                               |
| conversion_factor        | conversion_factor        | Derived from UNIT master where COMPOUNDUNIT.NAME = BASEUNITS (CONVERSION numeric); else ""  |
| opening_qty              | opening_qty              | STOCKITEM → OPENINGBALANCE leading numeric (e.g. "10 PCS" → 10); blank if zero              |
| opening_value            | opening_value            | STOCKITEM → OPENINGVALUE, sign-flipped (positive = asset / Dr, matches Tally UI display)    |
| standard_cost            | standard_cost            | STOCKITEM → STANDARDCOST (latest item-level standard cost; blank if not set)                |
| standard_selling_price   | standard_selling_price   | STOCKITEM → STANDARDPRICE (latest item-level standard selling price; blank if not set)      |
| hsn_code                 | hsn_code                 | STOCKITEM → GSTDETAILS.LIST → HSNCODE (falls back to top-level HSNCODE / HSN)               |
| gst_rate                 | gst_rate                 | STOCKITEM → GSTDETAILS.LIST → STATEWISEDETAILS.LIST → RATEDETAILS.LIST → first IGST rate    |
| reorder_level            | reorder_level            | STOCKITEM → REORDERLEVEL (numeric prefix; blank if not set)                                 |
| reorder_quantity         | reorder_quantity         | STOCKITEM → MINIMUMORDERQTY (numeric prefix; falls back to REORDERQTY)                      |
| is_active                | is_active                | Always TRUE (Tally collection only returns non-deleted items)                               |
| created_at               | created_at               | Set by push tool on first insert (ISO 8601 UTC). Preserved on update.                       |
| updated_at               | updated_at               | Set by push tool whenever a row's data changes (ISO 8601 UTC).                              |

## Dedupe / upsert key

The push tool uses the composite **`(company, location, item_id)`** to upsert
rows. Tally's stock-item GUID is unique within a company; Company / Location
disambiguate the same legal entity loaded as separate Tally companies. If those
three `key` values are renamed in the table above, update `UPSERT_KEYS` in
`tools/push_stock_item_master_to_sheet.py` as well.

## Push-managed columns

`created_at` and `updated_at` are **not** populated by the fetch tool — the
fetch JSON leaves them blank. The push tool fills them at upsert time:

- **Insert** (no existing row with the key): both timestamps = now.
- **Update** (existing row, schema columns differ): preserve `created_at`,
  set `updated_at` = now.
- **Unchanged** (existing row, all other schema columns match): leave both
  timestamps alone.

The "did the row change?" check **excludes** `created_at` and `updated_at`
from the equality comparison — otherwise every push would report every row
as updated.

## Sign convention

Tally exports stock asset values (OPENINGVALUE) as **negative** in XML —
mirroring how it stores ledger debit balances. The fetch tool flips the sign
on `opening_value` so the sheet shows positive numbers for items in stock.

`opening_qty`, `standard_cost`, `standard_selling_price`, `reorder_level`,
and `reorder_quantity` are exported as positive numbers by Tally and are
written as-is (numeric prefix only, unit suffix stripped).

## GST / HSN extraction

GST details in Tally live in a nested subcollection on each stock item:

```
<GSTDETAILS.LIST>
  <APPLICABLEFROM>...</APPLICABLEFROM>
  <HSNCODE>8443</HSNCODE>
  <STATEWISEDETAILS.LIST>
    <RATEDETAILS.LIST>
      <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
      <GSTRATE>18</GSTRATE>
    </RATEDETAILS.LIST>
    ...
  </STATEWISEDETAILS.LIST>
</GSTDETAILS.LIST>
```

The fetch tool returns the **first** HSN code found and the **first IGST
rate** found across all `GSTDETAILS.LIST` entries (most items have one).
If an item uses CGST/SGST instead of IGST, no rate is returned — adjust
the regex in `_extract_gst_rate()` if your chart needs that.
