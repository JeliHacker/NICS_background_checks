#!/usr/bin/env python3
import sys
import re
from pathlib import Path
from typing import List, Optional

import pdfplumber
import pandas as pd

# ---------------------------
# Regex & constants
# ---------------------------
YEAR_HEADER_RE = re.compile(r"\bYear\s+(\d{4})\b", re.IGNORECASE)
# Capture the full 4-digit year anywhere on the page as a fallback
ANY_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# Match header date ranges like: "January 1, 2025 - September 30, 2025"
HEADER_RANGE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}\s*-\s*"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(?:19|20)\d{2}",
    re.IGNORECASE,
)

MONTHS_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
MONTHS_FULL = ["January","February","March","April","May","June","July","August","September","October","November","December"]
MONTH_TO_IDX = {name: i + 1 for i, name in enumerate(MONTHS_FULL)}

# Lines to ignore
IGNORE_PREFIXES = (
    "NICS Firearm Background Checks", "Month/Year by State", "State / Territory",
    "NOTE", "Page", "January 1", "These statistics", "They do not represent",
)
IGNORE_CONTAINS = ("State / Territory", "Totals by State", "Totals by Year")
IGNORE_EXACT = {"", "Totals", "U.S. Total", "U. S. Total", "US Total", "U. S. Totals", "U.S. Totals"}


# ---------------------------
# Helpers
# ---------------------------
def _numbers_in_line(line: str) -> List[int]:
    """Return all integer tokens in a line, stripping commas."""
    return [int(s.replace(",", "")) for s in re.findall(r"\d[\d,]*", line)]


def is_state_row(line: str) -> bool:
    """
    Heuristic for a state/territory summary line:
      - Starts with a letter (state/territory name)
      - Isn’t a header/footer/US total
      - Contains numbers (months and totals)
    """
    s = line.strip()
    if s in IGNORE_EXACT:
        return False
    if any(s.startswith(p) for p in IGNORE_PREFIXES):
        return False
    if any(c in s for c in IGNORE_CONTAINS):
        return False
    if not re.match(r"^[A-Za-z]", s):
        return False
    if "U.S." in s or "U. S." in s or s.lower().startswith("us "):
        return False
    if not re.search(r"\d", s):
        return False
    return True


def _infer_year_for_page(text: str, current_year: Optional[int]) -> Optional[int]:
    """
    Prefer explicit 'Year ####'. If missing, fall back to the most recent 4-digit year
    present anywhere on the page (useful for partial/current-year pages).
    """
    page_years = YEAR_HEADER_RE.findall(text)
    if page_years:
        return int(page_years[-1])

    any_years = [int(y) for y in ANY_YEAR_RE.findall(text) if int(y) >= 1998]
    if any_years:
        return max(any_years)

    return current_year


def _infer_end_month_for_page(text: str, default: int = 12) -> int:
    """
    For partial years (e.g., 'January 1, 2025 - September 30, 2025'),
    return the month index of the page's end month (Sep -> 9).
    Falls back to the latest month name seen on the page, then to `default`.
    """
    m = HEADER_RANGE_RE.search(text)
    if m:
        end_month_name = m.group(2).capitalize()
        return MONTH_TO_IDX.get(end_month_name, default)

    # Fallback heuristic: pick the latest month name that appears
    last_idx = default
    for i, name in enumerate(MONTHS_FULL, start=1):
        if name in text:
            last_idx = i
    return last_idx


# ---------------------------
# Core extractors
# ---------------------------
def extract_monthlies_by_year(pdf_path: Path) -> pd.DataFrame:
    """
    Build (year, month) -> national total. Handles partial years by reading the header
    to learn the last month shown on the page (e.g., Jan–Sep).
    """
    monthly = {}  # year -> [12 monthly sums]
    current_year: Optional[int] = None
    current_end_month = 12

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            current_year = _infer_year_for_page(text, current_year)
            current_end_month = _infer_end_month_for_page(text, default=current_end_month)

            for raw_line in text.splitlines():
                line = raw_line.strip()

                # Inline year reset if header appears mid-page
                m = YEAR_HEADER_RE.search(line)
                if m:
                    current_year = int(m.group(1))
                    continue

                if current_year is None or not is_state_row(line):
                    continue

                nums = _numbers_in_line(line)
                # Expect the last value is the state Grand Total,
                # and the preceding N values are months Jan..end_month.
                need = current_end_month + 1  # months + grand total
                if len(nums) < need:
                    # Likely a wrapped/broken line; skip defensively
                    continue

                months_vals = nums[-need:-1]  # exactly the visible months on this page
                if len(months_vals) != current_end_month:
                    continue

                if current_year not in monthly:
                    monthly[current_year] = [0] * 12

                for i in range(current_end_month):
                    monthly[current_year][i] += months_vals[i]

    # Flatten to dataframe
    rows = []
    for y in sorted(monthly.keys()):
        for i, val in enumerate(monthly[y], start=1):
            rows.append({
                "year": y,
                "month": i,
                "month_name": MONTHS_ABBR[i - 1],
                "total_background_checks": val
            })
    return pd.DataFrame(rows).sort_values(["year", "month"]).reset_index(drop=True)


def extract_totals_by_year(pdf_path: Path) -> pd.DataFrame:
    """
    Yearly totals are the sum of monthly totals (so partial years appear).
    """
    monthly_df = extract_monthlies_by_year(pdf_path)
    if monthly_df.empty:
        return pd.DataFrame(columns=["year", "total_background_checks", "basis"])

    out = (
        monthly_df.groupby("year", as_index=False)["total_background_checks"]
        .sum()
        .assign(basis="sum_of_months")
        .sort_values("year")
        .reset_index(drop=True)
    )
    return out


# ---------------------------
# CLI
# ---------------------------
def main():
    if len(sys.argv) < 2:
        print("Usage: python nics_totals_by_year.py /path/to/NICS_Firearm_Checks_-_Month_Year_by_State.pdf")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    # Extract
    monthly_df = extract_monthlies_by_year(pdf_path)
    yearly_df = extract_totals_by_year(pdf_path)

    # Print previews
    if not yearly_df.empty:
        print("=== NATIONAL TOTALS BY YEAR ===")
        print(yearly_df.to_string(index=False))
    else:
        print("No year totals found.")

    if not monthly_df.empty:
        print("\n=== NATIONAL TOTALS BY YEAR-MONTH (first 12 rows) ===")
        print(monthly_df.head(12).to_string(index=False))
        print("...")
        print(monthly_df.tail(12).to_string(index=False))
    else:
        print("\nNo monthly totals found.")

    # Write CSVs
    out_year = pdf_path.with_name("nics_totals_by_year.csv")
    out_month = pdf_path.with_name("nics_totals_by_month.csv")
    yearly_df.to_csv(out_year, index=False)
    monthly_df.to_csv(out_month, index=False)
    print(f"\nWrote {out_year}")
    print(f"Wrote {out_month}")


if __name__ == "__main__":
    main()
