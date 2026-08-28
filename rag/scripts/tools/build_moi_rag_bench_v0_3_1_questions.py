#!/usr/bin/env python3
"""Build the QA-only v0.3.1 revision without changing the frozen v0.3 corpus."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PACKAGE = ROOT / "datasets/moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval"
SOURCE_BANK = ROOT / "datasets/moi-rag-bench-v0.2-raw-corpus/ready_for_eval"
SOURCE_MLLM_AUDIT = ROOT / "runs/readiness/mllm-semantic-audit-20260825-v0.3-final-balanced/audit.jsonl"
DEFAULT_OUTPUT = ROOT / "datasets/moi-rag-bench-v0.3.1-qa-revision"
DATASET_ID = "moi-rag-bench-v0.3.1-qa-revision"
REVISION = "qa-revision-v0.3.1"

REMOVE_IDS = {
    # DocBench: defective gold, noisy surface, or redundant low-value fact lookup.
    "moi500d_qa_docbench_2a92b9d6f137",
    "moi500d_qa_docbench_3c1d39166ae5",
    "moi500d_qa_docbench_4b03d8c80b32",
    "moi500d_qa_docbench_e92ad83421e7",
    "moi500d_qa_docbench_61503c9beb8c",
    "moi500d_qa_docbench_7979c046cc4f",
    "moi500d_qa_docbench_d681a87c53f9",
    "moi500d_qa_docbench_33dd2c8a7036",
    "moi500d_qa_docbench_fa94287dcc3a",
    "moi500d_qa_docbench_6c41ab61139d",
    "moi500d_qa_docbench_28433b0721b8",
    "moi500d_qa_docbench_ccb1f16c8039",
    "moi500d_qa_docbench_830d44518464",
    "moi500d_qa_docbench_be6672446ee3",
    "moi500d_qa_docbench_8adf6617db25",
    "moi500d_qa_docbench_2175a4d8c115",
    "moi500d_qa_docbench_2e28ee0149a2",
    "moi500d_qa_docbench_de1a0b10201f",
    "moi500d_qa_docbench_d0ff42b950f5",
    "moi500d_qa_docbench_46d292a916ee",
    "moi500d_qa_docbench_fed7e9d10674",
    "moi500d_qa_docbench_f99307768f8c",
    "moi500d_qa_docbench_87400470ee75",
    # MultiHop: repeated gold-document sets or an artificial comparison.
    "moi500d_qa_multihop_33dab87e59bf",
    "moi500d_qa_multihop_079b771dcc84",
    "moi500d_qa_multihop_ddc42f228f6f",
    "moi500d_qa_multihop_b3ff7fa32363",
    "moi500d_qa_multihop_ea8cfee0a197",
    "moi500d_qa_multihop_a157a2cf7478",
    "moi500d_qa_multihop_a41f80dab950",
    "moi500d_qa_multihop_13bf1b87c15a",
    "moi500d_qa_multihop_6ac2e17c91ae",
    "moi500d_qa_multihop_19ebec80a925",
}

TYPE_OVERRIDES = {
    # Four retained DocBench calculations become explicit metadata/structured cases.
    "moi500d_qa_docbench_029e997b904e": "meta-data",
    "moi500d_qa_docbench_17ae51aa44b3": "meta-data",
    "moi500d_qa_docbench_57aa519a5f84": "meta-data",
    "moi500d_qa_docbench_be7565ca78c2": "meta-data",
    # Existing enterprise rows that already ask for complete procedures or all fields.
    "moi500d_qa_enterprise_29a2d9fba863": "completeness",
    "moi500d_qa_enterprise_3aac5fa2dd18": "completeness",
    "moi500d_qa_enterprise_5c21e0985fd1": "completeness",
    "moi500d_qa_enterprise_790c13d7307a": "completeness",
    "moi500d_qa_enterprise_cb99f8e407fe": "completeness",
    "moi500d_qa_enterprise_e4e802b4bb98": "completeness",
    "moi500d_qa_enterprise_41e18c0ba07a": "completeness",
    "moi500d_qa_enterprise_c1facdde78f8": "completeness",
    # Existing rules rewritten to test explicit output/decision constraints.
    "moi500d_qa_enterprise_624112622927": "constrained",
    "moi500d_qa_enterprise_15312810c965": "constrained",
}

ROW_REWRITES: dict[str, dict[str, str]] = {
    "moi500d_qa_docbench_1010a7215ff3": {
        "question": "What is the model performance on SemEval-SS Frozen when SenseBERT is initialized from RoBERTa base?",
    },
    "moi500d_qa_docbench_029e997b904e": {
        "question": "According to Chevron's 2021 annual report, how much of the year-end 2021 environmental reserves balance related to U.S. downstream operations? Return the amount in the report's displayed unit.",
    },
    "moi500d_qa_docbench_17ae51aa44b3": {
        "question": "In Berkshire Hathaway's 2021 annual report table for K-47's service and retailing businesses, what was the 2020-versus-2019 percentage change in pretax earnings? Return the direction and percentage.",
    },
    "moi500d_qa_docbench_57aa519a5f84": {
        "question": "According to Wells Fargo's 2021 annual report Table 22.1, what were total restructuring charges as of December 31, 2020? Return USD millions.",
    },
    "moi500d_qa_docbench_be7565ca78c2": {
        "question": "According to Siemens Healthineers' 2021 report, how much Varian-acquisition goodwill was allocated to the Imaging segment? Return EUR millions.",
    },
    "moi500d_qa_docbench_f41a022529e3": {
        "question": "How does CodeBERT's natural-language code-search approach differ from the models reported by the CodeSearchNet challenge?",
    },
    "moi500d_qa_enterprise_00bd5fd2a4ba": {
        "question": "According to Sara's NVIDIA H200 80GB Dedicated announcement for eu-central-1 and ap-south-1, when did reservations open?",
    },
    "moi500d_qa_enterprise_6730a8be60fb": {
        "question": "According to Jess's NVIDIA H200 80GB Dedicated announcement for eu-central-1 and ap-south-1, when did reservations open?",
    },
    "moi500d_qa_enterprise_911d4a135551": {
        "question": "Comparing the Copperfield Nimbus workshop notes with the 2026-03-12 follow-up, what baseline, six-month growth, peak QPS, and peak concurrent-chat values should the current Dedicated sizing plan use?",
    },
    "moi500d_qa_enterprise_624112622927": {
        "question": "Using the prompt-experiment guidance, design a test with no more than four few-shot examples that includes a deliberate mistake and immediate correction. State the example order, required controls, and metrics for a few hundred probes.",
    },
    "moi500d_qa_enterprise_15312810c965": {
        "question": "Under the hiring approval matrix, classify an offer 20% above the band midpoint. Return exactly two fields, `range` and `required_signoff`, with approver roles in chain order.",
        "reference_answer": "{\"range\":\"+10% to +25%\",\"required_signoff\":[\"Hiring Manager\",\"HR Business Partner (HRBP)\"]}",
    },
}

MULTIHOP_REWRITES = {
    row["parent_question_id"]: row
    for row in (
        json.loads(line)
        for line in r'''
{"parent_question_id":"moi500d_qa_multihop_54b6e03717f3","question":"Are Fortune's October 13 and TechCrunch's October 19 reports on the effects of the Gaza blockade consistent? Give the conclusion first, then one key fact from each report.","reference_answer":"Consistent. Fortune says the blockade prevents civilians and items such as medicine from moving easily into or out of Gaza. TechCrunch says airstrikes and the total blockade cut electricity, water, and vital supplies and devastated Gaza."}
{"parent_question_id":"moi500d_qa_multihop_c629055a2ee6","question":"Are Fortune's October 4 and TechCrunch's October 6 accounts of Sam Bankman-Fried's role in the use of customer funds consistent? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. Fortune reports the government's claim that Bankman-Fried used Caroline Ellison as a front for secret access to customer money. TechCrunch says Ellison testified that she took $14 billion from customers to repay lenders under Bankman-Fried's instruction."}
{"parent_question_id":"moi500d_qa_multihop_4a02e1b63c06","question":"Are the two December 6 Independent reports on Taylor Swift and Travis Kelce's relationship consistent? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. One report says Swift had nothing to hide about the relationship. The other says she connected with Kelce in July after his failed attempt to give her a friendship bracelet bearing his phone number."}
{"parent_question_id":"moi500d_qa_multihop_c11b7d1208a9","question":"Are the Week 11 injury roundup and the Week 12 and Week 14 fantasy rankings consistent about Kenneth Walker III's oblique injury? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. The injury roundup says Walker left after four carries with an oblique injury. The Week 12 ranking says the injury creates an opportunity for a rookie. The Week 14 ranking recommends starting the player under discussion if Walker remains out, while considering other options when roster depth allows."}
{"parent_question_id":"moi500d_qa_multihop_76064e76bb37","question":"Are The Sydney Morning Herald's October 1 and November 5 portrayals of the Fed's rate outlook consistent? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent. The October 1 report says the Fed would base upcoming rate decisions on incoming economic data. The November 5 report says stocks rose on hopes that rate hikes were over, while weak U.S. data was viewed as the main market game-changer."}
{"parent_question_id":"moi500d_qa_multihop_0cd091a846cd","question":"Are CBS Sports's October 12 and The Independent's December 6 accounts of Taylor Swift and Travis Kelce's relationship consistent? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. CBS Sports says Kelce tried to give Swift a friendship bracelet with his number, showing interest in her. The Independent says Swift had nothing to hide about their new relationship."}
{"parent_question_id":"moi500d_qa_multihop_6b36901bcc01","question":"Are Polygon, The Verge, and Engadget consistent in how they portray Valve's Steam Deck and store strategy? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. Polygon reports minor physical upgrades to the new Steam Deck iteration. The Verge says Valve's store focuses exclusively on games. Engadget says the Steam Deck OLED would go on sale on November 16 at 1 p.m. ET, with units ready to ship that day."}
{"parent_question_id":"moi500d_qa_multihop_49ec964515aa","question":"Are the Search-fairness, publisher-antitrust, and local-ranking reports consistent in their portrayal of Google's search practices? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. The Search-fairness report says a critic maintains that Google manipulates Search to maximize ad revenue. The publisher-antitrust report describes an allegation that Google siphons publishers' content, readers, and ad revenue. The local-ranking report states that Google uses relevance, distance, and prominence."}
{"parent_question_id":"moi500d_qa_multihop_bb2c27ac123b","question":"Are the reports on Google's default-search payments, Apple's defense of its deal, and the publisher antitrust suit consistent in focus? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. One report says Google paid $26.3 billion in 2021 to browsers, phones, and platforms. Apple's defense says there was no valid alternative to Google at the time. The publisher-suit report describes an allegation that Google siphons publishers' content, readers, and ad revenue."}
{"parent_question_id":"moi500d_qa_multihop_b508110c3615","question":"Are Fortune and the two TechCrunch trial reports consistent in their portrayal of the allegations against Sam Bankman-Fried? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. Fortune reports the government's claim that Bankman-Fried used Caroline Ellison as a front for secret access to customer money. One TechCrunch report says his trial concerned seven fraud and conspiracy counts. The other says the prosecution alleged fraud for wealth, power, and influence, while the defense said he acted in good faith."}
{"parent_question_id":"moi500d_qa_multihop_be1523ed3c9f","question":"Are TechCrunch's September 28 and December 19 reports consistent about OpenAI's platform strategy? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. The September report says GPT-4 with vision would become available alongside the GPT-4 Turbo API launch. The December report predicts that OpenAI would strongly promote an app store for AI tools and toys."}
{"parent_question_id":"moi500d_qa_multihop_a79ad9d2d8e8","question":"Are TechCrunch's two trial reports and The Verge's management account consistent in focus on Sam Bankman-Fried's role? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. One TechCrunch report says Ellison testified that she took $14 billion from customers under Bankman-Fried's instruction. The Verge reports that he said FTX's growth left him unable to run both companies. The other TechCrunch report says the prosecution alleged fraud to obtain wealth, power, and influence, while the defense said he acted in good faith."}
{"parent_question_id":"moi500d_qa_multihop_b911dd48e08e","question":"Are TechCrunch's publisher-antitrust and YouTube-safeguards reports consistent in their portrayal of Google's conduct? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. The publisher-antitrust report describes an allegation that Google siphons publishers' content, readers, and ad revenue through anticompetitive means. The safeguards report says Google planned no additional YouTube measures over the next six months."}
{"parent_question_id":"moi500d_qa_multihop_56cd21e33b60","question":"Are TechCrunch and Music Business Worldwide consistent about the European Commission's concerns? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in subject. TechCrunch says earlier Commission actions focused on illegal content and disinformation related to the Israel-Hamas war. Music Business Worldwide says the Commission acknowledged concerns about a court ruling's impact and intended to seek a balanced solution."}
{"parent_question_id":"moi500d_qa_multihop_4420123d1eda","question":"Are The New York Times, The Guardian, and Sky Sports consistent in what they emphasize about Chelsea? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. The New York Times says expanding Chelsea's U.S. profile would make sense under Todd Boehly's co-ownership. The Guardian reports a second consecutive home league defeat and another hostile reaction from supporters. Sky Sports reports fitness checks for three players and a possible return from long-term injury."}
{"parent_question_id":"moi500d_qa_multihop_ea2158230144","question":"Are The Age and the two TechCrunch antitrust reports consistent in their portrayal of Google's business practices? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. The Age says a critic maintains that Google manipulates Search to maximize ad revenue. One TechCrunch report describes a publisher's allegation that Google siphons content, readers, and ad revenue. The other reports Epic's allegation that Google hid items from discovery and notes Google's response that it supplied extensive records to the court."}
{"parent_question_id":"moi500d_qa_multihop_87955cbf9718","question":"Are Sporting News's November 13 and November 27 accounts of the Vikings' offensive effectiveness consistent? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent. The November 13 report says Josh Dobbs was producing the same offensive results the Vikings had with Kirk Cousins. The November 27 report says the Bears' defense controlled the game and Minnesota could not get out of its own way."}
{"parent_question_id":"moi500d_qa_multihop_88776bb09a93","question":"Are The Sydney Morning Herald's October 1 and Fortune's October 6 reports consistent on the Fed's approach to inflation and interest rates? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. The Sydney Morning Herald says the Fed would base upcoming rate decisions on incoming economic data. Fortune says Fed officials were forced to raise rates aggressively to fight inflation after years of booming home prices."}
{"parent_question_id":"moi500d_qa_multihop_b952659cf22c","question":"Are Fortune's October 4 and the later TechCrunch account consistent in their portrayal of Sam Bankman-Fried amid the fraud allegations? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. Fortune says Bankman-Fried recruited Adam Yedidia to Alameda and then FTX, and reports the government's claim that he used Caroline Ellison as a front for secret access to customer money. TechCrunch says the prosecution alleged knowing fraud for wealth, power, and influence, while the defense said he acted in good faith."}
{"parent_question_id":"moi500d_qa_multihop_bf572428ed7e","question":"Are the two Verge articles consistent in what they say about Google Search? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. The article on Apple's search deal quotes Cue saying there was no valid alternative to Google at the time. The other says Sullivan was frustrated that the public and media did not understand what he considered basic principles of how search works."}
{"parent_question_id":"moi500d_qa_multihop_9987f1137151","question":"Are the three Sporting News reports on Inter Miami after Lionel Messi's arrival consistent in focus? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. The season-ticket report describes intense fan demand after Messi's arrival. The playoff report says Inter Miami were officially out of postseason contention. The tour report says the club scheduled two matches in China after the regular season, likely to keep Messi fit and generate revenue."}
{"parent_question_id":"moi500d_qa_multihop_0ae53c638915","question":"Are The Age's October 20 and November 3 reports consistent about Richelle Cranston's AFLW career? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. The October report says the Bulldogs were Cranston's third club after Melbourne and Geelong. The November report identifies her as a Bulldogs forward who had played the season while battling stage five chronic kidney disease and was among the players farewelled."}
{"parent_question_id":"moi500d_qa_multihop_a641a8807544","question":"Are CNBC and The Sydney Morning Herald consistent in their portrayal of Amazon's impact? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in perspective. CNBC quotes a user calling selling on Amazon a life-changing opportunity. The Sydney Morning Herald says Amazon fell 4 percent after the Federal Trade Commission and 17 state attorneys general filed an antitrust lawsuit."}
{"parent_question_id":"moi500d_qa_multihop_2f28efdb567d","question":"Are the three TechCrunch reports on Sam Bankman-Fried's wealth, customer funds, and fraud allegations consistent in portrayal? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in portrayal. One report says Bankman-Fried was the richest person under 30 and intended to spend his wealth to save humanity from extinction. Another says Ellison testified that she took $14 billion from customers under his instruction. A third says the prosecution alleged fraud for wealth, power, and influence, while the defense said he acted in good faith."}
{"parent_question_id":"moi500d_qa_multihop_183f9fcb58c0","question":"Are Polygon and Engadget consistent in what they report about Steam Deck OLED availability and updates? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. Polygon says Valve had made about 300 hardware updates since the original Steam Deck launched. Engadget says the OLED model would go on sale on November 16 at 1 p.m. ET, with units ready to ship that day."}
{"parent_question_id":"moi500d_qa_multihop_91036ab4e5ad","question":"Are the three TechCrunch reports from October and November consistent in their portrayal of Sam Altman's professional role? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. The October roundup says Altman backed a teen's AI startup. Another report says ChatGPT's rise made him a public face of generative AI. The later analysis says a prevailing theory was inferred from the board's language."}
{"parent_question_id":"moi500d_qa_multihop_fe88e2f477ad","question":"Are the three Roar reports consistent in their portrayal of the All Blacks' performance and motivation? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. One report notes the Springboks led 12-3 after the All Blacks again failed to release on their goal line. Another recalls Argentina's wins over the All Blacks in the prior year and in 2020. The third says the All Blacks appeared to be playing for themselves and for departing players and leaders."}
{"parent_question_id":"moi500d_qa_multihop_caefcb4afda7","question":"Are Sporting News and CBS Sports consistent about factors behind the Vikings' turnaround and defensive effectiveness? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. Sporting News says the cited player's high-level performance was instrumental in the Vikings' turnaround after a 1-4 start. CBS Sports says opponents had a low average depth of target against the Vikings' defense and no tight-end completions of 20 or more yards."}
{"parent_question_id":"moi500d_qa_multihop_dc27f797801e","question":"Are The New York Times and Sky Sports consistent in what they emphasize about Chelsea? Give the conclusion first, then one key fact from each.","reference_answer":"Inconsistent in focus. The New York Times says expanding Chelsea's U.S. profile would make sense under Todd Boehly's co-ownership. Sky Sports reports late fitness checks for three players and a possible return from long-term injury for Benoit Badiashile."}
{"parent_question_id":"moi500d_qa_multihop_0a6a4f8996c3","question":"Are Fortune and CNBC consistent about risks to FTX customer funds through Alameda Research? Give the conclusion first, then one key fact from each.","reference_answer":"Consistent. Fortune reports the government's claim that Bankman-Fried used Caroline Ellison as a front for secret access to customer money. CNBC reports concern that a large amount of FTX customer money was at risk."}
'''.strip().splitlines()
    )
}

GENERATED_SPECS = [
    json.loads(line)
    for line in r'''
{"source_dataset":"enterprise","question_type":"conflicting_info","question":"Under Redwood's 2025 cross-account GPU warm-pool handoff runbook, what capacity-sentry conditions must both hold for three minutes before an automatic handoff?","reference_answer":"Use the tightened 2025 trigger: the 90th-percentile queue time must exceed 2.0 seconds for three minutes and warm-pool saturation must exceed 95% for three minutes. The older runbook used a 2.5-second queue threshold and did not quantify saturation.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_a71c974496a8","moi500d_doc_enterprise_aae146313a79"],"gold_evidence":[{"doc_id":"moi500d_doc_enterprise_a71c974496a8","evidence":"- Automatic trigger: capacity-sentry signals a sustained 90th-percentile queue time > 2.5s and warm-pool saturation for 3 minutes."},{"doc_id":"moi500d_doc_enterprise_aae146313a79","evidence":"- Automatic trigger: capacity-sentry signals BOTH:\n  - sustained 90th-percentile queue time > 2.0s for 3 minutes (was 2.5s)\n  - warm-pool saturation > 95% for 3 minutes"}]}
{"source_dataset":"enterprise","question_type":"conflicting_info","question":"During a 2025 Redwood cross-account GPU warm-pool handoff, which IAM role and route-override mechanism are preferred, and when may the older role and direct API still be used?","reference_answer":"Prefer the IAM role redwood/gpu-warm-pool-handoff and apply the route override through router-admin. The older redwood/warm-pool-handoff role is a fallback for legacy accounts, and direct POST /router/configs/apply is allowed only where router-admin is unavailable.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_a71c974496a8","moi500d_doc_enterprise_aae146313a79"],"gold_evidence":[{"doc_id":"moi500d_doc_enterprise_a71c974496a8","evidence":"- Validate the target account has the cross-account role configured: role name \"redwood/warm-pool-handoff\"."},{"doc_id":"moi500d_doc_enterprise_a71c974496a8","evidence":"- Update router sidecar config: POST /router/configs/apply with route override: route { tenant: \"TENANT_ID\", pool: \"target-account-pool\" }"},{"doc_id":"moi500d_doc_enterprise_aae146313a79","evidence":"  - Preferred role name: \"redwood/gpu-warm-pool-handoff\"\n  - Legacy role name (some accounts only): \"redwood/warm-pool-handoff\""},{"doc_id":"moi500d_doc_enterprise_aae146313a79","evidence":"- Apply router override via router-admin (preferred): router-admin override set --tenant TENANT_ID --pool target-account-pool --ttl 30m\n- Legacy (only if router-admin is unavailable in the region): POST /router/configs/apply with route override: route { tenant: \"TENANT_ID\", pool: \"target-account-pool\" }"}]}
{"source_dataset":"enterprise","question_type":"conflicting_info","question":"For a 4 GB inter-region KV-cache seed in Redwood's 2025 cross-account GPU warm-pool handoff, what transfer target and stall threshold should oncall use, and what is the retry and fallback sequence?","reference_answer":"The current target is for 90% of 4 GB transfers to finish in under 75 seconds. Treat a transfer as stalled after 150 seconds, reduce concurrency to four shards, retry once, and abort to DNS-level fallback if failures repeat. The older targets were under 90 seconds and a 180-second stall threshold.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_a71c974496a8","moi500d_doc_enterprise_aae146313a79"],"gold_evidence":[{"doc_id":"moi500d_doc_enterprise_a71c974496a8","evidence":"- Expected durations: 90% < 90s for a 4GB seed over inter-region link (with typical egress-shim compression enabled)."},{"doc_id":"moi500d_doc_enterprise_a71c974496a8","evidence":"- If transfer stalls > 180s: reduce concurrent shards (--shards 4) and retry. If repeated failures, abort and fall back to DNS-level routing (see Fallback options)."},{"doc_id":"moi500d_doc_enterprise_aae146313a79","evidence":"- Expected durations:\n  - 90% < 75s for a 4GB seed over inter-region link (with compression enabled)"},{"doc_id":"moi500d_doc_enterprise_aae146313a79","evidence":"- If transfer stalls > 150s (was 180s):\n  1. reduce concurrent shards (--shards 4)\n  2. retry once\n  3. if repeated failures, abort and fall back to DNS-level routing (see Fallback options)."}]}
{"source_dataset":"enterprise","question_type":"conflicting_info","question":"For Copperfield Nimbus Solutions' current Dedicated sizing plan, which chat latency targets supersede the earlier workshop values, and from where are they measured?","reference_answer":"Use the later targets of p50 below 180 ms and p95 below 450 ms under peak chat concurrency, measured from Copperfield's application server. The earlier p50-below-150-ms and p95-below-400-ms targets were subsequently relaxed.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_1607c4759fd5","moi500d_doc_enterprise_09e8243ebead"],"gold_evidence":[{"doc_id":"moi500d_doc_enterprise_1607c4759fd5","evidence":"Core asks: predictable p50/p95 latency for chat (target p50 <150ms, p95 <400ms), embeddings throughput for nightly index jobs, and a routing policy that falls back to a smaller model for non-critical requests."},{"doc_id":"moi500d_doc_enterprise_09e8243ebead","evidence":"- They walked back the strictest latency ask slightly for p50, but are more focused on consistent p95 during peak chat concurrency."},{"doc_id":"moi500d_doc_enterprise_09e8243ebead","evidence":"- Chat: p50 < 180ms, p95 < 450ms under peak concurrency (measured from their app server)"}]}
{"source_dataset":"enterprise","question_type":"conflicting_info","question":"Which token mix, average per-request token count, and monthly volume forecast should Copperfield Nimbus Solutions' current Dedicated sizing worksheet use instead of the earlier assumptions?","reference_answer":"Use the updated mix of approximately 55% short, 35% medium, and 10% long requests; about 200 request tokens plus 160 response tokens, or 360 combined tokens per request; and a base forecast of about 1.5 billion tokens per month with up to 3.0 billion during launch months. The earlier assumptions were 60/30/10, 320 combined tokens, and 1.2 billion with 2.5 billion burst headroom.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_1607c4759fd5","moi500d_doc_enterprise_09e8243ebead"],"gold_evidence":[{"doc_id":"moi500d_doc_enterprise_1607c4759fd5","evidence":"- Token mix (measured during Hosted trial): ~60% short requests (<=64 tokens), ~30% medium (64-512 tokens), ~10% long (>512 tokens). Average request tokens ~180; average response tokens ~140; combined per-request tokens ~320."},{"doc_id":"moi500d_doc_enterprise_1607c4759fd5","evidence":"- Monthly token forecast (conservative): 1.2B tokens/mo with burst headroom to 2.5B/mo."},{"doc_id":"moi500d_doc_enterprise_09e8243ebead","evidence":"- Planning for ~150 concurrent chat sessions at peak (KV cache active) Token mix (from Hosted trial + updated estimate):\n- ~55% short (<=64 tokens)\n- ~35% medium (64–512 tokens)\n- ~10% long (>512 tokens) Average tokens/request now modeled as:\n- avg request tokens ~200\n- avg response tokens ~160\n- combined per-request tokens ~360 Monthly token forecast:\n- Base case: ~1.5B tokens/month\n- Burst/headroom planning: up to ~3.0B tokens/month during launch months"}]}
{"source_dataset":"enterprise","question_type":"conflicting_info","question":"For the NVIDIA H200 80GB Dedicated launch in eu-central-1 and ap-south-1, should Sales enforce an initial soft quota of 48 or 64 GPUs per organization?","reference_answer":"The corpus does not establish a single authoritative quota: one announcement says 48 GPUs per organization and another says 64, both across the two launch regions for the first 30 days. Sales should verify the current capacity runbook or ask capacity@redwood. Both announcements require approval-queue review for larger reservations.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_15e0877eafb0","moi500d_doc_enterprise_c1a77d876a32"],"gold_evidence":[{"doc_id":"moi500d_doc_enterprise_15e0877eafb0","evidence":"jess (platform): Soft cap = 48 GPUs per org across the two regions for first 30 days. Escalate to capacity@redwood for exceptions. Approval queue will gate large reservations."},{"doc_id":"moi500d_doc_enterprise_c1a77d876a32","evidence":"sara (platform): initial soft quota: 64 GPUs per org across both regions. For larger needs, escalate to capacity@redwood with expected commit size and duration. We'll enforce by reservation approval queue for first 30 days."}]}
{"source_dataset":"enterprise","question_type":"conflicting_info","question":"For the NVIDIA H200 80GB Dedicated launch, which GPU-utilization, temperature, and memory-error metric names should operators place on the launch dashboards?","reference_answer":"The corpus cannot determine the canonical metric names. One announcement lists model_gpu_util_h200, h200_gpu_temp_c, and h200_memory_errors; the other lists model_inference_gpu_util_h200, h200_temp_celsius, and h200_oom_events. Operators should verify the current metrics registry or H200 runbook. The announcements agree only on a default alert when p95 GPU utilization exceeds 85% for 10 minutes.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_15e0877eafb0","moi500d_doc_enterprise_c1a77d876a32"],"gold_evidence":[{"doc_id":"moi500d_doc_enterprise_15e0877eafb0","evidence":"omar (infra): monitoring hooks added: metric names model_gpu_util_h200, h200_gpu_temp_c, h200_memory_errors. Default alert: P95 util >85% for 10m."},{"doc_id":"moi500d_doc_enterprise_c1a77d876a32","evidence":"max (infra): monitoring hooks added to console metrics: model_inference_gpu_util_h200, h200_temp_celsius, h200_oom_events. Alerting default: P95 GPU util > 85% for 10m."}]}
{"source_dataset":"enterprise","question_type":"conflicting_info","question":"After Redwood's Friday all-hands mentioned parental-leave changes, what entitlement should US managers apply now: the proposal or the existing policy?","reference_answer":"Managers should continue applying the documented US policy for full-time employees: 16 weeks for primary caregivers and 8 weeks for secondary caregivers. The proposed changes were only moving to executive review, and People Ops said no policy would change without formal communication.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_5bbf9c84fa6c","moi500d_doc_enterprise_cc370d1465d8"],"gold_evidence":[{"doc_id":"moi500d_doc_enterprise_5bbf9c84fa6c","evidence":"Maya (People Ops): Good Q Juno — yes, ops will include a summary of benefits updates and next steps. No policy will change without formal comms."},{"doc_id":"moi500d_doc_enterprise_5bbf9c84fa6c","evidence":"5) Benefits: parental leave proposal moving to executive review (Maya/Talent)."},{"doc_id":"moi500d_doc_enterprise_cc370d1465d8","evidence":"<tr><td>Parental leave</td><td>Full-time</td><td>16 weeks primary, 8 weeks secondary (US policy)</td></tr>"}]}
{"source_dataset":"enterprise","question_type":"refusal","question":"According to the \"Ten-Step Restoration Sprint & Shadow Mentoring Handbook\", what exact Zoom or Google Meet URL and meeting passcode must the Incident Initiator use for the incident bridge?","reference_answer":"Not specified in the named handbook. It says to create a bridge using Zoom/Google Meet, but gives no fixed meeting URL or passcode.","answerable":false,"gold_doc_ids":["moi500d_doc_enterprise_0dd7f414c29f"],"gold_evidence":[]}
{"source_dataset":"enterprise","question_type":"refusal","question":"According to the \"Employee Lifecycle Operating Manual\", what exact monthly monetary amount is provided for the wellness stipend?","reference_answer":"Not specified in the named manual. It lists a monthly wellness stipend as a local-currency equivalent but gives no amount.","answerable":false,"gold_doc_ids":["moi500d_doc_enterprise_cc370d1465d8"],"gold_evidence":[]}
{"source_dataset":"enterprise","question_type":"refusal","question":"In the email thread \"CartPilot trial — POC scope & quick sync\", what exact budget did CartPilot approve for the 4–6 week POC?","reference_answer":"Not specified in the named email thread. It gives POC scope, volume, timing, and latency/cost discussion, but no approved budget.","answerable":false,"gold_doc_ids":["moi500d_doc_enterprise_cf639bb9c421"],"gold_evidence":[]}
{"source_dataset":"enterprise","question_type":"basic","question":"In the \"announcements\" Slack thread introducing the NVIDIA H200 80GB (h200-80) for Dedicated, what throughput improvement did microbenchmarks show over H100 for long-context models, and how long were warm-up P99 blips expected?","reference_answer":"About 1.5–1.7× H100 throughput, with warm-up P99 blips expected for the first 6–12 hours.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_15e0877eafb0"],"gold_evidence":["omar (infra): perf summary: in our microbenchmarks h200-80 gives ~1.5-1.7x throughput vs our h100 baseline for long-context models. Expect the usual warm-up P99 blips for first 6-12 hours as caches warm."]}
{"source_dataset":"enterprise","question_type":"basic","question":"In the GitHub item \"Cross-SDK: unify streaming handshake, consumer ergonomics, and auth/retry semantics (Python/TS/Go)\", which SDK versions were bumped for Python, TypeScript, and Go?","reference_answer":"Python 0.12.0, TypeScript 1.9.0, and Go 0.8.3.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_16794acef282"],"gold_evidence":["- Version bumps: python 0.12.0, typescript 1.9.0, go 0.8.3."]}
{"source_dataset":"enterprise","question_type":"basic","question":"According to \"Optimize 1.3 release notes — Performance Profiles & Cost Telemetry\", what p95 latency thresholds define the Console presets S0, S1, and S2?","reference_answer":"S0: <50 ms p95; S1: <120 ms p95; S2: <250 ms p95.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_e9fe15b06050"],"gold_evidence":["- Latency-tier Presets in Console: new predefined profiles (S0: <50ms p95, S1: <120ms p95, S2: <250ms p95) that automatically tune batching and cache TTL recommendations."]}
{"source_dataset":"enterprise","question_type":"basic","question":"In the #eng-runtime prefetch/regression nsys discussion, which kernel ranked first by total GPU time, and what GPU-time share and SM utilization were reported?","reference_answer":"attention_fwd_kernel_v3; 42% of total GPU time; 78% SM utilization.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_b1e69fd04dc4"],"gold_evidence":["cheng: quick share - ran nsys on the prefetch/regression job, seeing weird skew in kernel time. top 6 by total GPU time:","1. attention_fwd_kernel_v3 - 42% (SM util 78%)"]}
{"source_dataset":"enterprise","question_type":"basic","question":"For the NorthPoint Signalworks four-week Dedicated POC, which regions were designated primary and secondary, and what failover behavior was preferred over throttling?","reference_answer":"Primary: us-east-1; secondary: eu-west-1; the preferred behavior was region failover using warm standby rather than throttling.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_91b616fd5165"],"gold_evidence":["Routing plan agreed for POC: primary region = us-east-1, secondary = eu-west-1. Failover preference = region failover (warm standby) over throttling."]}
{"source_dataset":"enterprise","question_type":"basic","question":"In the Friday all-hands thread whose agenda is dated 2028-03-05, when was the rollout canary feature due to enter beta, and where were signups offered?","reference_answer":"It was due to enter beta the following week, with signups offered through the Console.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_5bbf9c84fa6c"],"gold_evidence":["2) Lina: rollout canary feature out in beta next week; signups via the Console."]}
{"source_dataset":"enterprise","question_type":"basic","question":"In \"Modernize CI by replacing legacy bash pipelines with composite actions and plugin hooks\", how long were the new composites intended to coexist with legacy workflows during canary, and how could a repository roll back?","reference_answer":"They were intended to coexist for a two-week canary; rollback was done by switching the workflow reference back to the legacy scripts.","answerable":true,"gold_doc_ids":["moi500d_doc_enterprise_97325ec30dea"],"gold_evidence":["The new composites are backward-compatible by default and live alongside legacy workflows during a 2-week canary.","Rollback is as simple as switching the workflow reference back to legacy scripts if an issue is detected."]}
{"source_dataset":"docbench","question_type":"structured_extraction","question":"From Costco’s fiscal 2022 shareholder letter, extract net sales, net sales growth, comparable sales growth, net income, diluted EPS, net income growth, membership-fee revenue, and membership-fee revenue growth. Return one JSON object using those field names.","reference_answer":"{\"net_sales\":\"$222.7 billion\",\"net_sales_growth\":\"16%\",\"comparable_sales_growth\":\"14%\",\"net_income\":\"$5.8 billion\",\"diluted_eps\":\"$13.14\",\"net_income_growth\":\"17%\",\"membership_fee_revenue\":\"$4.2 billion\",\"membership_fee_revenue_growth\":\"9%\"}","answerable":true,"gold_doc_ids":["moi500d_doc_docbench_011bc43f84f7"],"gold_evidence":["We had strong operating results in fiscal 2022. Net sales for the 52-week fiscal year totaled \\$222.7 billion, an increase of 16%, with a comparable sales increase of 14%. Net income for the 52-week fiscal year was \\$5.8 billion, or \\$13.14 per diluted share, an increase of 17%. Revenue from membership fees increased 9% to \\$4.2 billion."]}
{"source_dataset":"docbench","question_type":"structured_extraction","question":"From P&G’s \"FINANCIAL HIGHLIGHTS (UNAUDITED)\" table, extract the 2022 Net Sales, Operating Income, and Net Earnings Attributable to P&G. Return one JSON object and preserve the table’s units.","reference_answer":"{\"units\":\"billions\",\"net_sales\":\"$80.2\",\"operating_income\":\"$17.8\",\"net_earnings_attributable_to_pg\":\"$14.7\"}","answerable":true,"gold_doc_ids":["moi500d_doc_docbench_018b6c668b28"],"gold_evidence":["FINANCIAL HIGHLIGHTS (UNAUDITED) Amounts in billions, except per share amounts","<tr><td>Net Sales</td><td>$80.2</td><td>$76.1</td><td>$71.0</td><td>$67.7</td><td>$66.8</td></tr>","<tr><td>Operating Income</td><td>$17.8</td><td>$18.0</td><td>$15.7</td><td>$5.5</td><td>$13.4</td></tr>","<tr><td>Net Earnings Attributable to P&amp;G</td><td>$14.7</td><td>$14.3</td><td>$13.0</td><td>$3.9</td><td>$9.8</td></tr>"]}
{"source_dataset":"docbench","question_type":"structured_extraction","question":"From PepsiCo’s 2020 \"Summary of Operations\" table, extract the 2020 value and % change for Core operating profit, Reported earnings per share, and Free cash flow. Return structured JSON and preserve the displayed values and table units.","reference_answer":"{\"table_units\":\"in millions, except per share data; all per share amounts assume dilution\",\"rows\":[{\"metric\":\"Core operating profit(b)\",\"value_2020\":\"$10,531\",\"pct_change\":\"(1)%\"},{\"metric\":\"Reported earnings per share\",\"value_2020\":\"$5.12\",\"pct_change\":\"(2)%\"},{\"metric\":\"Free cash flow(d)\",\"value_2020\":\"$6,428\",\"pct_change\":\"15%\"}]}","answerable":true,"gold_doc_ids":["moi500d_doc_docbench_4cf8337c7378"],"gold_evidence":["PepsiCo, Inc. & Consolidated Subsidiaries (in millions, except per share data; all per share amounts assume dilution)","<tr><td>Core operating profit(b)</td><td>$10,531</td><td>$10,602</td><td>(1)%</td></tr>","<tr><td>Reported earnings per share</td><td>$5.12</td><td>$5.20</td><td>(2)%</td></tr>","<tr><td>Free cash flow(d)</td><td>$6,428</td><td>$5,587</td><td>15%</td></tr>"]}
'''.strip().splitlines()
]

LONG_SPEC_FILE = Path(__file__).with_name("data") / "moi_rag_bench_v0_3_1_constraint_completeness.jsonl"
EXPECTED_SOURCE_COUNTS = {"docbench": 110, "enterprise": 70, "multihop": 95}
EXPECTED_CAPABILITY_COUNTS = {
    "QA": 115,
    "multi-hop": 85,
    "refusal": 30,
    "structured/meta": 10,
    "constraint": 10,
    "conflict": 10,
    "completeness": 15,
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"Expected JSON objects: {path}")
    return rows


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def normalized_evidence(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\\$", "$")).strip()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def tree_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def evidence_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        for key in ("evidence", "text", "quote", "content", "snippet", "passage"):
            if item.get(key):
                return str(item[key])
    return ""


def capability(row: Mapping[str, Any]) -> str:
    question_type = str(row.get("question_type") or "")
    if not row.get("answerable") or question_type in {"unanswerable", "null_query", "refusal"}:
        return "refusal"
    if row.get("source_dataset") == "multihop":
        return "multi-hop"
    if question_type in {"meta-data", "structured_extraction"}:
        return "structured/meta"
    if question_type == "constrained":
        return "constraint"
    if question_type == "conflicting_info":
        return "conflict"
    if question_type == "completeness":
        return "completeness"
    return "QA"


def revised_id(source_dataset: str, parent_id: str | None, question: str) -> str:
    payload = f"{DATASET_ID}|{source_dataset}|{parent_id or 'constructed'}|{normalized(question)}"
    return f"moi031_qa_{source_dataset}_{hashlib.sha256(payload.encode()).hexdigest()[:12]}"


def generated_specs() -> list[dict[str, Any]]:
    if not LONG_SPEC_FILE.is_file():
        raise RuntimeError(f"Missing curated QA specification: {LONG_SPEC_FILE}")
    specs = [copy.deepcopy(row) for row in GENERATED_SPECS] + read_jsonl(LONG_SPEC_FILE)
    if len(specs) != 33:
        raise RuntimeError(f"Expected 33 constructed QA rows, got {len(specs)}")
    return specs


def source_bank_review(current_ids: set[str], frozen_doc_ids: set[str]) -> dict[str, Any]:
    eligible = [
        row
        for row in read_jsonl(SOURCE_BANK / "questions.jsonl")
        if row.get("question_id") not in current_ids and set(row.get("gold_doc_ids") or []) <= frozen_doc_ids
    ]
    matching = [
        row
        for row in eligible
        if (row.get("source_dataset") == "docbench" and row.get("question_type") in {"meta-data", "structured_extraction"})
        or (
            row.get("source_dataset") == "enterprise"
            and row.get("question_type") in {"basic", "conflicting_info", "constrained", "completeness", "refusal"}
        )
    ]
    return {
        "policy": "reuse an eligible frozen-corpus question before constructing a new question",
        "eligible_excluded_bank_rows_by_source": dict(sorted(Counter(str(row.get("source_dataset")) for row in eligible).items())),
        "eligible_rows_matching_requested_addition_types": len(matching),
        "reviewed_duplicate": {
            "question_id": "moi500d_qa_enterprise_da305fca3ebf",
            "disposition": "rejected",
            "reason": "same Arkadia savings-reconciliation issue as retained moi500d_qa_enterprise_790c13d7307a",
        },
        "construction_reason": "The original bank had no eligible DocBench structured rows or Enterprise rows in the requested addition types after frozen-corpus and duplicate checks.",
    }


def resolved_generated_evidence(spec: Mapping[str, Any], corpus_by_id: Mapping[str, Mapping[str, Any]]) -> list[Any]:
    """Split a curated non-contiguous quote into exact source lines before emission."""

    result: list[Any] = []
    gold_doc_ids = list(spec.get("gold_doc_ids") or [])
    texts = {
        doc_id: (SOURCE_PACKAGE / str(corpus_by_id[doc_id]["text_path"])).read_text(encoding="utf-8", errors="replace")
        for doc_id in gold_doc_ids
    }
    for item in spec.get("gold_evidence") or []:
        text = evidence_text(item)
        item_doc = str(item.get("doc_id")) if isinstance(item, Mapping) and item.get("doc_id") else None
        candidate_ids = [item_doc] if item_doc else gold_doc_ids
        if any(normalized_evidence(text) in normalized_evidence(texts[doc_id]) for doc_id in candidate_ids):
            result.append(copy.deepcopy(item))
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        split_rows: list[dict[str, str]] = []
        for line in lines:
            matched_doc = next(
                (doc_id for doc_id in candidate_ids if normalized_evidence(line) in normalized_evidence(texts[doc_id])),
                None,
            )
            if not matched_doc:
                split_rows = []
                break
            split_rows.append({"doc_id": matched_doc, "evidence": line})
        result.extend(split_rows or [copy.deepcopy(item)])
    return result


def make_generated_question(spec: Mapping[str, Any], corpus_by_id: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source_dataset = str(spec["source_dataset"])
    question = str(spec["question"])
    reference_answer = str(spec["reference_answer"])
    question_id = revised_id(source_dataset, None, question)
    source_question_id = f"{DATASET_ID}:constructed:{question_id.rsplit('_', 1)[-1]}"
    doc_ids = list(spec.get("gold_doc_ids") or [])
    evidence = resolved_generated_evidence(spec, corpus_by_id)
    question_type = str(spec["question_type"])
    answerable = bool(spec.get("answerable"))
    metadata = {
        "dataset": "MOI RAG Benchmark",
        "scope_policy": "global",
        "qa_revision": REVISION,
        "qa_origin": "constructed_from_frozen_corpus",
        "benchmark_dimension": capability({"source_dataset": source_dataset, "question_type": question_type, "answerable": answerable}),
        "source_bank_priority_checked": True,
    }
    question_row = {
        "answer": reference_answer,
        "answerable": answerable,
        "document_ids": [],
        "gold": {"document_ids": doc_ids, "evidence": evidence},
        "gold_doc_ids": doc_ids,
        "gold_evidence": evidence,
        "metadata": metadata,
        "question": question,
        "question_id": question_id,
        "question_type": question_type,
        "reference_answer": reference_answer,
        "scope_doc_ids": [],
        "source_dataset": source_dataset,
        "source_question_id": source_question_id,
    }
    source_doc_ids = [str((corpus_by_id[doc_id].get("metadata") or {}).get("source_document_id") or doc_id) for doc_id in doc_ids]
    gold_row = {
        "gold": {"document_ids": doc_ids, "evidence": evidence},
        "gold_doc_ids": doc_ids,
        "gold_evidence": evidence,
        "question_id": question_id,
        "reference_answer": reference_answer,
        "source_dataset": source_dataset,
        "source_gold": {
            "gold_doc_ids": source_doc_ids,
            "gold_evidence": evidence,
            "metadata": {"source_question_id": source_question_id},
            "question_id": source_question_id,
            "reference_answer": reference_answer,
            "scope_doc_ids": source_doc_ids,
        },
        "source_gold_doc_ids": source_doc_ids,
        "source_question_id": source_question_id,
    }
    lineage = {
        "question_id": question_id,
        "parent_question_id": None,
        "source_question_id": source_question_id,
        "source_dataset": source_dataset,
        "origin": "constructed_from_frozen_corpus",
        "change_kind": "added",
        "question_type": question_type,
        "benchmark_dimension": metadata["benchmark_dimension"],
        "gold_doc_ids": doc_ids,
    }
    return question_row, gold_row, lineage


def build_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    source_questions = read_jsonl(SOURCE_PACKAGE / "questions.jsonl")
    source_gold = {row["question_id"]: row for row in read_jsonl(SOURCE_PACKAGE / "gold.jsonl")}
    corpus = read_jsonl(SOURCE_PACKAGE / "corpus.jsonl")
    corpus_by_id = {row["doc_id"]: row for row in corpus}
    source_ids = {row["question_id"] for row in source_questions}
    if not REMOVE_IDS <= source_ids:
        raise RuntimeError(f"Unknown removal IDs: {sorted(REMOVE_IDS - source_ids)}")
    if not set(TYPE_OVERRIDES) <= source_ids or not set(ROW_REWRITES) <= source_ids or not set(MULTIHOP_REWRITES) <= source_ids:
        raise RuntimeError("A rewrite/reclassification parent is missing from v0.3-final")
    if REMOVE_IDS & (set(TYPE_OVERRIDES) | set(ROW_REWRITES) | set(MULTIHOP_REWRITES)):
        raise RuntimeError("A removed row is also scheduled for revision")

    questions: list[dict[str, Any]] = []
    gold_rows: list[dict[str, Any]] = []
    lineage_rows: list[dict[str, Any]] = []
    removed_rows: list[dict[str, Any]] = []
    for source_row in source_questions:
        parent_id = str(source_row["question_id"])
        if parent_id in REMOVE_IDS:
            removed_rows.append(
                {
                    "question_id": parent_id,
                    "source_dataset": source_row.get("source_dataset"),
                    "question_type": source_row.get("question_type"),
                    "reason": "defective_or_redundant_qa_reduction" if source_row.get("source_dataset") == "docbench" else "repeated_gold_set_or_artificial_comparison",
                }
            )
            continue

        row = copy.deepcopy(source_row)
        changes: list[str] = []
        if parent_id in TYPE_OVERRIDES:
            row["question_type"] = TYPE_OVERRIDES[parent_id]
            changes.append("question_type")
        rewrite = ROW_REWRITES.get(parent_id) or MULTIHOP_REWRITES.get(parent_id)
        if rewrite:
            for key in ("question", "reference_answer"):
                if rewrite.get(key) and rewrite[key] != row.get(key):
                    row[key] = rewrite[key]
                    changes.append(key)
        row["answer"] = row["reference_answer"]
        row["gold"] = {
            "document_ids": list(row.get("gold_doc_ids") or []),
            "evidence": copy.deepcopy(list(row.get("gold_evidence") or [])),
        }
        new_id = revised_id(str(row["source_dataset"]), parent_id, str(row["question"]))
        row["question_id"] = new_id
        metadata = copy.deepcopy(row.get("metadata") or {})
        metadata.update(
            {
                "qa_revision": REVISION,
                "qa_origin": "v0.3-final-question-bank",
                "parent_question_id": parent_id,
                "benchmark_dimension": capability(row),
            }
        )
        if row.get("source_dataset") == "docbench" and parent_id in TYPE_OVERRIDES:
            metadata["scope_policy"] = "global"
            metadata["scope_doc_ids"] = list(row.get("gold_doc_ids") or [])
            row["scope_doc_ids"] = list(row.get("gold_doc_ids") or [])
        row["metadata"] = metadata

        gold = copy.deepcopy(source_gold[parent_id])
        gold.update(
            {
                "question_id": new_id,
                "reference_answer": row["reference_answer"],
                "gold_doc_ids": list(row.get("gold_doc_ids") or []),
                "gold_evidence": copy.deepcopy(list(row.get("gold_evidence") or [])),
                "gold": copy.deepcopy(row["gold"]),
            }
        )
        questions.append(row)
        gold_rows.append(gold)
        lineage_rows.append(
            {
                "question_id": new_id,
                "parent_question_id": parent_id,
                "source_question_id": row.get("source_question_id"),
                "source_dataset": row.get("source_dataset"),
                "origin": "v0.3-final-question-bank",
                "change_kind": "rewritten" if changes else "reidentified",
                "changes": changes or ["question_id"],
                "question_type": row.get("question_type"),
                "benchmark_dimension": metadata["benchmark_dimension"],
                "gold_doc_ids": list(row.get("gold_doc_ids") or []),
            }
        )

    for spec in generated_specs():
        question, gold, lineage = make_generated_question(spec, corpus_by_id)
        questions.append(question)
        gold_rows.append(gold)
        lineage_rows.append(lineage)

    bank_review = source_bank_review(source_ids, set(corpus_by_id))
    return questions, gold_rows, lineage_rows, removed_rows, bank_review


def build_text_only_signoff(
    questions: list[dict[str, Any]], lineage_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    prior = {row["question_id"]: row for row in read_jsonl(SOURCE_MLLM_AUDIT)}
    result: list[dict[str, Any]] = []
    if len(questions) != len(lineage_rows):
        raise RuntimeError("Question/sign-off lineage count differs")
    for question, lineage in zip(questions, lineage_rows):
        parent_id = lineage.get("parent_question_id")
        if parent_id:
            parent = prior.get(parent_id)
            if not parent or parent.get("mllm_required"):
                raise RuntimeError(f"Missing safe parent MLLM sign-off: {parent_id}")
            provenance = "inherited_from_v0.3_final_via_parent_question_id"
            reason = "The parent QA was signed off as not requiring MLLM; the revised QA uses the same frozen text document(s) and introduces no visual request."
        else:
            provenance = "manual_text_evidence_review"
            reason = "The constructed QA was reviewed against exact Markdown evidence and contains no image, screenshot, diagram, or visual-inspection request."
        result.append(
            {
                "question_id": question["question_id"],
                "parent_question_id": parent_id,
                "question": question["question"],
                "question_type": question["question_type"],
                "source_dataset": question["source_dataset"],
                "gold_doc_ids": list(question.get("gold_doc_ids") or []),
                "status": "NOT_APPLICABLE",
                "mllm_required": False,
                "text_only_ready": True,
                "provenance": provenance,
                "decision": {"confidence": "high", "reason": reason},
            }
        )
    return result


def validate_rows(
    questions: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
    lineage_rows: list[dict[str, Any]],
    ready_output: Path,
) -> dict[str, Any]:
    corpus = read_jsonl(ready_output / "corpus.jsonl")
    corpus_by_id = {row["doc_id"]: row for row in corpus}
    question_ids = [row["question_id"] for row in questions]
    gold_ids = [row["question_id"] for row in gold_rows]
    normalized_questions = [normalized(row.get("question")) for row in questions]
    if not (len(questions) == len(gold_rows) == len(lineage_rows) == 275):
        raise RuntimeError("Expected exactly 275 aligned QA/gold/lineage rows")
    if question_ids != gold_ids or len(set(question_ids)) != 275:
        raise RuntimeError("Question/gold order or question ID uniqueness failed")
    if len(set(normalized_questions)) != 275:
        raise RuntimeError("Normalized duplicate questions remain")
    if any(doc_id not in corpus_by_id for row in questions for doc_id in row.get("gold_doc_ids") or []):
        raise RuntimeError("A QA row references a document outside the frozen corpus")

    source_counts = dict(sorted(Counter(str(row.get("source_dataset")) for row in questions).items()))
    capability_counts = dict(sorted(Counter(capability(row) for row in questions).items()))
    if source_counts != EXPECTED_SOURCE_COUNTS:
        raise RuntimeError(f"Source quotas differ: {source_counts}")
    if capability_counts != EXPECTED_CAPABILITY_COUNTS:
        raise RuntimeError(f"Capability quotas differ: {capability_counts}")

    document_text = {
        doc_id: (ready_output / str(row["text_path"])).read_text(encoding="utf-8", errors="replace")
        for doc_id, row in corpus_by_id.items()
    }
    constructed_ids = {row["question_id"] for row in lineage_rows if row["origin"] == "constructed_from_frozen_corpus"}
    visual_re = re.compile(r"\b(?:image|photo|photograph|screenshot|diagram|visual inspection|look at the figure)\b", re.IGNORECASE)
    for row in questions:
        if row["question_id"] not in constructed_ids:
            continue
        if visual_re.search(str(row.get("question") or "")):
            raise RuntimeError(f"Constructed QA has an unreviewed visual dependency: {row['question_id']}")
        evidence = list(row.get("gold_evidence") or [])
        if row.get("answerable") and not evidence:
            raise RuntimeError(f"Constructed answerable QA lacks evidence: {row['question_id']}")
        for item in evidence:
            snippet = normalized_evidence(evidence_text(item))
            item_doc = item.get("doc_id") if isinstance(item, Mapping) else None
            candidate_ids = [str(item_doc)] if item_doc else list(row.get("gold_doc_ids") or [])
            if item_doc and item_doc not in row.get("gold_doc_ids", []):
                raise RuntimeError(f"Evidence doc is outside gold_doc_ids: {row['question_id']}")
            if not snippet or not any(snippet in normalized_evidence(document_text[doc_id]) for doc_id in candidate_ids):
                raise RuntimeError(f"Constructed evidence does not resolve: {row['question_id']} :: {snippet[:100]}")

    typo_re = re.compile(r"\b(?:initalized|code-\s+searchnet|pril 2018)\b", re.IGNORECASE)
    if any(typo_re.search(f"{row.get('question', '')} {row.get('reference_answer', '')}") for row in questions):
        raise RuntimeError("Known typo/defective-gold pattern remains")

    source_documents = SOURCE_PACKAGE / "documents"
    target_documents = ready_output / "documents"
    if (SOURCE_PACKAGE / "corpus.jsonl").read_bytes() != (ready_output / "corpus.jsonl").read_bytes():
        raise RuntimeError("corpus.jsonl changed in a QA-only revision")
    if tree_digest(source_documents) != tree_digest(target_documents):
        raise RuntimeError("Frozen parsed-document tree changed")
    for row in corpus:
        target = ready_output / str(row["text_path"])
        if sha256_bytes(target.read_bytes()) != row["sha256"]:
            raise RuntimeError(f"Frozen document hash mismatch: {target}")

    return {
        "schema": "moi-rag-bench-v0.3.1-qa-validation-v1",
        "status": "PASS",
        "counts": {"documents": len(corpus), "questions": len(questions), "gold": len(gold_rows)},
        "source_counts": source_counts,
        "capability_counts": capability_counts,
        "question_gold_order_match": True,
        "normalized_question_duplicates": 0,
        "gold_document_closure": True,
        "constructed_evidence_resolved": True,
        "constructed_visual_dependency_rows": 0,
        "corpus_jsonl_byte_identical": True,
        "parsed_document_tree_byte_identical": True,
        "parsed_document_tree_sha256": tree_digest(target_documents),
    }


def materialize(output: Path) -> dict[str, Any]:
    if output.exists():
        raise RuntimeError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    questions, gold_rows, lineage_rows, removed_rows, bank_review = build_rows()
    text_only_signoff = build_text_only_signoff(questions, lineage_rows)
    source_manifest = read_json(SOURCE_PACKAGE / "manifest.json")

    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        stage = Path(temporary) / output.name
        ready = stage / "ready_for_eval"
        ready.mkdir(parents=True)
        shutil.copy2(SOURCE_PACKAGE / "corpus.jsonl", ready / "corpus.jsonl")
        shutil.copytree(SOURCE_PACKAGE / "documents", ready / "documents", copy_function=shutil.copy2)
        write_jsonl(ready / "questions.jsonl", questions)
        write_jsonl(ready / "gold.jsonl", gold_rows)
        write_jsonl(stage / "question-lineage.jsonl", lineage_rows)
        write_jsonl(stage / "text-only-signoff.jsonl", text_only_signoff)

        manifest = copy.deepcopy(source_manifest)
        manifest.update(
            {
                "dataset_id": DATASET_ID,
                "dataset_name": "MOI RAG Benchmark v0.3.1 QA revision over frozen v0.3 corpus",
                "dataset_revision": REVISION,
                "revision": REVISION,
                "benchmark_manifest": None,
                "source_manifest": "../../moi-rag-bench-v0.3-final-raw-corpus/provenance/documents.jsonl",
                "parent_package": "../../moi-rag-bench-v0.3-final-raw-corpus/ready_for_eval",
                "mllm_required": False,
                "multimodal": False,
                "image_llm": "NOT_APPLICABLE",
                "counts": {
                    "documents": 297,
                    "corpus_rows": 297,
                    "runner_documents": 297,
                    "questions": 275,
                    "question_rows": 275,
                    "gold_rows": 275,
                },
            }
        )
        manifest["conditions"] = {
            **dict(manifest.get("conditions") or {}),
            "qa_only_revision": True,
            "frozen_parent_corpus": True,
            "mllm_required": False,
            "note": "corpus.jsonl and ready_for_eval/documents are byte-identical to v0.3-final; only QA/gold/manifest changed.",
        }
        manifest["text_only_signoff"] = "../text-only-signoff.jsonl"
        write_json(ready / "manifest.json", manifest)

        validation = validate_rows(questions, gold_rows, lineage_rows, ready)
        summary = {
            "schema": "moi-rag-bench-v0.3.1-qa-revision-summary-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset_id": DATASET_ID,
            "revision": REVISION,
            "parent_dataset_id": source_manifest.get("dataset_id"),
            "documents_changed": 0,
            "parsed_documents_changed": 0,
            "questions_removed": len(removed_rows),
            "questions_added": sum(row["origin"] == "constructed_from_frozen_corpus" for row in lineage_rows),
            "questions_rewritten_or_reclassified": sum(row["change_kind"] == "rewritten" for row in lineage_rows),
            "text_only_signoff_rows": len(text_only_signoff),
            "mllm_calls_performed": 0,
            "source_counts": validation["source_counts"],
            "capability_counts": validation["capability_counts"],
            "removed_questions": removed_rows,
            "source_bank_priority": bank_review,
            "document_invariants": {
                "corpus_jsonl_byte_identical": True,
                "parsed_document_tree_byte_identical": True,
                "parsed_document_tree_sha256": validation["parsed_document_tree_sha256"],
            },
        }
        write_json(stage / "qa-revision-summary.json", summary)
        write_json(stage / "validation.json", validation)
        (stage / "README.md").write_text(
            "# MOI RAG Benchmark v0.3.1 QA revision\n\n"
            "This is a QA-only revision of v0.3-final. `ready_for_eval/corpus.jsonl` and all 297 Markdown documents are byte-identical to the parent package.\n\n"
            "- Documents: 297 (frozen)\n"
            "- QA / Gold: 275 / 275\n"
            "- Added QA: 33; removed QA: 33\n"
            "- Original question-bank reuse was checked before source-grounded construction.\n"
            "- Existing embeddings/indexes may be reused because corpus bytes did not change. QA generation and Judge scoring must be rerun.\n",
            encoding="utf-8",
        )
        stage.rename(output)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        questions, gold_rows, lineage_rows, removed_rows, bank_review = build_rows()
        result = {
            "status": "OK",
            "dry_run": True,
            "questions": len(questions),
            "gold": len(gold_rows),
            "lineage": len(lineage_rows),
            "removed": len(removed_rows),
            "source_bank_priority": bank_review,
        }
    else:
        result = {"status": "OK", "dry_run": False, "summary": materialize(args.output.resolve())}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
