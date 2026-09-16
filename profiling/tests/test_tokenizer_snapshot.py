import tempfile
import unittest
from pathlib import Path

from xgrammar_profile.config import TOKENIZER_REVISION
from xgrammar_profile.tokenizer_snapshot import TokenizerSnapshotError, prepare_tokenizer


class TokenizerSnapshotTests(unittest.TestCase):
    def test_local_source_cannot_claim_an_unverified_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with self.assertRaisesRegex(TokenizerSnapshotError, "git checkout"):
                prepare_tokenizer(
                    repository="Qwen/Qwen3-0.6B",
                    revision=TOKENIZER_REVISION,
                    output=root / "output",
                    local_source=source,
                )


if __name__ == "__main__":
    unittest.main()
