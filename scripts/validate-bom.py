import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path


EXPECTED_COLUMNS = (
    "item_id",
    "category",
    "description",
    "quantity",
    "unit",
    "manufacturer",
    "manufacturer_part_number",
    "form_factor",
    "supplier",
    "supplier_sku",
    "unit_cost",
    "currency",
    "procurement_url",
    "revision",
    "status",
    "notes",
)
ALLOWED_STATUSES = {
    "selection_required",
    "candidate",
    "validated",
    "obsolete",
    "design_required",
}


def main() -> None:
    root_path = Path(__file__).resolve().parent.parent
    bom_path = root_path / "sensor" / "hardware" / "bom.csv"
    with bom_path.open(newline="", encoding="utf-8") as bom_file:
        reader = csv.DictReader(bom_file)
        if tuple(reader.fieldnames or ()) != EXPECTED_COLUMNS:
            raise ValueError("BOM columns do not match the documented schema")
        rows = list(reader)

    if not rows:
        raise ValueError("BOM must contain at least one line item")

    item_ids: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        if None in row or any(not value.strip() for value in row.values()):
            raise ValueError(f"line {line_number}: every BOM field is required")
        if row["item_id"] in item_ids:
            raise ValueError(f"line {line_number}: duplicate item_id {row['item_id']}")
        item_ids.add(row["item_id"])
        if row["status"] not in ALLOWED_STATUSES:
            raise ValueError(f"line {line_number}: invalid status {row['status']}")
        if len(row["currency"]) != 3 or not row["currency"].isupper():
            raise ValueError(f"line {line_number}: currency must be an ISO 4217 code")

        try:
            if Decimal(row["quantity"]) <= 0:
                raise ValueError(f"line {line_number}: quantity must be positive")
            if row["unit_cost"] != "TBD" and Decimal(row["unit_cost"]) < 0:
                raise ValueError(f"line {line_number}: unit_cost cannot be negative")
        except InvalidOperation as error:
            raise ValueError(f"line {line_number}: invalid numeric value") from error

    print(f"BOM valid: {len(rows)} unique line items")


if __name__ == "__main__":
    main()