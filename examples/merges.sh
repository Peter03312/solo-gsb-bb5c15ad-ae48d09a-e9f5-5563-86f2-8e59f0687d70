#!/usr/bin/env bash
# 本轮用法端到端示例: 机构归并组 (同一机构的不同名称归为同一冲突判定组)
#   录入 -> 预演/发布 -> 确认/交评语 -> 会务方归并机构名称 (事务内核对修订号) ->
#   同组重复提交幂等 -> 预演解释以 *_via_merged_group 标明归并造成的冲突 ->
#   已确认槽位因归并失效 -> 补位释放该槽位 (双版本复核) -> 评语继承/归档可追溯
# 建议对新库运行: ./examples/merges.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名评审人 + 1 篇论文 (注意先清空库或使用新库) =="
# R1 的机构登记为 "Univ-X", R2 登记为 "X大学": 实为同一机构的两个名称
for spec in 'R1 r1-secret Univ-X' 'R2 r2-secret X大学' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Author-Inst"]}'

echo "== 2. 预演 + 发布: P1 分给 R1(Univ-X) 与 R2(X大学) =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j '{"serial": d["serial"], "plan_P1": d["plan"]["P1"]}'

echo "== 3. 两人确认并各交一份正式评语 =="
SERIAL=$(curl -s "$BASE/assignment" -H "$ORG" | j 'd["serial"]')
for R in R1 R2; do
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $R" -H "X-Reviewer-Credential: ${R,,}-secret" \
    -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
done
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 4, \"comment\": \"方法扎实, 建议补充实验。\"}"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret' \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SERIAL, \"score\": 3, \"comment\": \"选题尚可, 写作需打磨。\"}"

echo "== 4. 会务方归并: Univ-X 与 X大学 是同一机构 (携带所见资料修订号) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/institutions/merges" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Univ-X\", \"name_b\": \"X大学\", \"base_revision\": $REV}"; echo
# -> {"ok":true,"group_id":"ig-...","names":["Univ-X","X大学"],"changed":true,"revision":N+1}

echo "== 4b. 同组重复提交 (含颠倒顺序) -> 幂等 changed=false, 不推进修订号 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/institutions/merges" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"X大学\", \"name_b\": \"Univ-X\", \"base_revision\": $REV}"; echo

echo "== 4c. 空白名称 -> 422; 未知名称 -> 404; 过期修订号 -> 409 (均不留部分变更) =="
curl -s -o /dev/null -w '空白名称         -> HTTP %{http_code}\n' -X POST "$BASE/institutions/merges" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"  \", \"name_b\": \"Univ-X\", \"base_revision\": $REV}"
curl -s -o /dev/null -w '未知名称         -> HTTP %{http_code}\n' -X POST "$BASE/institutions/merges" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Univ-X\", \"name_b\": \"不存在机构\", \"base_revision\": $REV}"
curl -s -o /dev/null -w '过期修订号       -> HTTP %{http_code}\n' -X POST "$BASE/institutions/merges" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Univ-X\", \"name_b\": \"X大学\", \"base_revision\": 0}"

echo "== 5. 会务方查看当前全部归并组 (原始机构名称在资料中保持原样) =="
curl -s "$BASE/institutions/merges" -H "$ORG"; echo
curl -s "$BASE/reviewers" -H "$ORG" | j '[r["institution"] for r in d["reviewers"]]'

echo "== 6. 归并使 R1/R2 的已确认槽位失效: 补位预演标明 fixed_pair_violates_institution_rule_via_merged_group =="
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
echo "$DRY" | j "{'feasible': d['feasible'], 'plan_P1': d['plan']['P1'], 'fixed_P1': d['fixed']['P1'], 'problems_P1': d['fixed_problems']['P1']}"
BREV=$(echo "$DRY" | j 'd["revision"]')
BSER=$(echo "$DRY" | j 'd["serial"]')

echo "== 7. 补位发布 (双版本复核: 过期即 409); R1 槽位与评语沿用, R2 被替换 =="
curl -s -o /dev/null -w '过期双版本       -> HTTP %{http_code}\n' -X POST "$BASE/assignment/backfill/publish" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $((BREV - 1)), \"base_serial\": $BSER}"
curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $BREV, \"base_serial\": $BSER}" \
  | j "{'ok': d['ok'], 'serial': d['serial'], 'plan_P1': d['plan']['P1'], 'carried_confirmations': d['carried_confirmations'], 'carried_reviews': d['carried_reviews']}"

echo "== 8. 评语继承与追溯: R1 评语同收据沿用; R2 评语归档仅供会务方追溯 =="
curl -s "$BASE/papers/P1/reviews" -H "$ORG" \
  | j "{'progress': d['progress'], 'slots': [(s['reviewer_id'], s['state'], s['submitted']) for s in d['slots']], 'archived': [(a['reviewer_id'], a['serial'], a['comment']) for a in d['archived_reviews']]}"

echo "== 9. 归并也影响作者同机构判定: 把 Inst-C 与作者机构 Author-Inst 归并 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/institutions/merges" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Inst-C\", \"name_b\": \"Author-Inst\", \"base_revision\": $REV}" | j 'd["names"]'
echo "-- 普通预演: R3 被排除并标明 same_institution_as_author_via_merged_group --"
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'plan_P1': d['plan']['P1'], 'excluded': [e for e in d['papers']['P1']['excluded'] if e['reviewer_id'] == 'R3']}"

echo "== 10. 归并造成的合格者同机构诊断: eligible_reviewers_share_single_institution_via_merged_group =="
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Inst-D", "Author-Inst"]}'
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'P2_unassigned': 'P2' in d['unassigned'], 'P2_diagnostics': d['diagnostics'].get('P2')}"
