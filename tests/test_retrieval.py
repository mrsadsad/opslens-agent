import unittest
from opslens.retrieval import rank_records


class RetrievalTests(unittest.TestCase):
    def test_lexical_rank_finds_discontiguous_terms_and_is_deterministic(self):
        records = [{"id": "r1", "content": "pool connection acquisition timeout"},
                   {"id": "r2", "content": "disk space and inode exhaustion"}]
        result = rank_records(records, "connection pool")
        self.assertEqual([item["id"] for item in result], ["r1"])
        self.assertEqual(result, rank_records(list(reversed(records)), "connection pool"))
        self.assertEqual(rank_records(records, "不存在的词"), [])

    def test_chinese_text_without_whitespace(self):
        records = [{"id": "r1", "content": "连接池耗尽排查"}, {"id": "r2", "content": "磁盘空间不足"}]
        self.assertEqual(rank_records(records, "连接池")[0]["id"], "r1")


if __name__ == "__main__":
    unittest.main()
