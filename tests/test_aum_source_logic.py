from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from providers.Waystone.extract_waystone_fields import parse_cision_aum_rows
from providers.jpmorgan.extract_jpmorgan_fields import (
    extract_rows as extract_jpm_rows,
    supplement_missing_green_bond_share_classes,
    transform_row as transform_jpm_row,
)
from run_all_etf_pipeline import collect_rows_with_missing_aum, normalize_output_rows, verify_final_coverage
from scrapers.Janus_Henderson_extractor import parse_cision_nav_rows
from scrapers.Waystone_extractor import extract_waystone_detail_aum, parse_raw_money_to_millions


class JanusCisionParsingTests(unittest.TestCase):
    def test_parse_cision_nav_rows_extracts_exact_isin_and_millions(self) -> None:
        html = """
        <html>
          <head><title>Net Asset Value(s) - Janus Henderson US Short Duration High Yield Active Core UCITS ETF</title></head>
          <body>
            <table>
              <tr>
                <td>Valuation Date</td>
                <td>ISIN Code</td>
                <td>Shares in Issue</td>
                <td>Currency</td>
                <td>NET Asset Value</td>
                <td>NAV per Share</td>
              </tr>
              <tr>
                <td>07.07.26</td>
                <td>IE0007W7MZL0</td>
                <td>973,257.00</td>
                <td>EUR</td>
                <td>9,884,235.34</td>
                <td>10.1558</td>
              </tr>
            </table>
          </body>
        </html>
        """
        rows = parse_cision_nav_rows("https://news.cision.com/example", html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["isin"], "IE0007W7MZL0")
        self.assertEqual(rows[0]["valuation_date"], "07/07/2026")
        self.assertEqual(rows[0]["aum_currency"], "EUR")
        self.assertEqual(rows[0]["aum_mn"], "9.88423534")


class WaystoneParsingTests(unittest.TestCase):
    def test_parse_raw_money_to_millions_respects_units(self) -> None:
        self.assertEqual(parse_raw_money_to_millions("$3.3 Million")[0], "3.30")
        self.assertEqual(parse_raw_money_to_millions("1.2 Billion")[0], "1200.00")
        self.assertEqual(parse_raw_money_to_millions("327,167.03")[0], "0.33")

    def test_extract_waystone_detail_aum_reads_total_net_assets_and_as_of_date(self) -> None:
        html = """
        <html>
          <body>
            <div class='fund-banner-block'>
              <p class='label-banner'>Total Net Assets</p>
              <p class='value-banner'>$3.3 Million</p>
            </div>
            <div>As of 7/7/26</div>
          </body>
        </html>
        """
        detail = extract_waystone_detail_aum(html)
        self.assertEqual(detail["fund_aum_m"], "3.30")
        self.assertEqual(detail["fund_currency"], "USD")
        self.assertEqual(detail["as_of_date"], "7/7/26")

    def test_parse_cision_aum_rows_calculates_total_aum_from_units_and_nav(self) -> None:
        html = """
        <table>
          <tr><th>Valuation Date</th><th>Name</th><th>ISIN</th><th>Currency</th><th>Units</th><th>NAV per Unit</th></tr>
          <tr><td>2026/07/09</td><td>NT LSTD PRV EQ UCITS</td><td>IE0008ZGI5C1</td><td>USD</td><td>11171222.0000</td><td>29.4544</td></tr>
        </table>
        """
        row = parse_cision_aum_rows(html)["IE0008ZGI5C1"]
        self.assertEqual(row["aum_m"], "329.04")
        self.assertEqual(row["aum_currency"], "USD")
        self.assertEqual(row["date"], "09/07/2026")


