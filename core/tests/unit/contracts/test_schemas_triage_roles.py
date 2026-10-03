import pytest
from contract_samples import assessment, changed, claim_verification, paths, without

from tightrein.contracts import validate

CLAIM = "runner/roles/claim-verifier.schema.json"
REFUTER = "runner/roles/refuter.schema.json"
DEDUP = "runner/tasks/triage-dedup.schema.json"

DEDUP_RESULT = {"sameRootCause": True, "target": "0007", "evidence": ["src/Services/OrderService.cs:88"],
                "reason": "两个问题都来自 Query 未校验分页参数"}
VALID = [
    (CLAIM, claim_verification()),
    (CLAIM, changed(claim_verification(), verdict="refuted", impact=None,
                    sourceOfPhenomenon={"location": None, "factRef": 1, "explanation": "上游已校验"},
                    rootCauses=[], incidental=[], assessment=None)),
    (CLAIM, changed(claim_verification(), verdict="insufficient",
                    missingInfo=[{"item": "生产数据量", "source": "user"}])),
    (REFUTER, claim_verification()),
    (CLAIM, changed(claim_verification(), assessment=changed(assessment(), worth="defer", reevaluateWhen="再出现 3 次"))),
    (DEDUP, DEDUP_RESULT),
    (DEDUP, {"sameRootCause": False, "target": None, "evidence": [], "reason": "根因位置不同"}),
]

INVALID = [
    (CLAIM, changed(claim_verification(), verdict="maybe"), "$.verdict"),
    (CLAIM, changed(claim_verification(), facts=[{"location": "OrderService.cs", "observation": "x"}]),
     "$.facts[0].location"),
    (CLAIM, changed(claim_verification(), impact=changed(claim_verification()["impact"], kind="crash")),
     "$.impact"),
    (CLAIM, without(claim_verification(), "missingInfo"), "$"),
    (CLAIM, changed(claim_verification(), counterEvidence=[{"check": "入口", "location": "src/A.cs:1", "result": "无"}]),
     "$.counterEvidence[0]"),
    (CLAIM, changed(claim_verification(), sourceOfPhenomenon="上游已校验"), "$.sourceOfPhenomenon"),
    (REFUTER, changed(claim_verification(), tradeoffHit="TO-3"), "$.tradeoffHit"),
    (CLAIM, without(claim_verification(), "analysis"), "$"),
    (CLAIM, changed(claim_verification(), assessment=changed(assessment(), taskType="perf")), "$.assessment"),
    (CLAIM, changed(claim_verification(), assessment=changed(assessment(), flags={
        "design": {"flagged": True}, "dataStructure": {"flagged": False}, "publicContract": {"flagged": False}})),
     "$.assessment"),
    (DEDUP, changed(DEDUP_RESULT, evidence=[]), "$.evidence"),
    (DEDUP, changed(DEDUP_RESULT, target="R-1"), "$.target"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)


def test_refuter_inlines_to_claim_verifier_fields():
    refuter = validate.inline(REFUTER)
    claim = validate.inline(CLAIM)
    assert refuter["title"] != claim["title"]
    assert without(refuter, "title") == without(claim, "title")
