"""Selection-card regression test: options must never render as [object Object].

The userInput card in web/app.js once derived chip text straight from
`o.label || o`, so any option without a `label` key (a bare number, or an
object keyed as value/text/name/...) stringified to "[object Object]" in
the chip, the input placeholder, and the `selectedLabel` wire value — and a
question under an alternate key left the `.q` line blank.

This repo is stdlib-only with no JS harness, so this test guards the
contract at the source level: the normalizers exist, cover the synonym
keys, coerce primitives to strings, and every display/wire use goes
through them. Behavior against the payload shapes below was verified with
a throwaway probe mirroring the functions.

Run: python3 -m unittest tests.test_userinput_card -v   (from repo root)
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP_JS = Path(__file__).resolve().parent.parent / "web" / "app.js"


def load():
    return APP_JS.read_text()


class TestSelectionCardNormalization(unittest.TestCase):
    def test_old_passthrough_is_gone(self):
        src = load()
        self.assertNotIn("function chipLabel", src)
        self.assertNotRegex(src, r"return\s+\(o\s*&&\s*o\.label\)\s*\|\|\s*o\s*;")

    def test_option_label_covers_synonyms_and_coerces(self):
        src = load()
        m = re.search(r"function optionLabel\(o\) \{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m, "optionLabel missing")
        body = m.group(1)
        for key in ("label", "name", "text", "value", "title"):
            self.assertIn(f'"{key}"', body, f"synonym {key!r} missing")
        # Primitives become strings; unlabelable objects become "", never
        # the raw object (which the DOM would stringify to [object Object]).
        self.assertIn('return "";', body)
        self.assertIn("function asText(v)", src)
        as_text = re.search(r"function asText\(v\) \{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(as_text)
        self.assertIn("String(v)", as_text.group(1))

    def test_question_text_and_id_have_fallbacks(self):
        src = load()
        qt = re.search(r"function questionText\(q\) \{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(qt, "questionText missing")
        for key in ("question", "prompt", "title", "text"):
            self.assertIn(f'"{key}"', qt.group(1), f"key {key!r} missing")
        qi = re.search(r"function questionId\(q, i\) \{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(qi, "questionId missing")
        self.assertIn("questionId", qi.group(1))  # honors the wire key
        self.assertRegex(qi.group(1), r"q\$\{")  # stable fallback, never undefined

    def test_display_and_wire_go_through_normalizers(self):
        src = load()
        # Chips render normalized text...
        self.assertIn("const label = optionLabel(o);", src)
        self.assertIn("const desc = optionDesc(o);", src)
        self.assertNotIn("o.description", src.replace("optionDesc", ""))
        # ...unlabeled options never become chips...
        self.assertRegex(
            src, r"\(q\.options \|\| \[\]\)\.filter\(\(o\) => optionLabel\(o\)\)")
        # ...the question line never renders blank when an id exists...
        self.assertIn("questionText(q) || q.header || qid", src)
        # ...and the wire only ever carries normalized string labels.
        self.assertIn("selectedLabel: optionLabel(o)", src)
        self.assertNotIn("selectedLabel: chipLabel(o)", src)


class TestDefaultSelection(unittest.TestCase):
    """Option 1 is the default selection: no card can submit empty."""

    def test_empty_answer_guard_is_gone(self):
        self.assertNotIn("type an answer first", load())

    def test_immediate_send_falls_back_to_first_option(self):
        src = load()
        self.assertIn("selectedLabel: optionLabel(opts[0])", src)

    def test_first_chip_marked_picked_by_default(self):
        src = load()
        # Both immediate and form paths visibly mark option 1 picked.
        self.assertGreaterEqual(
            src.count('classList.add("picked")'), 2)
        self.assertIn('picked.add(optionLabel(opts[0]))', src)


if __name__ == "__main__":
    unittest.main()