class PipelinePolicyTests(unittest.TestCase):
    def test_pipeline_does_not_guess_aum_currency_when_aum_is_blank(self) -> None:
        rows = normalize_output_rows(
            [
                {
                    "ETF Name": "Example",
                    "Issuer": "J.P. Morgan Asset Management",
                    "ISIN": "IE000TEST123",
                    "CCY": "USD",
                    "TER(bps)": "32.00",
                    "Partial AUM(M)": "",
                    "Total AUM(M)": "",
                    "AUM CCY": "",
                    "Date": "",
                }
            ]
        )
        self.assertEqual(rows[0]["AUM CCY"], "")

    def test_blank_aum_row_is_reported_as_missing_aum(self) -> None:
        rows = [
            {
                "ETF Name": "ETF Missing AUM",
                "Issuer": "J.P. Morgan Asset Management",
                "ISIN": "IE000TEST123",
                "CCY": "USD",
                "TER(bps)": "32.00",
                "Partial AUM(M)": "",
                "Total AUM(M)": "",
                "AUM CCY": "",
                "Date": "",
            }
        ]
        self.assertEqual(collect_rows_with_missing_aum(rows), rows)

    def test_zero_aum_row_is_reported_as_missing_aum(self) -> None:
        rows = [
            {
                "Issuer": "Example Issuer",
                "ISIN": "IE000ZERO123",
                "Total AUM(M)": "0.00",
            }
        ]
        self.assertEqual(collect_rows_with_missing_aum(rows), rows)

    def test_final_coverage_reports_missing_whitelist_isins(self) -> None:
        rows = [{"ISIN": "IE000PRESENT1", "Issuer": "Example", "Total AUM(M)": "12.34"}]
        summary = verify_final_coverage(
            rows,
            {"IE000PRESENT1", "IE000MISSING1"},
            "ISIN",
        )
        self.assertEqual(summary.missing_expected_isins, ("IE000MISSING1",))
        self.assertEqual(summary.missing_aum_identifiers, ())

    def test_legacy_aum_column_is_promoted_to_total_aum(self) -> None:
        rows = normalize_output_rows(
            [
                {
                    "ETF Name": "Legacy AUM ETF",
                    "Issuer": "Example Issuer",
                    "ISIN": "IE000LEGACY1",
                    "CCY": "EUR",
                    "TER(bps)": "10.00",
                    "AUM(M)": "12.34",
                    "AUM CCY": "EUR",
                    "Date": "07/07/2026",
                }
            ]
        )
        self.assertEqual(rows[0]["Partial AUM(M)"], "")
        self.assertEqual(rows[0]["Total AUM(M)"], "12.34")


class JpmorganExtractionTests(unittest.TestCase):
    def test_transform_row_uses_source_date_and_currency(self) -> None:
        row = transform_jpm_row(
            {
                "shareclassName": "Sample ETF",
                "identifier": "IE000TEST123",
                "shareclassCurrencyCode": "USD",
                "ongoingCharge": 0.32,
                "assetsUnderManagement": 56019037.92,
                "fundValuationCurrency": "USD",
                "fundValuationDate": "2026-07-07",
            }
        )
        self.assertEqual(row["AUM(M)"], "56.02")
        self.assertEqual(row["AUM CCY"], "USD")
        self.assertEqual(row["Date"], "07/07/2026")

    def test_extract_rows_does_not_inject_liquidated_isin(self) -> None:
        with mock.patch(
            "providers.jpmorgan.extract_jpmorgan_fields.parse_snapshot_rows",
            return_value=[
                {
                    "categoryCode": "ETF",
                    "shareclassName": "Sample ETF",
                    "identifier": "IE000TEST123",
                    "shareclassCurrencyCode": "USD",
                    "ongoingCharge": 0.32,
                    "assetsUnderManagement": 56019037.92,
                    "fundValuationCurrency": "USD",
                    "fundValuationDate": "2026-07-07",
                }
            ],
        ):
            rows = extract_jpm_rows(input_path=Path("providers/jpmorgan/dummy.json"))

        self.assertEqual([row["ISIN"] for row in rows], ["IE000TEST123"])

    def test_missing_active_green_bond_share_classes_use_one_fund_total(self) -> None:
        with mock.patch(
            "providers.jpmorgan.extract_jpmorgan_fields.build_justetf_session",
            return_value=object(),
        ), mock.patch(
            "providers.jpmorgan.extract_jpmorgan_fields.fetch_justetf_profile",
            return_value={"fetch_status": "ok", "aum_mn": "27.00", "aum_ccy": "EUR"},
        ):
            rows = supplement_missing_green_bond_share_classes(
                [],
                Path("providers/jpmorgan/2026-07-10/jpmorgan_etf_export.json"),
            )

        self.assertEqual({row["ISIN"] for row in rows}, {"IE0005FKEK99", "IE000HZSZFP6"})
        self.assertEqual({row["AUM(M)"] for row in rows}, {"27.00"})
        self.assertEqual({row["AUM CCY"] for row in rows}, {"EUR"})


if __name__ == "__main__":
    unittest.main()
