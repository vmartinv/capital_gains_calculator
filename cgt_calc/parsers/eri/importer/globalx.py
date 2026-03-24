"""GlobalX ERI transaction parser."""

from __future__ import annotations

from decimal import Decimal
import logging
import re
from typing import TYPE_CHECKING

import dateutil.parser as date_parser
import pdfplumber

from cgt_calc.util import is_isin, round_decimal

if TYPE_CHECKING:
    import datetime
    from pathlib import Path

from cgt_calc.parsers.eri.model import ERITransaction

from .model import ERIImporter, ERIImporterOutput

LOGGER = logging.getLogger(__name__)

REPORT_FILE_REGEX = re.compile(r"^UK-Reportable_Income\.pdf$")
CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")
AMOUNT_REGEX = re.compile(r"^\d+\.\d+$")

ISIN_COLUMN = 2
REPORTING_PERIOD_END_COLUMN = 5
CURRENCY_COLUMN = 6
ERI_COLUMN = 7
COLUMNS = [ISIN_COLUMN, REPORTING_PERIOD_END_COLUMN, CURRENCY_COLUMN, ERI_COLUMN]

GLOBALX_ERI_FILENAME = "globalx_eri.csv"


def _extract_date(cell: str | None) -> datetime.date | None:
    if not cell:
        return None
    if not re.match(r"^\d{1,2}\s[A-Z][a-z]+\s\d{4}$", cell.strip()) and not re.match(
        r"^\w+,\s+\w+\s+\d{1,2},\s+\d{4}$", cell.strip()
    ):
        return None
    try:
        date = date_parser.parse(cell.strip(), fuzzy=True, dayfirst=True)
        min_valid_year = 1990
        max_valid_day = 31
        max_valid_month = 12
        if not (
            date.year >= min_valid_year
            and 1 <= date.day <= max_valid_day
            and 1 <= date.month <= max_valid_month
        ):
            return None
        return date.date()
    except date_parser.ParserError:
        return None


def _extract_report_year(page: pdfplumber.page.Page) -> int | None:
    prefix = "Period of account ended"
    matches = [
        line["text"] for line in page.extract_text_lines() if prefix in line["text"]
    ]
    if not matches:
        return None
    year_str = matches[0][len(prefix) :].strip()
    try:
        date = date_parser.parse(year_str, fuzzy=True, dayfirst=True)
    except date_parser.ParserError:
        return None
    return date.year


class GlobalXImporter(ERIImporter):
    """Parser for GlobalX ERI spreadsheets."""

    def __init__(self) -> None:
        """Create a new GlobalX Parser instance."""
        super().__init__(name="GlobalX")

    def parse(self, file: Path) -> ERIImporterOutput | None:
        """Parse a GlobalX ERI file."""
        if not REPORT_FILE_REGEX.match(file.name):
            return None

        result = ERIImporterOutput(
            transactions=[], output_file_name=GLOBALX_ERI_FILENAME
        )

        with pdfplumber.open(file) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                if not page.search("Global X Funds") or not page.search(
                    "UK reporting fund status report to investors"
                ):
                    if page_num == 1:
                        return None
                    LOGGER.warning("Page %d skipped, missing header", page_num)
                    continue
                year = _extract_report_year(page)
                min_valid_year = 2021
                if year is None or year < min_valid_year:
                    if page_num == 1:
                        return None
                    LOGGER.warning("Page %d skipped, old or missing year", page_num)
                    continue
                # Useful for debugging:
                # page.to_image(resolution=100).debug_tablefinder().save(f"cropped_p{page_num}.png")
                table = page.extract_table()
                min_rows = 2
                assert table
                assert len(table) >= min_rows
                cur_header = table[0]
                assert cur_header[ISIN_COLUMN] == "ISIN"
                assert len(cur_header) > max(COLUMNS)
                data_rows = table[1:]
                for row_num, row in enumerate(data_rows, 1):
                    reporting_period_end = _extract_date(
                        row[REPORTING_PERIOD_END_COLUMN]
                    )
                    assert reporting_period_end is not None, (
                        f"Bad date format in page {page_num}, row {row_num}: {row[REPORTING_PERIOD_END_COLUMN]}"
                    )
                    currency = (
                        (row[CURRENCY_COLUMN] or "")
                        .strip()
                        .replace("JPN", "JPY")
                        .upper()
                    )
                    assert re.match(CURRENCY_REGEX, row[CURRENCY_COLUMN] or ""), (
                        f"Bad currency in page {page_num}, row {row_num}: {row[CURRENCY_COLUMN]}"
                    )
                    isin = (row[ISIN_COLUMN] or "").strip().upper()
                    assert is_isin(isin), (
                        f"Bad ISIN in page {page_num}, row {row_num}: {row[ISIN_COLUMN]}"
                    )
                    amount_raw = (row[ERI_COLUMN] or "").strip()
                    assert re.match(AMOUNT_REGEX, amount_raw), (
                        f"Bad amount in page {page_num}, row {row_num}: {row[ERI_COLUMN]}"
                    )
                    amount = round_decimal(Decimal(amount_raw), 5)
                    result.transactions.append(
                        ERITransaction(
                            isin=isin,
                            date=reporting_period_end,
                            price=amount,
                            currency=currency,
                        )
                    )
                LOGGER.info("Read %d rows from page %d", len(data_rows), page_num)
        LOGGER.info("Done parsing, %d transaction extracted.", len(result.transactions))
        return result
