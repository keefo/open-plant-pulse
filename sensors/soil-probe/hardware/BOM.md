# Bill of materials

The canonical project BOM is [`bom.csv`](bom.csv). CSV is used because it is
diffable, works with spreadsheets and procurement tools, and can later be
generated from KiCad. Keep it UTF-8 and RFC 4180 compatible, with one physical
line item per row.

The BOM describes one complete Open Plant Pulse assembly. It is currently a
planning BOM, not an approved purchasing list. Rows marked `selection_required`
or containing `TBD` must be resolved before hardware is ordered for a release.
All current numeric `unit_cost` values are estimates in Canadian dollars (CAD),
as recorded by each row's `currency` field. Costs exclude tax and shipping.

Validate it locally after any change:

```sh
python3 scripts/validate-bom.py
```

## Columns

| Column | Meaning |
| --- | --- |
| `item_id` | Stable project identifier; never reuse an ID for a different part |
| `category` | Broad grouping such as electronics, sensor, power, or mechanical |
| `description` | Vendor-neutral functional description |
| `quantity` / `unit` | Amount required for one complete assembly |
| `manufacturer` | Original part manufacturer, not the marketplace seller |
| `manufacturer_part_number` | Exact orderable MPN; use `TBD` until confirmed |
| `form_factor` | Physical package, module footprint, connector pitch, or assembly format |
| `supplier` / `supplier_sku` | Preferred purchasing source and its stock code |
| `unit_cost` / `currency` | Reference price per unit and ISO 4217 currency code; current estimates use CAD |
| `procurement_url` | Stable manufacturer or distributor page when possible |
| `revision` | Hardware or drawing revision known to have been validated |
| `status` | Selection and validation state defined below |
| `notes` | Constraints that do not fit another field |

## Status values

- `selection_required`: alternatives exist but no exact part has been chosen.
- `candidate`: an intended part class is recorded but not fully validated.
- `validated`: the exact MPN/revision passed the bring-up checklist.
- `obsolete`: retained for history but must not be used for new builds.
- `design_required`: a project-authored part does not yet have released source.

## Maintenance rules

1. Use an exact manufacturer part number for every purchased item. Product names
   such as "RS485 board" or "18650" are not sufficient for a release.
2. Describe the physical implementation precisely. Castellated edge pads and
   through-hole pin headers are different termination styles even when both fit
   a standard 2.54 mm (0.1 in) breakout footprint.
3. Record alternates as separate rows or in an approved-alternates file after
   proving form, fit, function, electrical, and firmware compatibility.
4. Treat prices as dated estimates. Update them for a release and state the
   pricing date, currency, tax, and shipping assumptions in the release notes.
5. Do not silently replace a validated revision. Change the row and repeat the
   affected bring-up tests.
6. For a future custom PCB, generate its component BOM from KiCad and list the
   populated PCB as a subassembly in this top-level assembly BOM.

## Release process

For each hardware release:

1. Resolve all required `TBD` fields and freeze exact revisions.
2. Copy the canonical BOM into the release artifacts with the release version in
   its filename, for example `open-plant-pulse-bom-v1.0.csv`.
3. Export a human-readable PDF or spreadsheet only as a convenience artifact;
   the tagged CSV remains the source of truth.
4. Include total reference cost and any minimum-order quantities in release notes.