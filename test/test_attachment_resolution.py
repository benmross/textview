import os
import tempfile
import unittest

from tools.attachment_resolution import choose_fallback


class AttachmentResolutionTests(unittest.TestCase):
    def make(self, root, name, data):
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(data)
        return path

    def test_recorded_size_selects_unique_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            short = self.make(root, "a/file.gif", b"short")
            self.make(root, "b/file.gif", b"longer")
            self.assertEqual(choose_fallback([short, os.path.join(root, "b/file.gif")], 5)[0], short)

    def test_same_name_and_size_with_different_bytes_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as root:
            one = self.make(root, "a/file.gif", b"one")
            two = self.make(root, "b/file.gif", b"two")
            self.assertEqual(choose_fallback([one, two], 3), (None, "ambiguous"))

    def test_identical_copies_are_safe(self):
        with tempfile.TemporaryDirectory() as root:
            one = self.make(root, "a/file.gif", b"same")
            two = self.make(root, "b/file.gif", b"same")
            chosen, reason = choose_fallback([two, one], 4)
            self.assertEqual(chosen, one)
            self.assertEqual(reason, "identical-copies")

    def test_placeholder_size_does_not_make_first_candidate_safe(self):
        with tempfile.TemporaryDirectory() as root:
            one = self.make(root, "a/file.gif", b"one")
            two = self.make(root, "b/file.gif", b"two")
            self.assertEqual(choose_fallback([one, two], 1024 * 1024), (None, "ambiguous"))


if __name__ == "__main__":
    unittest.main()
