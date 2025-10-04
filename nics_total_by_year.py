#!/usr/bin/env python3
import sys
import re
from pathlib import Path

import pdfplumber
import pandas as pd

YEAR_HEADER_RE = re.compile(r"\bYear\s+(\d{4})\b")
# Lines we should ignore entirely
IGNORE_PREFIXES = (
    "NICS Firearm Background Checks", "Month/Year by State", "State / Territory",
    "NOTE", "Page", "January 1", "These statistics", "They do not represent",
)
IGNORE_EXACT = set(["", "Totals"])  # blank or standalone labels to skip


def is_state_row(line: str) -> bool:
    """
    Heuristic: A state/territory total line typically starts with a letter,
    contains numbers, and isn't a header or footnote line.
    """
    s = line.strip()
    if any(s.startswith(p) for p in IGNORE_PREFIXES): return False
    if s in IGNORE_EXACT: return False
    if not re.match(r"^[A-Za-z]", s): return False
    # Require at least one number present (we'll take the last one on the line)
    if not re.search(r"\d", s): return False
    # Exclude obvious section labels
    if "Grand Total" in s and "State / Territory" in s: return False
    return True


def parse_last_int(line: str) -> int | None:
    """
    Grab the last integer-looking token on the line (e.g., 1,234,567 -> 1234567).
    """
    nums = re.findall(r"\d[\d,]*", line)
    if not nums: return None
    last = nums[-1].replace(",", "")
    try:
        return int(last)
    except ValueError:
        return None


def extract_totals_by_year(pdf_path: Path) -> pd.DataFrame:
    totals = {}  # year -> running sum of state grand totals
    current_year = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            # Detect year header (there can be multiple per file)
            # If multiple year mentions appear, take the last one on the page (most specific section header)
            page_years = YEAR_HEADER_RE.findall(text)
            if page_years:
                current_year = int(page_years[-1])

            # Walk lines and accumulate totals
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if YEAR_HEADER_RE.search(line):
                    # Reset to this year if a header is inline mid-page
                    current_year = int(YEAR_HEADER_RE.search(line).group(1))
                    continue

                if current_year is None:
                    # Haven't found a year yet on earlier pages (unlikely), skip
                    continue

                if not is_state_row(line):
                    continue

                # Skip lines that are clearly not state rows (common culprits)
                # e.g., subtotal blurbs or section captions that look like sentences.
                # Heuristic: if it has too many words with lowercase/periods and few numbers, skip.
                words = re.findall(r"[A-Za-z]+", line)
                if len(words) > 8 and len(re.findall(r"\d", line)) < 3:
                    continue

                val = parse_last_int(line)
                if val is None:
                    continue

                # Defensive: exclude rows that look like month rows (they end with 12 numbers, not a single total)
                # Our approach grabs the last number, which for state rows is the "Grand Total".
                # If the line contains an unusual pattern like many numbers and no trailing total,
                # it's safer to still take the last number (empirically correct for these PDFs).
                print(f"{current_year}, {val}")
                totals[current_year] = totals.get(current_year, 0) + val

    # Build DataFrame
    rows = [{"year": y, "total_background_checks": v} for y, v in sorted(totals.items())]
    return pd.DataFrame(rows)


def main():
    if len(sys.argv) < 2:
        print("Usage: python nics_totals_by_year.py /path/to/NICS_Firearm_Checks_-_Month_Year_by_State.pdf")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    df = extract_totals_by_year(pdf_path)
    if df.empty:
        print("No year totals were found. Double-check the PDF format or adjust the heuristics.")
        sys.exit(2)

    # Pretty print
    print(df.sort_values("year").to_string(index=False))

    # Save CSV
    out_csv = pdf_path.with_name("nics_totals_by_year.csv")
    df.to_csv(out_csv, index=False)
    print(f"\nWrote {out_csv}")

if __name__ == "__main__":
    main()
