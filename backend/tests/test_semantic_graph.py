"""验证全文语义抽取、证据回定位和引用解析。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

# 允许从仓库根目录直接执行 unittest discover。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.semantic_graph import SemanticGraphExtractor, SemanticSourceDocument, TextChunk
from app.services.model_client import ModelCallError, ModelCallResult, ModelUsage
from app.agents.domainTree_agent import DomainTreeAgent, SourceDocument


class SemanticGraphExtractorTest(unittest.TestCase):
    """覆盖语义抽取主链路中不依赖真实模型服务的行为。"""

    def test_extracts_entities_relations_evidence_and_citations(self) -> None:
        """实体关系必须携带可回定位证据，引用必须连接正文标记。"""
        markdown = """# Example Paper

## Method

Method A improves Dataset B accuracy to 95%. Prior work is described in [1, 2].

## References

1. Smith, J.: Earlier Method. Journal 10, 1-8 (2020). https://doi.org/10.1000/test

2. Doe, A.: Another Study. Conference (2021)
"""
        model_payload = {
            "entities": [
                {
                    "localId": "e1",
                    "name": "Method A",
                    "canonicalName": "Method A",
                    "type": "method",
                    "aliases": [],
                    "attributes": [],
                    "evidenceQuote": "Method A improves Dataset B accuracy to 95%.",
                },
                {
                    "localId": "e2",
                    "name": "Dataset B",
                    "canonicalName": "Dataset B",
                    "type": "dataset",
                    "aliases": [],
                    "attributes": [{"name": "accuracy", "value": "95%", "unit": ""}],
                    "evidenceQuote": "Method A improves Dataset B accuracy to 95%.",
                },
            ],
            "relations": [
                {
                    "source": "e1",
                    "target": "e2",
                    "predicate": "improves accuracy",
                    "relationType": "experimental",
                    "confidence": 0.93,
                    "evidenceQuote": "Method A improves Dataset B accuracy to 95%.",
                }
            ],
        }

        call_options: list[dict[str, object]] = []

        def fake_chat(*args: object, **kwargs: object) -> str:
            """返回固定 JSON，避免测试访问外部模型。"""
            call_options.append(dict(kwargs))
            return json.dumps(model_payload)

        with tempfile.TemporaryDirectory() as directory:
            markdown_path = Path(directory) / "paper.md"
            markdown_path.write_text(markdown, encoding="utf-8")
            extractor = SemanticGraphExtractor(
                {"model": "test", "base_url": "http://localhost", "provider": "custom"},
                chat_fn=fake_chat,
            )
            result = extractor.extract(
                [SemanticSourceDocument("paper-1", "Example Paper", markdown_path)]
            )

        self.assertEqual(len(result["entities"]), 2)
        self.assertEqual(len(result["semanticRelations"]), 1)
        self.assertEqual(len(result["citations"]), 2)
        self.assertTrue(result["citations"][0]["contexts"])
        self.assertEqual(result["citations"][0]["doi"], "10.1000/test")
        relation = result["semanticRelations"][0]
        self.assertEqual(relation["relationType"], "experimental")
        self.assertTrue(relation["evidenceIds"])
        self.assertIn(relation["evidenceIds"][0], {item["id"] for item in result["evidence"]})
        self.assertTrue(call_options)
        self.assertTrue(all(options.get("thinking") is False for options in call_options))

    def test_discards_relation_when_quote_is_not_in_source(self) -> None:
        """模型虚构的证据无法回定位时，不得进入最终关系图。"""
        payload = {
            "entities": [
                {"localId": "a", "name": "A", "canonicalName": "A", "type": "method"},
                {"localId": "b", "name": "B", "canonicalName": "B", "type": "dataset"},
            ],
            "relations": [
                {
                    "source": "a",
                    "target": "b",
                    "predicate": "causes",
                    "relationType": "causal",
                    "confidence": 1,
                    "evidenceQuote": "这句话并不存在于原文",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text("# Paper\n\n## Result\n\nA is evaluated on B.", encoding="utf-8")
            extractor = SemanticGraphExtractor(
                {"model": "test"},
                chat_fn=lambda *args, **kwargs: json.dumps(payload),
            )
            result = extractor.extract([SemanticSourceDocument("p", "Paper", path)])

        self.assertEqual(result["semanticRelations"], [])

    def test_expands_citation_ranges(self) -> None:
        """连续引用编号应展开为每一条独立引用。"""
        extractor = SemanticGraphExtractor(None)
        contexts = extractor._find_inline_citation_contexts("Result follows prior work [2-4, 7].")
        self.assertEqual(sorted(contexts), [2, 3, 4, 7])

    def test_parses_unnumbered_author_year_references(self) -> None:
        """无编号的作者—年份制参考文献也应建立正文上下文。"""
        extractor = SemanticGraphExtractor(None)
        body = "## Related Work\n\nSmith et al. (2020) introduced the baseline."
        references = "Smith, J. (2020). A Useful Baseline. Journal of Tests."
        citations = extractor.parse_citations(
            SemanticSourceDocument("paper", "Paper", None),
            body,
            references,
            reference_start_line=5,
            local_titles={},
        )
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["year"], 2020)
        self.assertTrue(citations[0]["contexts"])

    def test_parses_acm_reference_without_using_author_initial_as_title(self) -> None:
        """ACM 作者年份格式不得把首字母 S 或整条参考文献当作标题。"""
        extractor = SemanticGraphExtractor(None)
        raw = (
            "S. Sobitha Ahila and K.L. Shunmuganathan. 2014. "
            "State Of Art In Homomorphic Encryption. "
            "International Journal of Computer Applications 104 (2014), 15-19."
        )

        citations = extractor.parse_citations(
            SemanticSourceDocument("paper", "Paper", None),
            "## Related Work\n\nNo author-year citation is present here.",
            f"[3] {raw}",
            reference_start_line=10,
            local_titles={},
        )

        self.assertEqual(citations[0]["title"], "State Of Art In Homomorphic Encryption")
        self.assertNotEqual(citations[0]["title"], "S")
        self.assertEqual(citations[0]["metadataQuality"], "valid")
        self.assertEqual(citations[0]["contexts"], [])

    def test_doi_match_prefers_local_zotero_metadata_over_reference_text(self) -> None:
        """DOI 命中本地 Zotero 条目时，应以结构化元数据覆盖文本回退结果。"""
        extractor = SemanticGraphExtractor(None)
        citations = extractor.parse_citations(
            SemanticSourceDocument("paper", "Paper", None),
            "## Body\n\nPrior work is cited in [1].",
            "[1] X. 2020. Corrupted title. doi:10.1000/example",
            reference_start_line=8,
            local_titles={},
            local_metadata={
                "zotero:item": {
                    "source": "zotero",
                    "title": "Authoritative CSL Title",
                    "authors": ["Ada Lovelace"],
                    "year": "2021",
                    "doi": "https://doi.org/10.1000/EXAMPLE",
                }
            },
        )

        self.assertEqual(citations[0]["matchedDocumentId"], "zotero:item")
        self.assertEqual(citations[0]["title"], "Authoritative CSL Title")
        self.assertEqual(citations[0]["authors"], ["Ada Lovelace"])
        self.assertEqual(citations[0]["year"], 2021)
        self.assertEqual(citations[0]["metadataSource"], "zotero")

    def test_relation_only_links_citations_supported_by_its_evidence_context(self) -> None:
        """关系只能绑定证据所在正文引用上下文对应的参考文献。"""
        extractor = SemanticGraphExtractor(None)
        relations = [{"id": "r1", "evidenceIds": ["e1"], "documentIds": ["paper"]}]
        evidence = [{
            "id": "e1",
            "documentId": "paper",
            "lineStart": 480,
            "quote": "The optimized parameters remain vulnerable to subfield lattice attacks (Albrecht et al. 2016).",
        }]
        citations = [
            {
                "id": "citation:paper:3",
                "documentId": "paper",
                "referenceNumber": 3,
                "year": 2014,
                "authorKeys": ["Ahila"],
                "contexts": [],
            },
            {
                "id": "citation:paper:5",
                "documentId": "paper",
                "referenceNumber": 5,
                "year": 2016,
                "authorKeys": ["Albrecht"],
                "contexts": [{
                    "lineStart": 482,
                    "quote": (
                        "Optimized parameters remain vulnerable to subfield lattice "
                        "attacks (Albrecht et al. 2016). The attack affects NTRU schemes."
                    ),
                }],
            },
        ]

        extractor.bind_relation_citations(relations, evidence, citations)

        self.assertEqual(relations[0]["citationIds"], ["citation:paper:5"])

    def test_evidence_marks_mixed_language_against_english_document(self) -> None:
        """英文文献中的中英混合批注应被标注，而不是静默当作英文正文。"""
        extractor = SemanticGraphExtractor(None)
        state = {"evidence": {}, "documentLanguages": {"paper": "en"}}
        document = SemanticSourceDocument("paper", "SilentWood", None)
        chunk = TextChunk(
            1,
            "Evaluation",
            "BCC 对计算速度贡献最大，GPU acceleration is limited.",
            12,
        )

        evidence_id = extractor._add_evidence(
            state,
            document,
            chunk,
            chunk.text,
            kind="relation",
        )

        item = state["evidence"][evidence_id]
        self.assertEqual(item["language"], "mixed")
        self.assertTrue(item["languageMismatch"])

    def test_legacy_evidence_language_is_inferred_from_document_majority(self) -> None:
        """历史图谱也应按同文档证据的整体语言补齐异常片段标记。"""
        extractor = SemanticGraphExtractor(None)
        evidence = [
            {
                "id": "e1",
                "documentId": "paper",
                "quote": "This English evidence sentence describes private inference performance.",
            },
            {
                "id": "e2",
                "documentId": "paper",
                "quote": "Another English paragraph explains the experimental evaluation in detail.",
            },
            {
                "id": "e3",
                "documentId": "paper",
                "quote": "BCC 对计算速度贡献最大，GPU acceleration is limited.",
            },
        ]

        extractor.annotate_evidence_languages(evidence)

        self.assertEqual(evidence[2]["documentLanguage"], "en")
        self.assertEqual(evidence[2]["language"], "mixed")
        self.assertTrue(evidence[2]["languageMismatch"])

    def test_retries_timeout_and_reports_chunk_progress(self) -> None:
        """语义分块超时应有限重试，并持续上报可观察进度。"""
        calls = 0
        updates: list[dict] = []

        def flaky_chat(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise requests.ReadTimeout("temporary timeout")
            return json.dumps({"entities": [], "relations": []})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text("# Method\n\nA short research method description.", encoding="utf-8")
            extractor = SemanticGraphExtractor(
                {"model": "test"},
                chat_fn=flaky_chat,
                progress_callback=updates.append,
            )
            with (
                patch("app.services.semantic_graph.settings.domain_tree_retry_attempts", 2),
                patch("app.services.semantic_graph.settings.domain_tree_retry_base_delay_seconds", 0),
            ):
                result = extractor.extract([SemanticSourceDocument("p", "Paper", path)])

        self.assertEqual(calls, 2)
        self.assertEqual(result["extraction"]["processedChunkCount"], 1)
        self.assertEqual(updates[-1]["completedChunks"], 1)
        self.assertEqual(updates[-1]["pendingChunks"], 0)

    def test_retries_structured_empty_response_and_preserves_failed_usage(self) -> None:
        """供应商偶发空回答应按配置重试，失败尝试的 Token 用量不能丢失。"""
        calls = 0
        metrics: list[dict] = []

        def flaky_chat(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelCallError(
                    "模型返回了空回答",
                    category="empty_response",
                    retryable=True,
                    request_accepted=True,
                    request_id="empty-request",
                    finish_reason="stop",
                    usage=ModelUsage(
                        prompt_tokens=100,
                        completion_tokens=200,
                        total_tokens=300,
                        reasoning_tokens=200,
                    ),
                )
            return json.dumps({"entities": [], "relations": []})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text("# Method\n\nA short research method description.", encoding="utf-8")
            extractor = SemanticGraphExtractor(
                {"model": "test"},
                chat_fn=flaky_chat,
                metric_callback=metrics.append,
            )
            with (
                patch("app.services.semantic_graph.settings.domain_tree_retry_attempts", 2),
                patch("app.services.semantic_graph.settings.domain_tree_retry_base_delay_seconds", 0),
            ):
                result = extractor.extract([SemanticSourceDocument("p", "Paper", path)])

        self.assertEqual(calls, 2)
        self.assertEqual(result["extraction"]["processedChunkCount"], 1)
        self.assertEqual(metrics[0]["status"], "failed")
        self.assertEqual(metrics[0]["errorCategory"], "empty_response")
        self.assertEqual(metrics[0]["requestId"], "empty-request")
        self.assertEqual(metrics[0]["finishReason"], "stop")
        self.assertEqual(metrics[0]["completionTokens"], 200)
        self.assertEqual(metrics[0]["totalTokens"], 300)
        self.assertEqual(metrics[0]["reasoningTokens"], 200)

    def test_cancelled_extraction_stops_before_model_call(self) -> None:
        """已取消任务不得继续启动新的模型请求。"""
        cancel_event = threading.Event()
        cancel_event.set()
        extractor = SemanticGraphExtractor({"model": "test"}, cancel_event=cancel_event)

        from app.services.task_control import DomainTreeGenerationCancelled

        with self.assertRaises(DomainTreeGenerationCancelled):
            extractor.extract([])

    def test_logs_chunk_performance_and_limited_model_output(self) -> None:
        """每个分块应记录耗时、结果计数和受限长度的模型输出预览。"""
        long_name = "A" * 2500
        model_payload = {
            "entities": [
                {
                    "localId": "e1",
                    "name": long_name,
                    "canonicalName": long_name,
                    "type": "method",
                    "evidenceQuote": "Method A is evaluated.",
                }
            ],
            "relations": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text("# Method\n\nMethod A is evaluated.", encoding="utf-8")
            extractor = SemanticGraphExtractor(
                {"model": "test", "provider": "custom"},
                chat_fn=lambda *args, **kwargs: json.dumps(model_payload),
            )
            with self.assertLogs("app.services.semantic_graph", level="INFO") as captured:
                extractor.extract([SemanticSourceDocument("paper", "Paper", path)])

        combined = "\n".join(captured.output)
        self.assertIn("语义分块模型请求开始", combined)
        self.assertIn("语义分块模型请求完成", combined)
        self.assertIn("elapsed_ms=", combined)
        self.assertIn("entity_count=1", combined)
        self.assertIn("<truncated", combined)

    def test_extraction_prompt_requires_source_language(self) -> None:
        """实体、关系谓词与证据必须明确要求保留原文语言。"""
        extractor = SemanticGraphExtractor({"model": "test"})
        prompt = extractor._build_extraction_prompt(
            SemanticSourceDocument("paper", "English Paper", None),
            TextChunk(1, "Method", "Method A improves accuracy.", 1),
        )

        self.assertIn("canonicalName", prompt)
        self.assertIn("must preserve the source language", prompt)
        self.assertIn("predicate must preserve the source language", prompt)
        self.assertIn("type is an inferred category label", prompt)
        self.assertIn('"type": "model"', prompt)
        self.assertNotIn('"type": "实体类型"', prompt)

    def test_normalizes_invalid_type_without_second_model_call(self) -> None:
        """英文模式遇到中文类型时应定向纠正一次，并保留其余原文字段。"""
        calls: list[list[dict[str, str]]] = []
        invalid_payload = {
            "entities": [
                {
                    "localId": "e1",
                    "name": "Method A",
                    "canonicalName": "Method A",
                    "type": "方法",
                    "aliases": [],
                    "attributes": [],
                    "evidenceQuote": "Method A improves accuracy.",
                }
            ],
            "relations": [],
        }
        def fake_chat(*args: object, **kwargs: object) -> str:
            calls.append(args[1])
            return json.dumps(invalid_payload, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text("# Method\n\nMethod A improves accuracy.", encoding="utf-8")
            result = SemanticGraphExtractor(
                {"model": "test"},
                entity_type_language="English",
                chat_fn=fake_chat,
            ).extract([SemanticSourceDocument("paper", "English Paper", path)])

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["entities"][0]["type"], "entity")
        self.assertEqual(result["entities"][0]["name"], "Method A")

    def test_replaces_repeated_language_violation_with_generic_type(self) -> None:
        """纠正后仍违规时保留实体，但使用目标语言下的通用类型。"""
        calls = 0
        payload = {
            "entities": [
                {
                    "localId": "e1",
                    "name": "Method A",
                    "canonicalName": "Method A",
                    "type": "方法",
                    "evidenceQuote": "Method A improves accuracy.",
                }
            ],
            "relations": [],
        }

        def fake_chat(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return json.dumps(payload, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.md"
            path.write_text("# Method\n\nMethod A improves accuracy.", encoding="utf-8")
            result = SemanticGraphExtractor(
                {"model": "test"},
                entity_type_language="English",
                chat_fn=fake_chat,
            ).extract([SemanticSourceDocument("paper", "English Paper", path)])

        self.assertEqual(calls, 1)
        self.assertEqual(result["entities"][0]["type"], "entity")
        self.assertEqual(result["entities"][0]["rawTypes"], ["方法"])

    def test_entity_type_language_is_part_of_chunk_cache_key(self) -> None:
        """中文和英文分析不得共享语义分块缓存。"""
        document = SemanticSourceDocument("paper", "Paper", None)
        chunk = TextChunk(1, "Method", "Method A improves accuracy.", 1)
        english = SemanticGraphExtractor({"model": "test"}, entity_type_language="English")
        chinese = SemanticGraphExtractor({"model": "test"}, entity_type_language="中文")

        self.assertNotEqual(
            english._chunk_cache_key(document, chunk),
            chinese._chunk_cache_key(document, chunk),
        )

    def test_english_type_validator_rejects_non_english_scripts(self) -> None:
        """英文类型不仅要排除中文，也必须包含 ASCII 英文字母。"""
        extractor = SemanticGraphExtractor(None, entity_type_language="English")

        self.assertTrue(extractor._is_valid_entity_type_language("data structure"))
        self.assertFalse(extractor._is_valid_entity_type_language("方法"))
        self.assertFalse(extractor._is_valid_entity_type_language("модель"))

    def test_ignores_cached_payload_with_wrong_type_language(self) -> None:
        """即使缓存键匹配，类型语言违规的缓存也必须按未命中处理。"""
        calls = 0
        invalid_payload = {
            "entities": [{"localId": "e1", "name": "A", "type": "方法"}],
            "relations": [],
        }

        def fake_chat(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return json.dumps({"entities": [], "relations": []})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "paper.md"
            path.write_text("# Method\n\nA cached research method.", encoding="utf-8")
            document = SemanticSourceDocument("paper", "Paper", path)
            extractor = SemanticGraphExtractor(
                {"model": "test"},
                entity_type_language="English",
                chat_fn=fake_chat,
                cache_dir=root / "cache",
            )
            chunk = extractor.split_chunks("# Method\n\nA cached research method.")[0]
            extractor._save_cached_payload(extractor._chunk_cache_key(document, chunk), invalid_payload)
            result = extractor.extract([document])

        self.assertEqual(calls, 1)
        self.assertEqual(result["extraction"]["cacheHitCount"], 0)
        self.assertEqual(result["extraction"]["cacheMissCount"], 1)

    def test_dynamically_merges_equivalent_entity_types_without_fixed_taxonomy(self) -> None:
        """类型归并应依据本次模型映射合并大小写与跨语言等价标签。"""
        def fake_chat(*args: object, **kwargs: object) -> str:
            messages = args[1]
            self.assertIn("semantically equivalent", messages[0]["content"])
            return json.dumps(
                {
                    "groups": [
                        {"canonical": "algorithm", "members": ["algorithm", "算法"]},
                        {"canonical": "dataset", "members": ["dataset"]},
                    ]
                },
                ensure_ascii=False,
            )

        entities = [
            {"name": "A", "type": "Algorithm", "typeCounts": {"Algorithm": 1}},
            {"name": "B", "type": "algorithm", "typeCounts": {"algorithm": 1}},
            {"name": "C", "type": "算法", "typeCounts": {"算法": 1}},
            {"name": "D", "type": "Dataset", "typeCounts": {"Dataset": 1}},
        ]
        extractor = SemanticGraphExtractor({"model": "test"}, chat_fn=fake_chat)

        stats = extractor._canonicalize_entity_types(entities)

        self.assertEqual([entity["type"] for entity in entities], ["algorithm", "algorithm", "algorithm", "dataset"])
        self.assertEqual(stats["entityTypeCountBefore"], 4)
        self.assertEqual(stats["entityTypeCountAfter"], 2)
        self.assertEqual(stats["entityTypeNormalizationMode"], "dynamic_model")
        self.assertEqual(entities[0]["rawTypes"], ["Algorithm"])

    def test_type_normalization_falls_back_without_guessing_semantic_synonyms(self) -> None:
        """没有模型时只统一表面形式，不通过固定词表猜测跨语言同义词。"""
        entities = [
            {"name": "A", "type": "Algorithm", "typeCounts": {"Algorithm": 1}},
            {"name": "B", "type": "algorithm", "typeCounts": {"algorithm": 1}},
            {"name": "C", "type": "算法", "typeCounts": {"算法": 1}},
        ]
        extractor = SemanticGraphExtractor(None)

        stats = extractor._canonicalize_entity_types(entities)

        self.assertEqual([entity["type"] for entity in entities], ["algorithm", "algorithm", "entity"])
        self.assertEqual(stats["entityTypeCountAfter"], 2)
        self.assertEqual(stats["entityTypeNormalizationMode"], "deterministic")

    def test_translates_canonical_when_group_has_no_target_language_member(self) -> None:
        """全部为中文的等价组允许生成英文 canonical，但成员必须完整覆盖输入。"""
        def fake_chat(*args: object, **kwargs: object) -> str:
            return json.dumps(
                {"groups": [{"canonical": "algorithm", "members": ["算法"]}]},
                ensure_ascii=False,
            )

        entities = [{"name": "A", "type": "算法", "typeCounts": {"算法": 1}}]
        extractor = SemanticGraphExtractor(
            {"model": "test"},
            entity_type_language="English",
            chat_fn=fake_chat,
        )

        stats = extractor._canonicalize_entity_types(entities)

        self.assertEqual(entities[0]["type"], "algorithm")
        self.assertEqual(entities[0]["rawTypes"], ["算法"])
        self.assertEqual(stats["entityTypeNormalizationMode"], "dynamic_model")

    def test_reuses_dynamic_type_mapping_cache(self) -> None:
        """相同模型和类型集合不应重复请求类型归并。"""
        calls = 0

        def fake_chat(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return json.dumps(
                {"groups": [{"canonical": "algorithm", "members": ["algorithm", "算法"]}]},
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            extractor = SemanticGraphExtractor(
                {"model": "test", "provider": "custom"},
                chat_fn=fake_chat,
                cache_dir=directory,
            )
            for _ in range(2):
                entities = [
                    {"name": "A", "type": "Algorithm", "typeCounts": {"Algorithm": 1}},
                    {"name": "B", "type": "算法", "typeCounts": {"算法": 1}},
                ]
                extractor._canonicalize_entity_types(entities)
                self.assertEqual([entity["type"] for entity in entities], ["algorithm", "algorithm"])

        self.assertEqual(calls, 1)

    def test_rejects_type_mapping_that_invents_a_taxonomy_label(self) -> None:
        """模型不得把动态归并变成引入新类型的隐式固定分类体系。"""
        extractor = SemanticGraphExtractor(None)
        mapping = extractor._validate_type_mapping(
            {
                "groups": [
                    {"canonical": "technique", "members": ["algorithm", "算法"]},
                ]
            },
            ["algorithm", "算法"],
        )
        self.assertIsNone(mapping)

    def test_reuses_persistent_chunk_cache(self) -> None:
        """相同模型和原文的第二次抽取应直接复用磁盘缓存。"""
        calls = 0

        def fake_chat(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return json.dumps({"entities": [], "relations": []})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "paper.md"
            path.write_text("# Method\n\nA cached research method.", encoding="utf-8")
            document = SemanticSourceDocument("paper", "Paper", path)
            first = SemanticGraphExtractor(
                {"model": "test", "provider": "custom"},
                chat_fn=fake_chat,
                cache_dir=root / "cache",
            ).extract([document])
            second = SemanticGraphExtractor(
                {"model": "test", "provider": "custom"},
                chat_fn=fake_chat,
                cache_dir=root / "cache",
            ).extract([document])

        self.assertEqual(calls, 1)
        self.assertEqual(first["extraction"]["cacheMissCount"], 1)
        self.assertEqual(second["extraction"]["cacheHitCount"], 1)
        self.assertEqual(second["extraction"]["cacheMissCount"], 0)

    def test_limits_parallel_chunk_requests_to_four(self) -> None:
        """缓存未命中的模型请求应并行执行，但同时最多运行四个。"""
        active_calls = 0
        maximum_active_calls = 0
        lock = threading.Lock()

        def fake_chat(*args: object, **kwargs: object) -> str:
            nonlocal active_calls, maximum_active_calls
            with lock:
                active_calls += 1
                maximum_active_calls = max(maximum_active_calls, active_calls)
            time.sleep(0.03)
            with lock:
                active_calls -= 1
            return json.dumps({"entities": [], "relations": []})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents: list[SemanticSourceDocument] = []
            for index in range(8):
                path = root / f"paper-{index}.md"
                path.write_text(f"# Method\n\nResearch method number {index}.", encoding="utf-8")
                documents.append(SemanticSourceDocument(f"paper-{index}", f"Paper {index}", path))
            result = SemanticGraphExtractor(
                {"model": "test", "provider": "custom"},
                chat_fn=fake_chat,
                max_workers=4,
            ).extract(documents)

        self.assertEqual(result["extraction"]["processedChunkCount"], 8)
        self.assertEqual(result["extraction"]["maxWorkers"], 4)
        self.assertGreater(maximum_active_calls, 1)
        self.assertLessEqual(maximum_active_calls, 4)

    def test_reports_usage_quality_and_failure_reasons(self) -> None:
        """抽取摘要必须暴露真实用量、覆盖率和结构化失败原因。"""
        calls = 0
        metrics: list[dict] = []

        def fake_chat(*args: object, **kwargs: object) -> ModelCallResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelCallResult(
                    json.dumps({"entities": [], "relations": []}),
                    ModelUsage(100, 20, 120, 10),
                    "request-1",
                )
            raise ModelCallError(
                "模型服务返回 HTTP 402",
                category="quota_exhausted",
                http_status=402,
                request_accepted=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = []
            for index in range(2):
                path = root / f"paper-{index}.md"
                path.write_text(f"# Method\n\nResearch method {index}.", encoding="utf-8")
                documents.append(SemanticSourceDocument(f"p-{index}", f"Paper {index}", path))
            with patch("app.services.semantic_graph.settings.semantic_graph_ready_ratio", 1.0), patch(
                "app.services.semantic_graph.settings.semantic_graph_degraded_ratio",
                0.75,
            ):
                result = SemanticGraphExtractor(
                    {"model": "test"},
                    chat_fn=fake_chat,
                    max_workers=1,
                    metric_callback=metrics.append,
                ).extract(documents)

        extraction = result["extraction"]
        self.assertEqual(extraction["processedChunkCount"], 1)
        self.assertEqual(extraction["failedChunkCount"], 1)
        self.assertEqual(extraction["qualityStatus"], "failed")
        self.assertEqual(extraction["failureReasons"]["quota_exhausted"], 1)
        self.assertEqual(extraction["usage"]["totalTokens"], 120)
        self.assertEqual(len(metrics), 2)

    def test_circuit_breaker_stops_repeated_fatal_errors(self) -> None:
        """连续不可恢复错误达到配置阈值后，不得继续访问模型。"""
        calls = 0

        def rejected(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            raise ModelCallError(
                "模型服务返回 HTTP 401",
                category="authentication",
                http_status=401,
                request_accepted=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = []
            for index in range(4):
                path = root / f"paper-{index}.md"
                path.write_text(f"# Method\n\nResearch method {index}.", encoding="utf-8")
                documents.append(SemanticSourceDocument(f"p-{index}", f"Paper {index}", path))
            with patch(
                "app.services.semantic_graph.settings.semantic_graph_consecutive_fatal_limit",
                2,
            ), patch(
                "app.services.semantic_graph.settings.semantic_graph_failure_window_minimum",
                4,
            ):
                result = SemanticGraphExtractor(
                    {"model": "test"},
                    chat_fn=rejected,
                    max_workers=1,
                ).extract(documents)

        self.assertEqual(calls, 2)
        self.assertEqual(result["extraction"]["failedChunkCount"], 4)
        self.assertEqual(
            result["extraction"]["circuitOpenReason"],
            "consecutive_authentication",
        )


class DomainTreeSemanticIntegrationTest(unittest.TestCase):
    """验证全文语义结果会被并入原有领域知识图谱。"""

    @patch("app.agents.domainTree_agent.SemanticGraphExtractor.extract")
    def test_merges_semantic_nodes_and_edges(self, extract: object) -> None:
        """实体、语义关系和引用应生成兼容现有前端的节点与边。"""
        extract.return_value = {
            "entities": [
                {
                    "id": "entity:a",
                    "name": "方法 A",
                    "type": "方法",
                    "aliases": [],
                    "attributes": [],
                    "evidenceIds": ["evidence:1"],
                    "documentIds": ["paper-1"],
                },
                {
                    "id": "entity:b",
                    "name": "数据集 B",
                    "type": "数据集",
                    "aliases": [],
                    "attributes": [],
                    "evidenceIds": ["evidence:1"],
                    "documentIds": ["paper-1"],
                },
            ],
            "semanticRelations": [
                {
                    "id": "relation:1",
                    "source": "entity:a",
                    "target": "entity:b",
                    "predicate": "评测于",
                    "relationType": "experimental",
                    "confidence": 0.9,
                    "evidenceIds": ["evidence:1"],
                    "documentIds": ["paper-1"],
                }
            ],
            "citations": [
                {
                    "id": "citation:paper-1:1",
                    "documentId": "paper-1",
                    "referenceNumber": 1,
                    "title": "参考论文",
                    "rawReference": "Author: 参考论文 (2020)",
                    "contexts": [],
                }
            ],
            "evidence": [{"id": "evidence:1", "documentId": "paper-1", "quote": "证据"}],
            "extraction": {"entityCount": 2, "semanticRelationCount": 1, "citationCount": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            agent = DomainTreeAgent(storage_dir=directory, metadata_db_path=Path(directory) / "missing.db")
            graph = agent._build_knowledge_graph(
                project_id="workspace",
                documents=[SourceDocument("paper-1", "论文", "", [], None, None, [])],
                tags=[{"label": "1 测试领域", "child": [{"label": "1.1 测试方向"}]}],
                catalog_text="",
                project={},
                model_runtime={},
            )

        node_types = {node["type"] for node in graph["nodes"]}
        relations = {edge["relation"] for edge in graph["edges"]}
        self.assertIn("entity", node_types)
        self.assertIn("reference", node_types)
        self.assertIn("mentions_entity", relations)
        self.assertIn("semantic_relation", relations)
        self.assertIn("cites", relations)


if __name__ == "__main__":
    unittest.main()
