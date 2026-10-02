"""Small inspectable BM25 baseline; no claim of semantic retrieval."""

import math
import re
from collections import Counter


def tokenize(text):
    words = re.findall(r"[a-z0-9]+", text.casefold())
    for span in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(span if len(span) == 1 else [span[index:index + 2] for index in range(len(span) - 1)])
    return words


def rank_records(records, query, limit=10):
    docs = [Counter(tokenize(record["content"])) for record in records]
    if not docs:
        return []
    lengths = [sum(doc.values()) for doc in docs]
    average = sum(lengths) / len(docs) or 1
    terms = set(tokenize(query))
    scores = [0.0] * len(docs)
    for term in terms:
        frequency = sum(term in doc for doc in docs)
        idf = math.log(1 + (len(docs) - frequency + 0.5) / (frequency + 0.5))
        for index, doc in enumerate(docs):
            count = doc[term]
            if count:
                denominator = count + 1.2 * (1 - 0.75 + 0.75 * lengths[index] / average)
                scores[index] += idf * count * 2.2 / denominator
    ordered = sorted(range(len(docs)), key=lambda index: (-scores[index], records[index]["id"]))
    return [{**records[index], "retrieval_score": round(scores[index], 6)} for index in ordered[:limit] if scores[index] > 0]
