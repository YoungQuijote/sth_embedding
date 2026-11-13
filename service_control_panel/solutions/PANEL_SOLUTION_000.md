# 示例交付文档

*具体内容可以自行发挥，交付内容表达清晰即可；下面以一个模板进行演示*

---

# Agent Delivery Report

## 0. Delivery Metadata

- Feature ID:
- Feature Name:
- Agent Name:
- Execution Date:
- Git Branch:
- Delivery Status: READY_FOR_REVIEW

## 1. Executive Summary

### Objective
本次交付的目标。

### Implementation Summary
实际完成的主要工作。

### Delivery Result
- Completed:
- Partially Completed:
- Not Completed:

## 2. Requirement Traceability

| Requirement ID | Acceptance Criteria | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-001 | ... | ... | TEST-001 | PASS |
| REQ-002 | ... | ... | TEST-002 | PASS |

要求：每一条验收标准都必须有对应的实现和验证记录。

## 3. Implementation Details

### Code Changes
| File | Change Type | Description |
|---|---|---|
| src/... | Added | ... |
| src/... | Modified | ... |

### Architecture Changes
- New components:
- Modified components:
- Dependencies:
- API changes:
- Database changes:

## 4. Test & Verification Results

| Test Type | Test Case | Result | Evidence |
|---|---|---|---|
| Unit Test | ... | PASS | ... |
| Integration Test | ... | PASS | ... |
| E2E Test | ... | PASS | ... |
| Security Test | ... | NOT RUN | ... |

### Test Summary
- Total:
- Passed:
- Failed:
- Skipped:
- Coverage:

### Verification Commands
记录实际执行的命令、环境和结果。

## 5. Deviations & Decisions

记录实际实现与原始 SPEC 不一致的地方。

| Item | Original Spec | Actual Implementation | Reason | Approval |
|---|---|---|---|---|

没有偏差时填写 None。

## 6. Known Issues & Risks

| Issue | Severity | Impact | Mitigation |
|---|---|---|---|

## 7. Deployment & Rollback

### Deployment
- Configuration:
- Environment Variables:
- Database Migration:
- Deployment Steps:

### Rollback
- Rollback Steps:
- Data Recovery:
- Feature Flag:

不适用时填写 N/A。

## 8. Deliverables

- Source Code:
- Test Reports:
- API Documentation:
- Database Migration:
- Build Artifacts:
- Other Documents:

## 9. Handoff

### Remaining Tasks
尚未完成的工作。

### Follow-up Recommendations
后续建议。

### Operational Notes
运行、监控和维护注意事项。

## 10. Final Checklist

- [ ] All requirements traced
- [ ] All acceptance criteria verified
- [ ] Unit tests completed
- [ ] Integration tests completed
- [ ] No unresolved critical issues
- [ ] Documentation updated
- [ ] SPEC and implementation consistent
- [ ] Evidence attached
- [ ] Ready for human review

## 11. Review & Acceptance

- Agent Self-Assessment:
- Reviewer:
- Review Status: PENDING
- Review Comments:
- Acceptance Date:


---

**注意：以上仅以一个模板进行演示，不是每个模块都得包含，也不是只能用这个模板；需求文档的填写可自由发挥，只要需求表达清晰即可**
