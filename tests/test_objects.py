# tests/test_objects.py
"""Task 7 gate: object tree / properties / attributes on the corpus's real
tables. Ground truth verified empirically (release 88 zork1.z3, base
0x02E5 = h.objects + 2*31 - 9): 250/250 property-table short names decode
to real Zork I object names, and the dfrotz -t walkthrough ('open mailbox'
-> leaflet) matches the tree below. The ZSpec 1.0 overview's object numbers
(239/68/127/80) are from a different release and do NOT apply to this file."""
import unittest
from pathlib import Path

from zmach.storyfile import StoryFile
from zmach.vm import VM

C = Path(__file__).parent / "corpus"
SEED = 10


class TestZork1Objects(unittest.TestCase):
    """v3 (9-byte entries, 4 attr bytes, 1-byte parent/sibling/child,
    2-byte property addr; property headers (size-1)<<5 | num)."""

    def setUp(self):
        self.vm = VM(StoryFile.load(C / "zork1.z3"), seed=SEED)
        vm = self.vm
        # object table base = h.objects + 2*31 - 9 (ledger T5-objects)
        self.assertEqual(vm.objects_base, 0x02B0 + 2 * 31 - 9)
        self.assertEqual(vm.obj_entry(160), 0x02E5 + 160 * 9)

    # tree facts (names verified via property-table short names):
    #   West of House = 180 (parent 82, child 181 'door', sibling 15)
    #   door          = 181 (parent 180, sibling 160)
    #   small mailbox = 160 (parent 180, sibling 0, child 161 'leaflet')
    #   leaflet       = 161 (parent 160, sibling 0, child 0)
    def test_mailbox_tree(self):
        vm = self.vm
        self.assertEqual(vm.get_parent(160), 180)
        self.assertEqual(vm.get_sibling(160), 0)
        self.assertEqual(vm.get_child(160), 161)
        self.assertEqual(vm.get_parent(161), 160)
        # door <-> room <-> mailbox chain
        self.assertEqual(vm.get_parent(181), 180)
        self.assertEqual(vm.get_sibling(181), 160)
        self.assertEqual(vm.get_parent(180), 82)
        self.assertEqual(vm.get_child(180), 181)
        # object 0 = nothing
        self.assertEqual(vm.get_parent(0), 0)
        self.assertEqual(vm.get_child(0), 0)

    def test_attributes(self):
        vm = self.vm
        # mailbox 160: attr bytes 00 04 10 00 -> attrs 13 and 19 (MSB-first)
        self.assertTrue(vm.test_attr(160, 13))
        self.assertTrue(vm.test_attr(160, 19))
        self.assertFalse(vm.test_attr(160, 0))
        self.assertFalse(vm.test_attr(160, 14))
        # West of House 180: bytes 02 40 08 00 -> attrs 6, 9, 20 (Container)
        self.assertTrue(vm.test_attr(180, 20))
        self.assertTrue(vm.test_attr(180, 6))
        self.assertTrue(vm.test_attr(180, 9))
        self.assertFalse(vm.test_attr(180, 31))
        # object 0: no attributes
        self.assertFalse(vm.test_attr(0, 13))

    def test_properties(self):
        vm = self.vm
        # mailbox property table (0x1a38): [18] 4b, [17] 2b, [16] 1b, [10] 2b
        self.assertEqual(vm.get_prop(160, 16), 0xF4)          # 1-byte, zero-extended
        self.assertEqual(vm.get_prop(160, 10), 0x000A)        # 2-byte
        self.assertEqual(vm.get_prop(160, 17), 0x6E94)
        # missing property -> defaults table. defprop = h.objects - 2 =
        # 0x02AE; file facts: word 0 = 0x00F5 (packed "You're " pointer),
        # word 15 = 0x0005, words 1-14/16-30 = 0x0000. Mailbox has no prop 15.
        self.assertEqual(vm.defprop, 0x02AE)
        self.assertEqual(vm.mem.getw(vm.defprop), 0x00F5)
        self.assertEqual(vm.get_prop(160, 15), 0x0005)
        # get_prop_addr: v3 returns the data address; 0 for missing
        self.assertGreater(vm.get_prop_addr(160, 16), 0)
        self.assertEqual(vm.get_prop_addr(160, 3), 0)


class TestPlanetfallObjects(unittest.TestCase):
    """v5 (14-byte entries, 6 attr bytes, word parent/sibling/child) —
    guards the v5 path that Task 5's transcript gate relies on."""

    def setUp(self):
        self.vm = VM(StoryFile.load(C / "planetfall.z5"), seed=SEED)
        vm = self.vm
        self.assertEqual(vm.objects_base, 0x00516)

    def test_deck_nine(self):
        vm = self.vm
        # obj 56 = 'Deck Nine' (verified Task 5: g163 = 56)
        self.assertEqual(vm.get_parent(56), 47)   # from the verified obj-1 entry layout
        self.assertGreater(vm.get_prop_addr(56, 46), 0)  # props: 61,57,55,53,46,43,42,41


if __name__ == "__main__":
    unittest.main()
