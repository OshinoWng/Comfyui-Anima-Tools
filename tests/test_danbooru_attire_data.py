import importlib.util
import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

BODY = """[expand=Table of Contents]
* 1. "About":#dtext-about
* 2. "Headwear and Headgear":#dtext-headwear

h4#about. About

Tags describing publicly-worn clothing.

h4#headwear. Headwear and Headgear

See also [[tag group:hair]], [[tag group:hair ornaments]].

* [[balaclava]]
* [[hat]]
** [[beret|Beret]]
* plain text tag
* prose entry: describes something

h4#tops. Shirts and Topwear

* [[long_hair]]
* [[shirt]]
* [[trench coat]]
** [[raincoat]]

h4#jewelry. Jewelry and Accessories

h6#jhead. Head and Face

* [[earrings]]
* [[hair ornament]]

h6#jtorso. Torso and Misc

* [[necktie]]
"""


def load_tool_module():
    path = REPO_ROOT / "tools" / "fetch_danbooru_attire.py"
    spec = importlib.util.spec_from_file_location("fetch_danbooru_attire", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DanbooruAttireToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool_module()

    def test_normalize_tag_converts_underscores_to_spaces(self):
        self.assertEqual(self.tool.normalize_tag("long_hair"), "long hair")
        self.assertEqual(self.tool.normalize_tag("  Very  Long_Hair "), "very long hair")

    def test_parse_sections_keeps_wiki_labels_and_plain_text_entries(self):
        sections = self.tool.parse_sections(BODY)
        self.assertIn("hat", sections["Headwear and Headgear"])
        self.assertIn("beret", sections["Headwear and Headgear"])
        self.assertIn("plain text tag", sections["Headwear and Headgear"])
        self.assertNotIn("prose entry: describes something", sections["Headwear and Headgear"])

    def test_child_section_bullets_are_attributed_to_the_parent_section(self):
        sections = self.tool.parse_sections(BODY)
        self.assertIn("earrings", sections["Jewelry and Accessories"])
        self.assertIn("necktie", sections["Jewelry and Accessories"])
        self.assertIn("earrings", sections["Head and Face"])
        self.assertIn("necktie", sections["Torso and Misc"])

    def test_sections_stop_at_the_next_heading(self):
        sections = self.tool.parse_sections(BODY)
        self.assertNotIn("necktie", sections["Shirts and Topwear"])
        self.assertNotIn("balaclava", sections["About"])

    def test_bundled_slot_mapping_is_consistent(self):
        for slot, section_titles in self.tool.SLOT_SECTIONS.items():
            self.assertIn(slot, self.tool.SLOT_ORDER)
            for title in section_titles:
                self.assertIsInstance(title, str)
        self.assertNotIn(self.tool.VOCABULARY_KEY, self.tool.SLOT_ORDER)

    def test_bundled_data_file_shape(self):
        data = json.loads((REPO_ROOT / "js" / "danbooru_attire_data.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data), set(self.tool.SLOT_ORDER) | {self.tool.VOCABULARY_KEY})
        for slot in self.tool.SLOT_ORDER:
            self.assertTrue(data[slot], slot)

        self.assertIn("skirt", data["bottom"])
        self.assertIn("shirt", data["top"])
        self.assertIn("socks", data["socks"])
        self.assertIn("sandals", data["shoes"])
        self.assertIn("school uniform", data["uniform"])
        self.assertIn("kimono", data["traditional"])
        self.assertIn("earrings", data["decoration"])

        # The vocabulary is a superset of every slot.
        for slot in self.tool.SLOT_ORDER:
            self.assertTrue(set(data[slot]).issubset(set(data[self.tool.VOCABULARY_KEY])), slot)


if __name__ == "__main__":
    unittest.main()
