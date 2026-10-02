"""本轮功能测试: 机构归并组 (同一机构的不同名称归为同一冲突判定组)。

覆盖需求:
- 会务方凭密钥提交两条当前论文作者或评审人资料中出现的机构原名 + 所见资料修订号;
  同一事务内先核对修订号: 过期/超前 409, 空白名称 422, 未知名称 404,
  被拒请求不留下部分变更 (不推进修订号、不写归并记录);
- 归并关系传递且不可拆分; 返回归并后的名称组、当前修订号与是否变更;
  同组重复提交幂等 (changed=false, 不推进修订号);
- 普通分配与补位均按归并组判定作者同机构与两名评审人同机构;
  预演解释以 *_via_merged_group 原因代码标明仅因归并才成立的冲突;
  原始机构名称与既有响应字段保持原样;
- 已确认槽位因归并而失效时补位不得固定该槽位; 旧发布版与评语仍可追溯;
  补位发布继续遵守双版本复核与评语继承规则。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-merges-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots", "reviewer_status_changes",
            "institution_merge_groups", "institution_merge_members",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution=None, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution or f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid="P1", topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics else ["AI"],
            "institutions": institutions if institutions is not None else ["Univ-X"],
        },
    )


def current_revision(client):
    return client.get("/meta", headers=ORG).json()["revision"]


def merge(client, name_a, name_b, revision=None):
    if revision is None:
        revision = current_revision(client)
    return client.post(
        "/institutions/merges",
        headers=ORG,
        json={"name_a": name_a, "name_b": name_b, "base_revision": revision},
    )


def setup_and_publish(client, papers=("P1",)):
    """4 名不同机构评审人 (Inst-R1..R4, 容量 3) + 论文 (作者机构 Univ-X)。返回 plan。"""
    for rid in ("R1", "R2", "R3", "R4"):
        assert add_reviewer(client, rid).status_code == 201
    for pid in papers:
        assert add_paper(client, pid).status_code == 201
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()["plan"]


def decide(client, rid, pid, decision, reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def submit_review(client, rid, pid, score=4, comment="评语"):
    serial = client.get("/assignment", headers=ORG).json()["serial"]
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


# ------------------------------------------------------------ 归并接口: 校验与幂等

def test_merge_requires_organizer_key(client):
    add_reviewer(client, "R1", "Inst-A")
    r = client.post(
        "/institutions/merges",
        json={"name_a": "Inst-A", "name_b": "Inst-A", "base_revision": 1},
    )
    assert r.status_code == 401
    assert client.get("/institutions/merges").status_code == 401


def test_merge_creates_group_and_bumps_revision(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    rev = current_revision(client)
    r = merge(client, "Inst-A", "Inst-B")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["revision"] == rev + 1
    assert j["names"] == ["Inst-A", "Inst-B"]
    assert j["group_id"]
    # 会务方可查看当前全部归并组
    listed = client.get("/institutions/merges", headers=ORG).json()
    assert listed["revision"] == rev + 1
    assert len(listed["groups"]) == 1
    group = listed["groups"][0]
    assert group["group_id"] == j["group_id"]
    assert group["names"] == ["Inst-A", "Inst-B"]
    assert group["created_revision"] == rev + 1


def test_merge_same_group_is_idempotent(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    rev1 = merge(client, "Inst-A", "Inst-B").json()["revision"]
    # 同组重复提交 (含颠倒顺序) -> 幂等, 不推进修订号
    for a, b in (("Inst-A", "Inst-B"), ("Inst-B", "Inst-A")):
        r = merge(client, a, b, revision=rev1)
        assert r.status_code == 200
        j = r.json()
        assert j["changed"] is False
        assert j["revision"] == rev1
        assert j["names"] == ["Inst-A", "Inst-B"]
    assert current_revision(client) == rev1
    assert len(client.get("/institutions/merges", headers=ORG).json()["groups"]) == 1


def test_merge_same_name_is_idempotent_noop(client):
    add_reviewer(client, "R1", "Inst-A")
    rev = current_revision(client)
    r = merge(client, "Inst-A", "Inst-A")
    assert r.status_code == 200
    j = r.json()
    assert j["changed"] is False and j["revision"] == rev
    assert j["names"] == ["Inst-A"]
    assert current_revision(client) == rev
    assert client.get("/institutions/merges", headers=ORG).json()["groups"] == []


def test_merge_is_transitive_and_not_splittable(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    rev1 = merge(client, "Inst-A", "Inst-B").json()["revision"]
    # 传递: 已归并的 Inst-B 再与 Inst-C 归并 -> 三名称同组
    r = merge(client, "Inst-B", "Inst-C", revision=rev1)
    assert r.status_code == 200
    j = r.json()
    assert j["changed"] is True
    assert j["names"] == ["Inst-A", "Inst-B", "Inst-C"]
    # 组内任意两个名称的再提交均幂等
    r = merge(client, "Inst-A", "Inst-C", revision=j["revision"])
    assert r.json()["changed"] is False
    groups = client.get("/institutions/merges", headers=ORG).json()["groups"]
    assert len(groups) == 1 and groups[0]["names"] == ["Inst-A", "Inst-B", "Inst-C"]


def test_merge_blank_unknown_stale_rejected_without_partial_changes(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X"])
    rev = current_revision(client)
    # 空白名称 -> 422
    assert merge(client, "  ", "Inst-A").status_code == 422
    assert merge(client, "Inst-A", "").status_code == 422
    # 未知名称 (未出现在任何论文作者或评审人资料中) -> 404
    assert merge(client, "Inst-A", "Inst-GHOST").status_code == 404
    assert merge(client, "Inst-GHOST", "Inst-GONE").status_code == 404
    # 过期/超前修订号 -> 409 (优先于其他检查)
    assert merge(client, "Inst-A", "Univ-X", revision=rev + 5).status_code == 409
    assert merge(client, "  ", "Inst-GHOST", revision=rev + 5).status_code == 409
    # 全部被拒且不留下部分变更: 修订号未推进, 无任何归并组
    assert current_revision(client) == rev
    assert client.get("/institutions/merges", headers=ORG).json()["groups"] == []


def test_merge_name_known_only_via_paper_authors(client):
    add_reviewer(client, "R1", "Inst-A")
    add_paper(client, "P1", institutions=["Univ-X", "Univ-X-医学院"])
    r = merge(client, "Univ-X", "Univ-X-医学院")
    assert r.status_code == 200 and r.json()["changed"] is True


def test_merge_name_no_longer_in_data_is_unknown(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    client.delete("/reviewers/R2", headers=ORG)  # Inst-B 不再出现在任何资料中
    assert merge(client, "Inst-A", "Inst-B").status_code == 404


# ------------------------------------------------------------ 归并影响普通分配

def test_dry_run_marks_author_conflict_via_merged_group(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_reviewer(client, "R4", "Univ-X")  # 与作者原始名称直接相同
    add_paper(client, "P1", institutions=["Univ-X"])
    merge(client, "Inst-A", "Univ-X")  # Inst-A 与作者机构实为同一机构
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is True
    assert body["plan"]["P1"] == ["R2", "R3"]
    reasons = {e["reviewer_id"]: e["reason"] for e in body["papers"]["P1"]["excluded"]}
    # 仅因归并才成立的冲突单独标明; 原始名称直接相同的冲突保持原代码
    assert reasons["R1"] == "same_institution_as_author_via_merged_group"
    assert reasons["R4"] == "same_institution_as_author"


def test_dry_run_reviewer_pair_conflict_via_merged_group(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["plan"]["P1"] == ["R1", "R2"]
    # 归并后 R1/R2 视为同机构, 不得同评一篇
    merge(client, "Inst-A", "Inst-B")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["plan"]["P1"] == ["R1", "R3"]


def test_diagnostics_mark_shared_institution_via_merged_group(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-D", avoid=["P1"])
    add_paper(client, "P1")
    merge(client, "Inst-A", "Inst-B")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    assert body["feasible"] is False
    diag = body["diagnostics"]["P1"]
    assert diag["eligible_reviewers"] == ["R1", "R2"]
    assert "eligible_reviewers_share_single_institution_via_merged_group" in diag["reasons"]
    assert "eligible_reviewers_share_single_institution" not in diag["reasons"]
    assert "no_valid_reviewer_pair" in diag["reasons"]


def test_diagnostics_plain_shared_institution_unchanged(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-A")
    add_reviewer(client, "R3", "Inst-D", avoid=["P1"])
    add_paper(client, "P1")
    body = client.post("/assignment/dry-run", headers=ORG).json()
    diag = body["diagnostics"]["P1"]
    assert "eligible_reviewers_share_single_institution" in diag["reasons"]
    assert "eligible_reviewers_share_single_institution_via_merged_group" not in diag["reasons"]


def test_original_institution_names_preserved(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_paper(client, "P1", institutions=["Univ-X"])
    merge(client, "Inst-A", "Univ-X")
    merge(client, "Inst-A", "Inst-B")
    reviewers = {r["reviewer_id"]: r for r in client.get("/reviewers", headers=ORG).json()["reviewers"]}
    assert reviewers["R1"]["institution"] == "Inst-A"
    assert reviewers["R2"]["institution"] == "Inst-B"
    paper = client.get("/papers", headers=ORG).json()["papers"][0]
    assert paper["institutions"] == ["Univ-X"]


def test_merge_bumps_revision_so_stale_publish_rejected(client):
    add_reviewer(client, "R1", "Inst-A")
    add_reviewer(client, "R2", "Inst-B")
    add_reviewer(client, "R3", "Inst-C")
    add_paper(client, "P1")
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    merge(client, "Inst-A", "Inst-B")
    assert client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev}
    ).status_code == 409
    rev2 = current_revision(client)
    assert rev2 == rev + 1
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev2})
    assert r.status_code == 200
    assert r.json()["plan"]["P1"] == ["R1", "R3"]


# ------------------------------------------------------------ 归并影响补位

def test_backfill_releases_confirmed_pair_invalidated_by_merge(client):
    plan = setup_and_publish(client)
    assert plan["P1"] == ["R1", "R2"]
    decide(client, "R1", "P1", "confirm")
    decide(client, "R2", "P1", "confirm")
    # 归并使两名已确认评审人变为同机构 -> 已确认槽位失效, 补位不得固定该槽位
    merge(client, "Inst-R1", "Inst-R2")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert dry["fixed"]["P1"] == ["R1"]
    problems = {(p["reviewer_id"], p["reason"]) for p in dry["fixed_problems"]["P1"]}
    assert ("R2", "fixed_pair_violates_institution_rule_via_merged_group") in problems
    assert dry["plan"]["P1"] == ["R1", "R3"]
    # 双版本复核不变: 过期修订号/序号 -> 409
    assert client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"] - 1, "base_serial": dry["serial"]},
    ).status_code == 409
    r = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["plan"]["P1"] == ["R1", "R3"]
    assert j["carried_confirmations"] == 1
    pub = client.get("/assignment", headers=ORG).json()
    states = {s["reviewer_id"]: s["state"] for s in pub["papers"]["P1"]["slots"]}
    assert states == {"R1": "confirmed", "R3": "pending"}


def test_backfill_releases_confirmed_slot_conflicting_author_via_merge(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]  # R1, R2
    decide(client, rid_a, "P1", "confirm")
    # 归并 rid_a 的机构与作者机构 -> 其已确认槽位因归并失效
    merge(client, f"Inst-{rid_a}", "Univ-X")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"] is True
    assert dry["fixed"]["P1"] == []
    problems = {(p["reviewer_id"], p["reason"]) for p in dry["fixed_problems"]["P1"]}
    assert (rid_a, "same_institution_as_author_via_merged_group") in problems
    assert rid_a not in dry["plan"]["P1"]
    # 普通预演同样按归并组排除并标明
    normal = client.post("/assignment/dry-run", headers=ORG).json()
    reasons = {e["reviewer_id"]: e["reason"] for e in normal["papers"]["P1"]["excluded"]}
    assert reasons[rid_a] == "same_institution_as_author_via_merged_group"


def test_backfill_publish_after_merge_keeps_review_inheritance_rules(client):
    plan = setup_and_publish(client)
    rid_a, rid_b = plan["P1"]  # R1, R2
    decide(client, rid_a, "P1", "confirm")
    decide(client, rid_b, "P1", "confirm")
    r1 = submit_review(client, rid_a, "P1", score=5, comment="甲评语").json()
    r2 = submit_review(client, rid_b, "P1", score=3, comment="乙评语").json()
    old_serial = client.get("/assignment", headers=ORG).json()["serial"]
    # 归并使 rid_b 与作者同机构 -> 其确认槽位与评语被移出; rid_a 槽位与评语沿用
    merge(client, f"Inst-{rid_b}", "Univ-X")
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    r = client.post(
        "/assignment/backfill/publish", headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["carried_confirmations"] == 1
    assert j["carried_reviews"] == 1
    assert rid_b not in j["plan"]["P1"]
    # 连续保留槽位沿用原评语 (同收据); 被移出槽位的评语仅供会务方追溯
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    slots = {s["reviewer_id"]: s for s in view["slots"]}
    assert slots[rid_a]["submitted"] is True
    assert slots[rid_a]["review"]["receipt"] == r1["receipt"]
    assert view["progress"]["submitted"] == 1
    archived = view["archived_reviews"]
    assert len(archived) == 1
    assert archived[0]["reviewer_id"] == rid_b
    assert archived[0]["receipt"] == r2["receipt"]
    assert archived[0]["serial"] == old_serial
    # 旧发布版决定仍可追溯 (旧序号行保留)
    with db.read_txn() as conn:
        row = conn.execute(
            "SELECT state FROM assignment_decisions WHERE serial = ? AND paper_id = 'P1'"
            " AND reviewer_id = ?",
            (old_serial, rid_b),
        ).fetchone()
    assert row["state"] == "confirmed"


def test_merge_does_not_touch_published_version_until_backfill(client):
    plan = setup_and_publish(client)
    decide(client, plan["P1"][0], "P1", "confirm")
    before = client.get("/assignment", headers=ORG).json()
    merge(client, "Inst-R1", "Inst-R2")
    after = client.get("/assignment", headers=ORG).json()
    assert before == after  # 归并本身不改变已发布版本
