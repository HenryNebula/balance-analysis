"""The layout tool catalog — render/grep/regions (views) and
table/render_table/anchor_value (grid): the model's spatial assistance."""

import json
import re
from pathlib import Path

import pytest

import statementledger as sl

FIXTURES = Path(__file__).parent / "fixtures"


def line(page, y, *items):
    """items: (text, x1, x2) triples."""
    return {"page": page, "y": y,
            "items": [[text, x1, x2] for text, x1, x2 in items]}


def statement_lines():
    """A two-page synthetic statement: form block, ledger table with a
    leading wrapped row, page furniture, stop line, prose footer."""
    return [
        line(0, 40, ("Demo Bank", 72, 140)),                      # 0 furniture
        line(0, 60, ("Account No:", 72, 130), ("1234 5678", 300, 360)),
        line(0, 70, ("Opening balance:", 72, 150), ("$1,000.00", 300, 360)),
        line(0, 190, ("Date", 72, 100), ("Description", 150, 220),
             ("Amount", 380, 420), ("Balance", 500, 545)),         # 3 header
        line(0, 210, ("TRANSFER", 150, 200)),                      # 4 wrap
        line(0, 220, ("01 Feb", 72, 100), ("in from work", 150, 220),
             ("500.00", 380, 420), ("1,500.00", 500, 545)),        # 5
        line(0, 230, ("02 Feb", 72, 100), ("coffee run", 150, 220),
             ("12.34", 380, 420), ("1,487.66", 500, 545)),         # 6
        line(0, 770, ("Demo Bank", 72, 140)),                      # 7 furniture
        line(1, 40, ("Demo Bank", 72, 140)),                      # 8 furniture
        line(1, 60, ("Account No:", 72, 130), ("1234 5678", 300, 360)),
        line(1, 70, ("Opening balance:", 72, 150), ("$1,000.00", 300, 360)),
        line(1, 190, ("Date", 72, 100), ("Description", 150, 220),
             ("Amount", 380, 420), ("Balance", 500, 545)),         # 11 furniture
        line(1, 210, ("03 Feb", 72, 100), ("market vegetables", 150, 240),
             ("60.00", 380, 420), ("1,427.66", 500, 545)),         # 12
        line(1, 230, ("End of activity.", 72, 160)),               # 13 stop
        line(1, 260, ("This statement is provided electronically and "
                      "covers the period shown above", 72, 500)),  # 14 prose
        line(1, 770, ("Demo Bank", 72, 140)),                     # 15 footer
    ]


# --- render ------------------------------------------------------------------


def test_render_gutter_and_page_markers(capsys):
    sl.render(statement_lines(), 12)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[Page 0]"
    assert re.match(r"^000 \| ", out[1])
    assert "[Page 1]" in out


def test_render_positions_preserve_x_order(capsys):
    lines = [line(0, 50, ("01 Feb", 72, 100), ("coffee run", 150, 220),
                  ("12.34", 380, 420), ("1,487.66", 500, 545))]
    sl.render(lines, 1)
    wall = capsys.readouterr().out.splitlines()[1].split("| ", 1)[1]
    columns = [wall.index(token) for token in
               ("01 Feb", "coffee run", "12.34", "1,487.66")]
    assert columns == sorted(columns)
    assert columns[1] - columns[0] > 5  # visibly separated, not mashed


def test_render_clips_long_description_not_the_money(capsys):
    lines = [line(0, 50, ("STORE 48200 PURCHASE AUTHORIZED " + "X" * 30,
                          200, 490), ("112.33", 500, 545))]
    sl.render(lines, 1, width=90)
    wall = capsys.readouterr().out.splitlines()[1].split("| ", 1)[1]
    assert "112.33" in wall
    assert "…" in wall
    assert wall.index("112.33") > wall.index("…")


def test_render_empty_line_is_bare_gutter(capsys):
    sl.render([line(0, 10)], 1)
    assert capsys.readouterr().out.splitlines() == ["[Page 0]", "000 | "]


# --- grep --------------------------------------------------------------------


def test_grep_hits_with_indices_and_count(capsys):
    sl.grep(statement_lines(), "balance")
    out = capsys.readouterr().out
    assert "001: Account No: 1234 5678" not in out
    assert "002: Opening balance: $1,000.00" in out
    assert out.rstrip().endswith("match(es)")


def test_grep_is_case_insensitive_by_default(capsys):
    sl.grep(statement_lines(), "OPENING BALANCE")
    assert "002: Opening balance: $1,000.00" in capsys.readouterr().out


def test_grep_regex_mode(capsys):
    sl.grep(statement_lines(), r"\d{2} Feb", regex=True)
    out = capsys.readouterr().out
    assert "005:" in out and "006:" in out and "012:" in out
    assert "000:" not in out


def test_grep_context_groups(capsys):
    sl.grep(statement_lines(), "Feb", context=1)
    out = capsys.readouterr().out
    assert "--" in out          # the 005/006 group and the 012 group are disjoint
    assert "004: TRANSFER" in out
    assert out.rstrip().endswith("3 match(es)")


def test_grep_whiff(capsys):
    sl.grep(statement_lines(), "nonexistent-term")
    out = capsys.readouterr().out
    assert "0 match(es)" in out


# --- table -------------------------------------------------------------------


def test_table_columns_and_wrap_merge():
    cols, rows = sl.table(statement_lines(),
                          stop_terms=("End of activity.",))
    assert cols == ["Date", "Description", "Amount", "Balance"]
    assert [r["Description"] for r in rows] == [
        "TRANSFER in from work", "coffee run", "market vegetables"]
    assert rows[0]["Amount"] == "500.00"
    assert rows[0]["_line"] == 5  # the wrap (line 4) merged forward


def test_table_no_merge_keeps_wrap_rows():
    _cols, rows = sl.table(statement_lines(), stop_terms=("End of activity.",),
                           merge_continuations=False)
    assert rows[0]["Description"] == "TRANSFER"
    assert rows[1]["Description"] == "in from work"


def test_table_drops_page_furniture():
    _cols, rows = sl.table(statement_lines(),
                           stop_terms=("End of activity.",))
    assert all(r["_line"] not in (0, 3, 7, 8, 11) for r in rows)
    assert not any(r["Date"] == "Date" for r in rows)  # header copies gone


def test_table_sections_tag_rows():
    lines = statement_lines()
    lines.insert(6, line(0, 225, ("WITHDRAWALS", 72, 150)))
    _cols, rows = sl.table(lines, stop_terms=("End of activity.",),
                           sections={"withdrawals": "WITHDRAWALS"})
    assert rows[-1]["_section"] == "withdrawals"
    assert rows[0]["_section"] is None


def test_table_start_stop_bounds():
    _cols, rows = sl.table(statement_lines(), start=6, stop=7)
    assert [r["Description"] for r in rows] == ["coffee run"]


def test_table_explicit_header():
    lines = statement_lines()
    header = lines[3]
    _cols, rows = sl.table(lines, header=header, start=4, stop=6)
    assert rows[0]["Description"] == "TRANSFER in from work"


def test_table_no_header_raises_with_hint():
    with pytest.raises(ValueError, match="grep"):
        sl.table([line(0, 50, ("no columns here", 72, 200))])


# --- render_table ------------------------------------------------------------


def test_render_table_aligned_and_capped(capsys):
    cols = ["Date", "Description"]
    rows = [{"Date": "01 Feb",
             "Description": "A VERY LONG DESCRIPTION THAT MUST BE CAPPED",
             "_line": 5}]
    sl.render_table(cols, rows, width=20)
    out = capsys.readouterr().out.splitlines()
    assert " | " in out[0]
    assert "-+-" in out[1]
    assert "A VERY LONG DESCRIP…" in out[2]


def test_render_table_hides_empty_columns(capsys):
    cols = ["Date", "Description", "Amount"]
    rows = [{"Date": "01 Feb", "Description": "coffee", "Amount": "",
             "_line": 5}]
    sl.render_table(cols, rows)
    out = capsys.readouterr().out
    assert "Amount" not in out


# --- anchor_value ------------------------------------------------------------


def test_anchor_value_beside_the_label():
    assert sl.anchor_value(statement_lines(), "Account No:") == "1234 5678"


def test_anchor_value_index_picks_nth_match():
    assert sl.anchor_value(statement_lines(), "Account No:", index=1) == \
        "1234 5678"  # the page-1 copy


def test_anchor_value_label_without_value_names_the_line():
    lines = [line(0, 50, ("Statement", 72, 130))]
    with pytest.raises(ValueError, match="Statement"):
        sl.anchor_value(lines, "Statement")


def test_anchor_value_absent_label():
    with pytest.raises(ValueError, match="label not printed anywhere"):
        sl.anchor_value(statement_lines(), "IBAN")


# --- regions -----------------------------------------------------------------


def regions_lines():
    """Bigger synthetic: 6-line form, header + 8 data rows, stop, prose."""
    lines = [line(0, 40, ("Demo Bank", 72, 140))]                   # 0 furniture
    labels = ("Account No:", "Opening balance:", "Closing balance:",
              "Statement Period:", "Currency:", "Branch:")
    for number, label in enumerate(labels):
        lines.append(line(0, 55 + number * 10, (label, 72, 170),
                          ("value " + str(number), 300, 360)))      # 1-6 form
    lines.append(line(0, 190, ("Date", 72, 100), ("Description", 150, 220),
                      ("Amount", 380, 420), ("Balance", 500, 545)))  # 7 header
    for number in range(8):                                         # 8-15 rows
        lines.append(line(0, 205 + number * 10,
                          (f"{number + 1:02d} Feb", 72, 100),
                          (f"payee number {number}", 150, 230),
                          (f"{number * 10}.50", 380, 420),
                          (f"1,{number:03d}.00", 500, 545)))
    lines.append(line(0, 300, ("End of activity", 72, 160)))         # 16 stop
    for number in range(3):                                         # 17-19 prose
        lines.append(line(0, 330 + number * 10,
                          ("This statement is provided electronically and "
                           "covers the period shown above, copy "
                           + chr(ord("a") + number), 72, 500)))
    lines.append(line(1, 40, ("Demo Bank", 72, 140)))               # 20 furniture
    return lines


def test_regions_classifies_form_table_prose(capsys):
    found = sl.regions(regions_lines())
    kinds = [r["kind"] for r in found]
    assert "form" in kinds and "table" in kinds and "prose" in kinds
    table_region = found[kinds.index("table")]
    assert table_region["start"] <= 8 and table_region["stop"] >= 15
    out = capsys.readouterr().out
    assert "TABLE" in out and "furniture:" in out


# --- fixture smoke: the grid reproduces ground truth -------------------------


@pytest.mark.parametrize("name,stop", [
    ("copperkettle_checking", "Transactions stop"),
    ("bluelantern_savings", "End of activity"),
])
def test_table_reproduces_fixture_truth(name, stop):
    frames = json.loads((FIXTURES / f"{name}.frames.json").read_text())
    truth = json.loads((FIXTURES / f"{name}.truth.json").read_text())
    cols, rows = sl.table(frames["statements"][0]["lines"],
                          stop_terms=(stop,))
    assert cols == ["Date", "Description", "Debit", "Credit", "Balance"] \
        or cols == ["Date", "Description", "Amount", "Balance"]
    assert [r["Description"] for r in rows] == \
        [t["description"] for t in truth["transactions"]]
